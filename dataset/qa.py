"""A Dataset that loads the Amazon product QA."""

import os

import torch
import torch.utils.data

from common import EXTRA_VOCAB, UNK, BOS, EOS
import common


class QADataset(torch.utils.data.Dataset):
    """Loads the data."""

    def __init__(self, data_dir, vocab_size, seqlen, part, **unused_kwargs):
        super(QADataset, self).__init__()

        self.seqlen = seqlen
        self.part = part

        self.vocab = (common.unpickle(os.path.join(data_dir, 'vocab.pkl'))
                      .add_extra_vocab(EXTRA_VOCAB)
                      .truncate(vocab_size).set_unk_tok(UNK))

        self.qs = common.unpickle(os.path.join(data_dir, part + '.pkl'))

    def __getitem__(self, index):
        q = self.qs[index]
        toks = q.split(' ')[:self.seqlen-1]

        qtoks = torch.LongTensor(self.seqlen + 1).zero_()
        qtoks[0] = self.vocab[BOS]
        for i, tok in enumerate(toks, 1):
            qtoks[i] = self.vocab[tok]
        qtoks[len(toks)] = self.vocab[EOS]

        return {
            'toks': qtoks,
            'labels': 1,
        }

    def __len__(self):
        return len(self.qs)

    def decode(self, toks_vec):
        """Turns a vector of token indices into a string."""
        return ' '.join([self.vocab[idx] for idx in toks_vec if idx > 0])


def create(*args, **kwargs):
    """Returns a QADataset."""
    return QADataset(*args, **kwargs)


def test_dataset():
    """Tests the QADataset."""

    # pylint: disable=unused-variable
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'qa')
    part = 'test'
    vocab_size = 25000
    seqlen = 21
    debug = True

    ds = QADataset(**locals())
    datum = ds[0]
    print(datum)
    print(ds.decode(datum['toks']))

    for i in torch.randperm(len(ds)):
        datum = ds[i]
        toks = datum['toks']
        assert (toks >= 0).all() and (toks < vocab_size).all()
