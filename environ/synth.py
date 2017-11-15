"""A class for training a SeqGAN model on synthetic data."""

import logging
import time

import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import functional as nnf

import common
from common import LABEL_GEN, LABEL_REAL
import dataset
import model

from .environment import Environment

class SynthEnvironment(Environment):
    """Functions for training a model on a synthetic dataset."""

    @classmethod
    def get_opt_parser(cls):
        """Returns an `ArgumentParser` that parses env-specific opts."""
        parser = super(SynthEnvironment, cls).get_opt_parser()
        parser.add_argument(
            '--oracle-type', default=model.generator.RNN,
            choices=model.generator.TYPES)
        parser.add_argument('--grad-reg', default=0, type=float)
        parser.add_argument('--oracle-dim', default=128, type=int)
        parser.add_argument('--use-oracle-w2v', action='store_true')
        parser.set_defaults(
            num_gen_samps=100000,
            seqlen=20,
            vocab_size=5000,
            g_tok_emb_dim=32,
            d_tok_emb_dim=32,
            pretrain_g_epochs=50,  # try 20 when using oracle w2v
            pretrain_d_epochs=10,
            train_hasher_epochs=25,
            adv_train_iters=750,
            rnn_dim=32,
            code_len=6,
            dropout=0.25,
            num_gen_layers=1,
            lr_g=0.01,
            lr_d=0.001,
            lr_hasher=0.002,
            )
        return parser

    def __init__(self, opts):
        """Creates a SynthEnvironment."""
        torch.nn._functions.rnn.force_unfused = opts.grad_reg  # pylint: disable=protected-access

        super(SynthEnvironment, self).__init__(opts)

        self.ro_init_toks.data.zero_()

        self.oracle = self._create_oracle().cuda()
        oracle_checksum = sum(p.data.sum() for p in self.oracle.parameters())
        logging.debug(f'#oracle: {oracle_checksum:.3f}')

        self.oracle_dataset = self._create_dataset(self.oracle, LABEL_REAL)
        self.oracle_test_set = self._create_dataset(
            self.oracle, LABEL_REAL,
            num_samples=len(self.ro_init_toks)*5, seed=-1)

        if self.opts.use_oracle_w2v:
            for net in (self.g, self.d):
                net.tok_emb = model.utils.Apply(self.oracle.tok_emb,
                                                detach=True)

    def _create_oracle(self):
        """Returns a randomly initialized generator."""
        with common.rand_state(torch, self.opts.seed):
            opt_vars = vars(self.opts)
            opt_vars.pop('rnn_dim')
            oracle = model.generator.create(
                gen_type=self.opts.oracle_type,
                rnn_dim=self.opts.oracle_dim,
                **opt_vars)
            for param in oracle.parameters():
                nn.init.normal(param, std=1)
        return oracle

    def _create_dataset(self, gen, label, num_samples=None, seed=None):
        num_samples = num_samples or self.opts.num_gen_samps
        seed = seed or self.opts.seed
        return dataset.GenDataset(generator=gen,
                                  label=label,
                                  seqlen=self.opts.seqlen,
                                  seed=seed,
                                  gen_init_toks=self.ro_init_toks,
                                  num_samples=num_samples)

    def compute_oracle_nll(self, toks, return_probs=False):
        """
        oracle: a Generator
        toks: [N]*T
        """
        if isinstance(toks, list):
            toks = torch.cat(toks).view(len(toks), -1)  # T*N
        gen_log_probs = self.get_tok_log_probs(self.oracle, toks.t())
        flat_log_probs = gen_log_probs.view(-1, gen_log_probs.size(-1))  # T*N*V
        nll = nnf.nll_loss(flat_log_probs, toks.view(-1)).data[0]
        if return_probs:
            return nll, gen_log_probs
        return nll

    def _compute_test_nll(self, num_samples=256):
        test_nll = 0
        num_test_batches = max(num_samples // len(self.init_toks), 1)
        with common.rand_state(torch.cuda, -1):
            for _ in range(num_test_batches):
                gen_seqs, _ = self.g.rollout(self.init_toks, self.opts.seqlen)
                test_nll += self.compute_oracle_nll(gen_seqs)
        test_nll /= num_test_batches
        return test_nll

    def _compute_test_acc(self, num_samples=256):
        num_test_batches = max(num_samples // len(self.init_toks), 1)

        oracle_test_loader = self._create_dataloader(self.oracle_test_set)
        acc_oracle = 0
        for i, batch in enumerate(oracle_test_loader):
            if i == num_test_batches:
                break
            oracle_test_toks = Variable(
                next(test_loader)[0][:, 1:].cuda())  # slice off init toks
            acc_oracle += self.compute_acc(self.d(oracle_test_toks), LABEL_REAL)
        acc_oracle /= i

        acc_gen = 0
        with common.rand_state(torch.cuda, -1):
            for _ in range(num_test_batches):
                gen_seqs, _ = self.g.rollout(self.init_toks, self.opts.seqlen)
                acc_gen += self.compute_acc(self.d(gen_seqs), LABEL_GEN)
        acc_gen /= num_test_batches

        return acc_gen, acc_oracle

    def _compute_hasher_test_loss(self):
        test_loader = self._create_dataloader(self.oracle_test_set)
        return sum(self._forward_hasher_train(batch, volatile=True)[0].data[0]
                   for batch in test_loader) / len(test_loader)

    def train_hasher(self):
        """Train an auto-encoder on the dataset for use in hashing."""
        dataloader = self._create_dataloader(self.oracle_dataset)
        self.hasher.train()
        for epoch in range(1, self.opts.train_hasher_epochs + 1):
            tick = time.time()
            train_loss = train_entropy = 0
            for batch in dataloader:
                loss, _, code_logits = self._forward_hasher_train(batch)
                train_loss += loss.data[0]

                entropy = self._get_entropy(code_logits)[0]
                loss -= entropy * self.opts.hasher_ent_reg
                train_entropy += entropy.data[0]

                self.optim_hasher.zero_grad()
                loss.backward()
                self.optim_hasher.step()
            train_loss /= len(dataloader)
            train_entropy /= len(dataloader)

            test_loss = self._compute_hasher_test_loss()
            logging.info(
                f'[{epoch:02d}]  '
                f'loss: train={train_loss:.3f} test={test_loss:.3f}  '
                f'H: {train_entropy:.3f}  '
                f'({time.time() - tick:.1f})')

    def pretrain_g(self):
        """Pretrains G using maximum-likelihood on a synthetic dataset."""

        dataloader = self._create_dataloader(self.oracle_dataset)

        logging.info(f'[00] nll: {self._compute_test_nll():.3f}')
        for epoch in range(1, self.opts.pretrain_g_epochs + 1):
            tick = time.time()
            train_loss = entropy = 0
            for batch in dataloader:
                loss, gen_log_probs = self._forward_g_pretrain(batch)
                entropy += self._get_entropy(gen_log_probs)[1].data[0]
                train_loss += loss.data[0]

                self.optim_g.zero_grad()
                loss.backward()
                self.optim_g.step()
            train_loss /= len(dataloader)
            entropy /= len(dataloader)

            oracle_nll = self._compute_test_nll()
            logging.info(
                f'[{epoch:02d}] loss: {train_loss:.3f}  nll: {oracle_nll:.3f}  '
                f'H: {entropy:.2f}  '
                f'gnorm: {self._get_grad_norm(self.g).data[0]:.2f}  '
                f'({time.time() - tick:.1f})')

    def pretrain_d(self):
        """Pretrains D using pretrained G."""

        for epoch in range(1, self.opts.pretrain_d_epochs+1):
            tick = time.time()
            gen_dataset = self._create_dataset(self.g, LABEL_GEN,
                                               seed=self.opts.seed+epoch)
            dataloader = self._create_dataloader(torch.utils.data.ConcatDataset(
                (self.oracle_dataset, gen_dataset)))

            train_loss = 0
            gnorm = 0
            for batch in dataloader:
                loss, _ = self._forward_d(batch)

                train_loss += loss.data[0]

                self.optim_d.zero_grad()
                loss.backward()
                gnorm += sum(
                    (p.grad.data**2).sum() for p in self.d.parameters(dx2=True))
                self.optim_d.step()
            train_loss /= len(dataloader)
            gnorm /= len(dataloader)

            acc_gen, acc_oracle = self._compute_test_acc()
            logging.info(f'[{epoch:02d}] loss: {train_loss:.3f}  '
                         f'acc: oracle={acc_oracle:.2f}  gen={acc_gen:.2f}  '
                         f'gnorm: {gnorm:.2f}  '
                         f'({time.time() - tick:.1f})')

    def _get_qs(self, g_ro, rep_gen_seqs):
        qs = Variable(torch.cuda.FloatTensor(
            self.opts.seqlen, self.opts.batch_size).zero_())

        qs[-1] = self.d(rep_gen_seqs[:qs.size(1)])[:, LABEL_REAL]

        if self.opts.num_rollouts == 0:
            qs.data[:-1] = qs.data[None, -1].expand(qs.size(0) - 1, qs.size(1))
            qs.data.exp_()
            return qs.t().detach()

        ro_rng = torch.cuda.get_rng_state()
        _, ro_hid = g_ro(self.ro_init_toks)
        for n in range(1, self.opts.seqlen):
            # ro_seqs, _  = g_ro.rollout(rep_gen_seqs[:,:n], self.opts.seqlen-n)

            torch.cuda.set_rng_state(ro_rng)
            ro_state = (rep_gen_seqs[:, n-1].unsqueeze(-1), ro_hid)
            ro_seqs, _, (ro_hid, ro_rng) = g_ro.rollout(
                ro_state, self.opts.seqlen - n, return_first_state=True)
            full_ro = torch.cat([rep_gen_seqs[:, :n]] + ro_seqs, -1)
            assert full_ro.size(1) == self.opts.seqlen

            q = self.d(full_ro).view(self.opts.num_rollouts, -1, 2)
            # LABEL_G gives cost, LABEL_REAL gives reward
            qs[n-1] = q.mean(0)[:, LABEL_REAL]

        qs.data.exp_()
        return qs.t().detach()

    def _get_advantages(self, gen_seqs):
        rep_gen_seqs = gen_seqs.repeat(self.opts.num_rollouts, 1)
        qs_g = self._get_qs(self.g, rep_gen_seqs)

        advs = qs_g

        # advs = advs[:, self._inv_idx].cumsum(1)[:, self._inv_idx]  # adv to go
        advs -= advs.mean()
        advs /= advs.std()
        return advs.detach()

    @staticmethod
    def _get_grad_norm(mod):
        return sum((p.grad**2).sum() for p in mod.parameters(dx2=True))

    def _ds_iter(self, dataset, batch_size):
        batch_idxs_it = iter(())
        while True:
            try:
                yield dataset[next(batch_idxs_it)]
            except StopIteration:
                batch_idxs = torch.randperm(len(dataset)).split(batch_size)
                if len(dataset) % batch_size:
                    batch_idxs = batch_idxs[:-1]
                batch_idxs_it = iter(batch_idxs)

    def train_adv(self):
        """Adversarially train G against D."""

        self.optim_g.param_groups[0]['lr'] *= 0.01
        if self.opts.exploration_bonus:
            self.hasher.eval()

        oracle_dataloader = iter(
            self._create_dataloader(self.oracle_dataset, cycle=True))

        replay_buffer, rbuf_loader = self._create_replay_buffer(100, LABEL_GEN)
        replay_buffer_iter = None

        for i in range(1, self.opts.adv_train_iters+1):
            tick = time.time()

            loss_g, gen_seqs, entropy_g = self._train_adv_g(replay_buffer)
            self.optim_g.zero_grad()
            loss_g.backward(create_graph=bool(self.opts.grad_reg))
            if not self.opts.grad_reg:
                self.optim_g.step()

            if replay_buffer_iter is None:
                replay_buffer_iter = iter(rbuf_loader)

            loss_d = self._train_adv_d(gen_seqs, oracle_dataloader,
                                       replay_buffer_iter)
            self.optim_d.zero_grad()
            loss_d.backward(create_graph=bool(self.opts.grad_reg))

            gnormg, gnormd = map(self._get_grad_norm, (self.g, self.d))
            gnorm = (gnormg * (self.opts.grad_reg * 50.) +
                     gnormd * (self.opts.grad_reg * 0.1))
            # nn.utils.clip_grad_norm(self.g.parameters(), 5)
            # nn.utils.clip_grad_norm(self.d.parameters(), 1)
            if self.opts.grad_reg:
                gnorm.backward()
                self.optim_g.step()
            self.optim_d.step()

            acc_gen, acc_oracle = self._compute_test_acc()
            test_nll = self._compute_test_nll()
            logging.info(
                f'[{i:03d}] nll: {test_nll:.3f}  '
                f'acc: o={acc_oracle:.2f} g={acc_gen:.2f}  '
                f'gnorm: g={gnormg.data[0]:.2f} d={gnormd.data[0]:.2f}  '
                f'H: {entropy_g.data[0]:.2f}  '
                f'({time.time() - tick:.1f})')

    def _train_adv_g(self, replay_buffer):
        losses = []
        entropies = []
        for i in range(self.opts.adv_g_iters):
            # train G
            gen_seqs, gen_log_probs = self.g.rollout(
                self.init_toks, self.opts.seqlen,
                temperature=self.opts.temperature)
            gen_seqs = torch.cat(gen_seqs, -1)  # N*T
            if i == 0:
                replay_buffer.add_samples(gen_seqs)

            gen_log_probs = torch.stack(gen_log_probs)  # T*N*V
            seq_log_probs = self._gather_act_probs(gen_seqs, gen_log_probs)

            advantages = self._get_advantages(gen_seqs)  # N*T
            score = (seq_log_probs * advantages).sum(1).mean()
            if self.opts.exploration_bonus:
                score = score + self._get_exploration_bonus(gen_seqs)

            disc_entropy, entropy = self._get_entropy(
                gen_log_probs, discount_rate=self.opts.discount)

            # _, roomtemp_lprobs = self.g.rollout(
            #     self.init_toks, self.opts.seqlen, temperature=1)
            # roomtemp_lprobs = torch.stack(roomtemp_lprobs)
            # _, entropy = self._get_entropy(roomtemp_lprobs)

            entropies.append(entropy)
            losses.append(-score - disc_entropy * self.opts.g_ent_reg)

        loss = sum(losses)
        avg_entropy = sum(entropies) / len(entropies)
        return loss, gen_seqs, avg_entropy

    def _train_adv_d(self, gen_seqs, oracle_dataloader, replay_buffer_iter):
        REAL_W = 0.5
        GEN_W = (1 - REAL_W)
        RBUF_W = 0.5

        loss_d, d_log_probs = self._forward_d(next(oracle_dataloader))
        loss_d *= REAL_W
        entropy_d = REAL_W * self._get_entropy(d_log_probs)[0]

        n_rbuf_batches = min(len(replay_buffer_iter) // self.opts.batch_size, 4)
        cur_w = GEN_W
        if n_rbuf_batches:
            cur_w *= RBUF_W
            rbuf_batch_w = GEN_W * RBUF_W / n_rbuf_batches

        self._labels.fill_(LABEL_GEN)
        loss_d_g, d_log_probs = self._forward_d(
            (gen_seqs.data, self._labels), has_init=False)

        loss_d += cur_w * loss_d_g
        entropy_d += cur_w * self._get_entropy(d_log_probs)[0]

        for _ in range(n_rbuf_batches):
            loss_d_g, d_log_probs = self._forward_d(
                next(replay_buffer_iter), has_init=False)
            loss_d += rbuf_batch_w * loss_d_g
            entropy_d += rbuf_batch_w * self._get_entropy(d_log_probs)[0]

        return loss_d - entropy_d * self.opts.d_ent_reg
