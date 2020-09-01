import unittest

import pytest
import torch
from baal.active import ActiveLearningDataset
from baal.utils.ssl_dataset import SemiSupervisedIterator
from torch.utils.data import Dataset, ConcatDataset


class SSLTestDataset(Dataset):
    """Dataset returns even number for labeled samples and odd numbers for unlabeled samples.
    """

    def __init__(self, labeled=True, length=100):
        if labeled:
            self.data = torch.tensor(range(0, length * 2, 2))
        else:
            self.data = torch.tensor(range(1, length * 2, 2))

    def __getitem__(self, index):
        return self.data[index]

    def __len__(self):
        return len(self.data)


class SSLDatasetTest(unittest.TestCase):
    def setUp(self):
        d1_len = 100
        d2_len = 1000
        d1 = SSLTestDataset(labeled=True, length=d1_len)
        d2 = SSLTestDataset(labeled=False, length=d2_len)
        dataset = ConcatDataset([d1, d2])

        print(len(dataset))

        al_dataset = ActiveLearningDataset(dataset)
        al_dataset.label(list(range(d1_len)))  # Label data from d1 (even numbers)

        self.ss_iterator = SemiSupervisedIterator(al_dataset, p=None, num_steps=None, batch_size=10)

    def test_epoch(self):
        labeled_data = []
        unlabeled_data = []

        for batch_idx, batch in enumerate(self.ss_iterator):
            if SemiSupervisedIterator.is_labeled(batch):
                batch = SemiSupervisedIterator.get_batch(batch)
                labeled_data.extend(batch)
            else:
                batch = SemiSupervisedIterator.get_batch(batch)
                unlabeled_data.extend(batch)

        labeled_data = torch.tensor(labeled_data)
        unlabeled_data = torch.tensor(unlabeled_data)

        assert len(labeled_data) == len(unlabeled_data)
        assert torch.all(labeled_data % 2 == 0)
        assert torch.all(unlabeled_data % 2 != 0)


if __name__ == '__main__':
    pytest.main()
