# -*- coding: utf-8 -*-

import argparse
import os
import sys
import datetime
import time

import torch
import torch.distributed as dist
from torch.autograd import Variable
from torch.multiprocessing import Process as TorchProcess
from torch.utils.data import DataLoader
from torchvision import datasets

from torchvision import transforms
from model_alexnet import alexnet
from partition_data import partition_dataset, select_dataset
from model_utils import MySGD, test_model

parser = argparse.ArgumentParser()
# 集群信息
parser.add_argument('--ps-ip', type=str, default='127.0.0.1')
parser.add_argument('--ps-port', type=str, default='29000')
parser.add_argument('--this-rank', type=int, default=2)
parser.add_argument('--learners', type=str, default='1-2')

# 模型与数据集
parser.add_argument('--data-dir', type=str, default='./data')
parser.add_argument('--data-name', type=str, default='cifar10')
parser.add_argument('--model', type=str, default='AlexNet')
parser.add_argument('--save-path', type=str, default='./')

# 参数信息
parser.add_argument('--epochs', type=int, default=20)

args = parser.parse_args()


# noinspection PyTypeChecker
def run(rank, workers, model, save_path, train_data, test_data):
    # 获取ps端传来的模型初始参数
    _group = [w for w in workers].append(0)
    group = dist.new_group(_group)

    for p in model.parameters():
        tmp_p = torch.zeros_like(p)
        dist.scatter(tensor=tmp_p, src=0, group=group)
        p.data = tmp_p
    print('Model recved successfully!')

    optimizer = MySGD(model.parameters(), lr=0.01, momentum=0.5)
    criterion = torch.nn.CrossEntropyLoss()
    print('Begin!')

    for epoch in range(args.epochs):
        pre_time = datetime.datetime.now()
        model.train()

        # AlexNet在指定epoch减少学习率LR
        if args.model == 'AlexNet':
            if epoch + 1 in [40, 60]:
                for param_group in optimizer.param_groups:
                    param_group['lr'] *= 0.1
                    print('LR Decreased! Now: {}'.format(param_group['lr']))

        epoch_train_loss = 0
        for batch_idx, (data, target) in enumerate(train_data):
            data, target = Variable(data), Variable(target)
            optimizer.zero_grad()
            output = model(data)
            loss = criterion(output, target)
            loss.backward()

            delta_ws = optimizer.get_delta_w()
            # 同步操作
            for idx, param in enumerate(model.parameters()):
                dist.gather(tensor=delta_ws[idx], dst=0, group=group)
                recv = torch.zeros_like(delta_ws[idx])
                dist.scatter(tensor=recv, src=0, group=group)
                param.data = recv

            epoch_train_loss += loss.data.item()
            print('Rank {}, Epoch {}, Batch {}/{}, Loss:{}'
                  .format(rank, epoch, batch_idx, len(train_data), loss.data.item()))

        end_time = datetime.datetime.now()
        h, remainder = divmod((end_time - pre_time).seconds, 3600)
        m, s = divmod(remainder, 60)
        time_str = "Time %02d:%02d:%02d" % (h, m, s)

        epoch_train_loss /= len(train_data)
        epoch_train_loss = format(epoch_train_loss, '.4f')

        # 训练结束后进行test
        test_loss, acc = test_model(rank, model, test_data, criterion=criterion)
        print('total time ' + str(time_str))
        f = open('./result_' + str(rank) + '_' + args.model + '.txt', 'a')
        f.write('Rank: ' + str(rank) +
                ', \tEpoch: ' + str(epoch + 1) +
                ', \tTrainLoss: ' + str(epoch_train_loss) +
                ', \tTestLoss: ' + str(test_loss) +
                ', \tTestAcc: ' + str(acc) +
                ', \tTime: ' + str(time_str) + '\n')
        f.close()

        if (epoch + 1) % 5 == 0:
            if not os.path.exists(save_path):
                os.makedirs(save_path)
            torch.save(model.state_dict(),
                       save_path + '/' + args.model + '_' + str(epoch + 1) + '.pkl')


def init_processes(rank, size, workers,
                   model, save_path,
                   train_dataset, test_dataset,
                   fn, backend='tcp'):
    os.environ['MASTER_ADDR'] = args.ps_ip
    os.environ['MASTER_PORT'] = args.ps_port
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(rank, workers, model, save_path, train_dataset, test_dataset)


if __name__ == '__main__':
    workers = [int(v) for v in str(args.learners).split('-')]

    transform = transforms.Compose(
        [transforms.Resize(128),
         transforms.RandomHorizontalFlip(),
         transforms.ToTensor(),
         transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))]
    )

    model = alexnet(num_classes=10)
    train_dataset = datasets.CIFAR10(args.data_dir, train=True, download=False,
                                     transform=transform)
    test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                    transform=transform)
    train_bsz = 64
    test_bsz = 128

    train_bsz /= len(workers)
    train_bsz = int(train_bsz)

    train_data = partition_dataset(train_dataset, workers)
    # test_data = partition_dataset(test_dataset, workers)

    this_rank = args.this_rank
    train_data = select_dataset(workers, this_rank, train_data, batch_size=train_bsz)
    # test_data = select_dataset(workers, this_rank, test_data, batch_size=test_bsz)

    # 用所有的测试数据测试
    test_data = DataLoader(test_dataset, batch_size=test_bsz, shuffle=True)

    world_size = len(workers) + 1

    save_path = str(args.save_path)
    save_path = save_path.rstrip('/')

    p = TorchProcess(target=init_processes, args=(this_rank, world_size, workers,
                                                  model, save_path,
                                                  train_data, test_data,
                                                  run))
    p.start()
    p.join()

