"""
Semi-supervised model for classification.
Pi-Model from TEMPORAL ENSEMBLING FOR SEMI-SUPERVISED LEARNING (Laine 2017).
"""


import argparse
from argparse import Namespace
from typing import Dict, OrderedDict

import numpy as np
import torch
from baal.active import ActiveLearningDataset
from baal.utils.metrics import Accuracy
from baal.utils.ssl_module import SSLModule
from torch import nn, Tensor
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import CIFAR10
from torchvision.models import vgg16


class GaussianNoise(nn.Module):
    """ Add random gaussian noise to images"""
    def __init__(self, std=0.05):
        super(GaussianNoise, self).__init__()
        self.std = std

    def forward(self, x):
        return x + torch.randn(x.size()).type_as(x) * self.std


class RandomTranslation(nn.Module):
    """Randomly translate images"""
    def __init__(self, augment_translation=10):
        super(RandomTranslation, self).__init__()
        self.augment_translation = augment_translation

    def forward(self, x):
        batch_size = len(x)

        t_min = -self.augment_translation / x.shape[-1]
        t_max = (self.augment_translation + 1) / x.shape[-1]

        matrix = torch.eye(3)[None].repeat((batch_size, 1, 1))
        tx = (t_min - t_max) * torch.rand(batch_size) + t_max
        ty = (t_min - t_max) * torch.rand(batch_size) + t_max

        matrix[:, 0, 2] = tx
        matrix[:, 1, 2] = ty
        matrix = matrix[:, 0:2, :]

        grid = nn.functional.affine_grid(matrix, x.shape).type_as(x)
        x = nn.functional.grid_sample(x, grid)

        return x


