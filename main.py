#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6
import sys

import matplotlib
import matplotlib.pyplot as plt
import copy
import numpy as np
from torchvision import datasets, transforms
import torch
import os

from utils.sampling import mnist_iid, mnist_noniid, cifar_iid, cifar_noniid
from utils.options import args_parser
from models.Update import LocalUpdate
from models.Nets import MLP, CNNMnist, CNNCifar, CNNFemnist, CharLSTM, MNIST_CNN_Net
from models.Fed import FedAvg
from models.test import test_img
from utils.dataset import FEMNIST, ShakeSpeare
from utils.Functions import *

matplotlib.use('Agg')

if __name__ == '__main__':
    # parse args
    torch.manual_seed(123)
    torch.cuda.manual_seed_all(123)
    torch.cuda.manual_seed(123)
    np.random.seed(123)

    args = args_parser()
    args.device = torch.device('cuda:{}'.format(args.gpu) if torch.cuda.is_available() and args.gpu != -1 else 'cpu')

    # load dataset and split users
    if args.dataset == 'mnist':
        trans_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
        dataset_train = datasets.MNIST('./data/mnist/', train=True, download=True, transform=trans_mnist)
        dataset_test = datasets.MNIST('./data/mnist/', train=False, download=True, transform=trans_mnist)
        args.num_channels = 1
        # sample users
        if args.iid:
            dict_users = mnist_iid(dataset_train, args.num_users)
        else:
            dict_users = mnist_noniid(dataset_train, args.num_users)
    elif args.dataset == 'cifar':
        # trans_cifar = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5,
        # 0.5))])
        args.num_channels = 3
        trans_cifar_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
        trans_cifar_test = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
        dataset_train = datasets.CIFAR10('./data/cifar', train=True, download=True, transform=trans_cifar_train)
        dataset_test = datasets.CIFAR10('./data/cifar', train=False, download=True, transform=trans_cifar_test)
        if args.iid:
            dict_users = cifar_iid(dataset_train, args.num_users)
        else:
            dict_users = cifar_noniid(dataset_train, args.num_users)
    elif args.dataset == 'fashion-mnist':
        args.num_channels = 1
        trans_fashion_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.5,), (0.5,))])
        dataset_train = datasets.FashionMNIST('./data/fashion-mnist', train=True, download=True,
                                              transform=trans_fashion_mnist)
        dataset_test = datasets.FashionMNIST('./data/fashion-mnist', train=False, download=True,
                                             transform=trans_fashion_mnist)
        if args.iid:
            dict_users = mnist_iid(dataset_train, args.num_users)
        else:
            dict_users = mnist_noniid(dataset_train, args.num_users)
    elif args.dataset == 'femnist':
        args.num_channels = 1
        dataset_train = FEMNIST(train=True)
        dataset_test = FEMNIST(train=False)
        dict_users = dataset_train.get_client_dic()
        args.num_users = len(dict_users)
        if args.iid:
            exit('Error: femnist dataset is naturally non-iid')
        else:
            print("Warning: The femnist dataset is naturally non-iid, you do not need to specify iid or non-iid")
    elif args.dataset == 'shakespeare':
        dataset_train = ShakeSpeare(train=True)
        dataset_test = ShakeSpeare(train=False)
        dict_users = dataset_train.get_client_dic()
        args.num_users = len(dict_users)
        if args.iid:
            exit('Error: ShakeSpeare dataset is naturally non-iid')
        else:
            print("Warning: The ShakeSpeare dataset is naturally non-iid, you do not need to specify iid or non-iid")
    else:
        sys.exit('Error: unrecognized dataset')
    img_size = dataset_train[0][0].shape

    # build model
    if args.model == 'cnn' and args.dataset == 'cifar':
        net_glob = CNNCifar(args=args).to(args.device)
    elif args.model == 'cnn' and (args.dataset == 'mnist' or args.dataset == 'fashion-mnist'):
        # net_glob = CNNMnist(args=args).to(args.device)
        net_glob = MNIST_CNN_Net().to(args.device)
    elif args.dataset == 'femnist' and args.model == 'cnn':
        net_glob = CNNFemnist(args=args).to(args.device)
    elif args.dataset == 'shakespeare' and args.model == 'lstm':
        net_glob = CharLSTM().to(args.device)
    elif args.model == 'mlp':
        len_in = 1
        for x in img_size:
            len_in *= x
        net_glob = MLP(dim_in=len_in, dim_hidden=64, dim_out=args.num_classes).to(args.device)
    else:
        sys.exit('Error: unrecognized model')

    dp_epsilon = args.dp_epsilon / (args.frac * args.epochs)
    dp_delta = args.dp_delta
    dp_mechanism = args.dp_mechanism
    dp_clip = args.dp_clip

    print(net_glob)
    net_glob.train()

    # copy weights
    w_glob = net_glob.state_dict()
    all_clients = list(range(args.num_users))
    prepareTime = getPrepareTime(args.num_users)  # FedSA
    currentLostTime = copy.deepcopy(prepareTime)

    # training
    # acc_test = []
    acc_test = {}
    learning_rate = [args.lr for i in range(args.num_users)]
    runtime = 0

    # for iter in range(args.epochs):
    rnd = 1
    while rnd < 1000:
        w_locals, loss_locals = [], []
        m = max(int(args.frac * args.num_users), 1)
        # idxs_users = np.random.choice(range(args.num_users), m, replace=False)  # 修改为FedSA的半异步选择客户方法
        if args.sync:  # 同步
            chosenClients, currentLostTime, iterationTime = chooseClientsSync(args.num_users,
                                                                              m, prepareTime, currentLostTime)
        else:     # 半异步
            chosenClients, currentLostTime, iterationTime = chooseClientsSemiAsync(m, prepareTime, currentLostTime)

        runtime += iterationTime
        # chosenClients = addStaleClients(tau)

        # 本地训练
        for idx in chosenClients:
            args.lr = learning_rate[idx]
            local = LocalUpdate(args=args, dataset=dataset_train, idxs=dict_users[idx],
                                dp_epsilon=dp_epsilon, dp_delta=dp_delta,
                                dp_mechanism=dp_mechanism, dp_clip=dp_clip)
            w, loss, curLR = local.train(net=copy.deepcopy(net_glob).to(args.device))
            learning_rate[idx] = curLR
            w_locals.append(copy.deepcopy(w))
            loss_locals.append(copy.deepcopy(loss))

        w_glob = FedAvg(w_locals)   # update global weights
        net_glob.load_state_dict(w_glob)    # copy weight to net_glob

        # print accuracy
        net_glob.eval()
        acc_t, loss_t = test_img(net_glob, dataset_test, args)
        print("Round {:3d},Testing accuracy: {:.2f}".format(rnd, acc_t))
        rnd += 1
        acc_test[runtime] = acc_t.item()

    rootpath = './log'
    if not os.path.exists(rootpath):
        os.makedirs(rootpath)
    accfile = open(rootpath + '/' + str(args.sync) + '_accfile_fed_{}_{}_{}_iid{}_dp_{}_epsilon_{}_{}.txt'.
                   format(args.dataset, args.model, args.epochs, args.iid,
                          args.dp_mechanism, args.dp_epsilon, args.frac), "w")

    for it in acc_test.items():
        sac = str(it)
        accfile.write(sac)
        accfile.write('\n')
    accfile.close()

    # plot loss curve
    plt.figure()
    plt.plot(acc_test.keys(), acc_test.values())
    plt.ylabel('test accuracy')
    plt.savefig(rootpath + '/' + str(args.sync) + '_fed_{}_{}_{}_C{}_iid{}_dp_{}_epsilon_{}_{}_acc.png'.format(
        args.dataset, args.model, args.epochs, args.frac, args.iid, args.dp_mechanism, args.dp_epsilon, args.frac))