class PIModel(SSLModule):
    train_transform = transforms.Compose([transforms.RandomHorizontalFlip(),
                                          transforms.ToTensor()])
    test_transform = transforms.Compose([transforms.ToTensor()])

    def __init__(self, train_set: ActiveLearningDataset, hparams: Namespace, network: nn.Module):
        super().__init__(train_set, hparams)

        self.network = network

        M = len(self.train_set)
        N = (len(self.train_set) + len(self.train_set.pool))
        self.max_unsupervised_weight = self.hparams.w_max * M / N

        self.criterion = nn.CrossEntropyLoss()
        self.consistency_criterion = nn.MSELoss()

        if self.hparams.baseline:
            assert self.hparams.p == 1, "Only labeled data is used for baseline (p=1)"

        # Consistency augmentations
        self.gaussian_noise = GaussianNoise()
        self.random_crop = RandomTranslation()

        self.accuracy_metric = Accuracy()

    def forward(self, x):

        # plt.figure()
        # plt.title("Before")
        # plt.imshow(x[0].squeeze().permute(1, 2, 0))

        if self.training:  # and not self.hparams.baseline:
            x = self.random_crop(x)
            x = self.gaussian_noise(x)

        # plt.figure()
        # plt.title("After")
        # plt.imshow(x[0].squeeze().permute(1, 2, 0))
        # plt.show()

        return self.network(x)

    def supervised_training_step(self, batch, *args) -> Dict:
        x, y = batch

        z = self.forward(x)

        supervised_loss = self.criterion(z, y)

        self.accuracy_metric.update(z, y)
        accuracy = self.accuracy_metric.calculate_result()

        logs = {'cross_entropy_loss': supervised_loss,
                'accuracy': accuracy}

        if not self.hparams.baseline:
            z_hat = self.forward(x)
            unsupervised_loss = self.consistency_criterion(z, z_hat)

            unsupervised_weight = self.max_unsupervised_weight * self.rampup_value()

            loss = supervised_loss + unsupervised_weight * unsupervised_loss

            logs.update({'supervised_consistency_loss': unsupervised_loss,
                         'unsupervised_weight': unsupervised_weight,
                         })

        else:
            loss = supervised_loss

        logs.update({'supervised_loss': loss,
                     'rampup_value': self.rampup_value(),
                     'learning_rate': self.rampup_value() * self.hparams.lr})

        return {'loss': loss, "progress_bar": logs, 'log': logs}

    def unsupervised_training_step(self, batch, *args) -> Dict:
        x = batch

        z = self.forward(x)
        z_hat = self.forward(x)

        unsupervised_loss = self.consistency_criterion(z, z_hat)

        unsupervised_weight = self.max_unsupervised_weight * self.rampup_value()

        loss = unsupervised_weight * unsupervised_loss

        logs = {'unsupervised_consistency_loss': unsupervised_loss,
                'unsupervised_loss': loss}

        return {'loss': loss, 'log': logs, "progress_bar": logs}

    def rampup_value(self):
        if self.current_epoch <= self.hparams.rampup_stop - 1:
            T = (1 / (self.hparams.rampup_stop - 1)) * self.current_epoch
            return np.exp(-5 * (1 - T) ** 2)
        else:
            return 1

    def rampdown_value(self):
        if self.current_epoch >= self.epoch - self.hparams.rampup_stop - 1:
            T = (1 / (self.epoch - self.hparams.rampup_stop - 1)) * self.current_epoch
            return np.exp(-12.5 * T ** 2)
        else:
            return 0

    def optimizer_step(self, epoch_nb, batch_nb, optimizer, optimizer_i, opt_closure, **kwargs):
        if self.current_epoch < self.hparams.rampup_stop:
            lr_scale = self.rampup_value()
            for pg in optimizer.param_groups:
                pg['lr'] = lr_scale * self.hparams.lr
        elif self.current_epoch > self.hparams.epochs - self.hparams.rampdown_start:
            pass

        optimizer.step()
        optimizer.zero_grad()

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.lr, betas=(0.9, 0.999), weight_decay=1e-4)

    def test_val_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)

        # calculate loss
        loss_val = self.criterion(y_hat, y)
        self.accuracy_metric.update(y_hat, y)
        accuracy = self.accuracy_metric.calculate_result()

        tqdm_dict = {'val_loss': loss_val, 'accuracy': accuracy}
        output = OrderedDict[{
            'loss': loss_val,
            'progress_bar': tqdm_dict,
            'log': tqdm_dict
        }]
        return output

    def validation_step(self, *args, **kwargs) -> Dict[str, Tensor]:
        return self.test_val_step(*args, **kwargs)

    def test_step(self, *args, **kwargs) -> Dict[str, Tensor]:
        return self.test_val_step(*args, **kwargs)


    def val_dataloader(self):
        ds = CIFAR10(root=self.hparams.data_root, train=False, transform=self.test_transform, download=True)
        return DataLoader(ds, self.hparams.batch_size, shuffle=False)

    def test_dataloader(self):
        ds = CIFAR10(root=self.hparams.data_root, train=False, transform=self.test_transform, download=True)
        return DataLoader(ds, self.hparams.batch_size, shuffle=False)

    def epoch_end(self, outputs):
        out = {}
        if len(outputs) > 0:
            out = {key: torch.stack([x[key] for x in outputs]).mean() for key in outputs[0].keys() if isinstance(key, torch.Tensor)}
        return out

    def training_epoch_end(self, outputs):
        return self.epoch_end(outputs)

    def validation_epoch_end(self, outputs):
        return self.epoch_end(outputs)

    def test_epoch_end(self, outputs):
        return self.epoch_end(outputs)

    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Add model specific arguments to argparser.

        Args:
            parent_parser (argparse.ArgumentParser): parent parser to which to add arguments

        Returns:
            argparser with added arguments
        """
        parser = super(PIModel, PIModel).add_model_specific_args(parent_parser)
        parser.add_argument('--baseline', action='store_true')
        parser.add_argument('--rampup_stop', default=80)
        parser.add_argument('--rampdown_start', default=50, help='Number of epochs before the end to start rampdown')
        parser.add_argument('--epochs', default=300, type=int)
        parser.add_argument('--batch-size', default=100, type=int, help='batch size', dest='batch_size')
        parser.add_argument('--lr', default=0.003, type=float, help='Max learning rate', dest='lr')
        parser.add_argument('--w_max', default=100, type=float, help='Maximum unsupervised weight, default=100 for '
                                                                     'CIFAR10 as described in paper')
        return parser


if __name__ == '__main__':
    from pytorch_lightning import Trainer
    from argparse import ArgumentParser

    args = ArgumentParser(add_help=False)
    args.add_argument('--data-root', default='/tmp', type=str, help='Where to download the data')
    args.add_argument('--gpus', default=0, type=int)
    args = PIModel.add_model_specific_args(args)
    params = args.parse_args()

    active_set = ActiveLearningDataset(
        CIFAR10(params.data_root, train=True, transform=PIModel.train_transform, download=True),
        pool_specifics={'transform': PIModel.test_transform},
        make_unlabelled=lambda x: x[0])
    active_set.label_randomly(5000)

    print("Active set length: {}".format(len(active_set)))
    print("Pool set length: {}".format(len(active_set.pool)))

    net = vgg16(pretrained=False, num_classes=10)

    system = PIModel(network=net, train_set=active_set, hparams=params)

    trainer = Trainer(num_sanity_val_steps=0, max_epochs=params.epochs, profiler=True, early_stop_callback=False,
                      gpus=params.gpus)

    trainer.fit(system)

    trainer.test(ckpt_path='best')