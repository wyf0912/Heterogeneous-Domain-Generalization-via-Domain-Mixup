import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import accuracy_score
import torchvision.models as models
from collections import OrderedDict
import math
import sys

dist = sys.argv and any(['--local_rank' in x for x in sys.argv])


def select_GPUs(N_per_process, max_utilization=.5, max_memory_usage=.5):
    '''
    select `N_per_process` GPUs.
    If distributed training is enabled, GPUs will be assigned properly among different processes.
    Arguments:
        N_per_process (int): How many GPUs you want to select for each process
        max_utilization (float): GPU with utilization higher than `max_utilization` is considered as not available.
        max_memory_usage (float): GPU with memory usage higher than `max_memory_usage` is considered as not available.

    Returns:
        list containing IDs of selected GPUs
    '''
    if not dist:
        return get_available_GPUs(N_per_process, max_utilization, max_memory_usage)
    try:
        rank = torch.distributed.get_rank()
    except Exception as e:
        print('please call torch.distributed.init_process_group first')
        raise e
    world_size = torch.distributed.get_world_size()
    tensor = torch.zeros(world_size * N_per_process, dtype=torch.int).cuda()
    if rank == 0:
        device_ids = get_available_GPUs(world_size * N_per_process)
        tensor = torch.tensor(device_ids, dtype=torch.int).cuda()
    torch.distributed.broadcast(tensor, 0)
    ids = list(tensor.cpu().numpy())
    return ids[N_per_process * rank: N_per_process * rank + N_per_process]


def get_available_GPUs(N, max_utilization=.5, max_memory_usage=.5):
    '''
    get `N` available GPU ids with *utilization* less than `max_utilization` and *memory usage* less than max_memory_usage
    Arguments:
        N (int): How many GPUs you want to select
        max_utilization (float): GPU with utilization higher than `max_utilization` is considered as not available.
        max_memory_usage (float): GPU with memory usage higher than `max_memory_usage` is considered as not available.

    Returns:
        list containing IDs of available GPUs
    '''
    from subprocess import Popen, PIPE
    cmd = ["nvidia-smi",
           "--query-gpu=index,utilization.gpu,memory.total,memory.used",
           "--format=csv,noheader,nounits"]
    p = Popen(cmd, stdout=PIPE)
    output = p.stdout.read().decode('UTF-8')
    gpus = [[int(x) for x in line.split(',')] for line in output.splitlines()]
    gpu_ids = []
    for (index, utilization, total, used) in gpus:
        if utilization / 100.0 < max_utilization:
            if used * 1.0 / total < max_memory_usage:
                gpu_ids.append(index)
    if len(gpu_ids) < N:
        raise Exception("Only %s GPU(s) available but %s GPU(s) are required!" % (len(gpu_ids), N))
    available = gpu_ids[:N]
    return list(available)


def feature_extractor(output_channel):
    resnet18 = models.resnet18(pretrained=True)
    # models.resnet18(pretrained=True)    # pre-trained model under ImageNet
    resnet18.avgpool = nn.AvgPool2d(3, 1)  # for input size is 72*72
    # resnet18.avgpool = nn.AvgPool2d(1, 1)   #for input size is 32*32
    num_ftrs = resnet18.fc.in_features
    resnet18.fc = nn.Linear(num_ftrs, output_channel)
    for param in resnet18.parameters():
        param.requires_grad = True
    return resnet18.cuda()


def l1_penalty(var):
    return torch.abs(var)


def fix_nn(model, theta):
    def k_param_fn(tmp_model, name=None):
        if len(tmp_model._modules) != 0:
            for (k, v) in tmp_model._modules.items():
                if name is None:
                    k_param_fn(v, name=str(k))
                else:
                    k_param_fn(v, name=str(name + '.' + k))
        else:
            for (k, v) in tmp_model._parameters.items():
                if not isinstance(v, torch.Tensor):
                    continue
                tmp_model._parameters[k] = theta[str(name + '.' + k)]

    k_param_fn(model)
    return model


class Hot_Plug(object):
    def __init__(self, model):
        self.model = model
        self.params = OrderedDict(self.model.named_parameters())

    def update(self, lr=0.1):
        for param_name in self.params.keys():
            path = param_name.split('.')
            cursor = self.model
            for module_name in path[:-1]:
                cursor = cursor._modules[module_name]
            if lr > 0:
                cursor._parameters[path[-1]] = self.params[param_name] - lr * self.params[param_name].grad
            else:
                cursor._parameters[path[-1]] = self.params[param_name]

    def restore(self):
        self.update(lr=0)


class Critic_Network_MLP(nn.Module):
    def __init__(self, h, hh):
        super(Critic_Network_MLP, self).__init__()
        self.fc1 = nn.Linear(h, hh)
        self.fc2 = nn.Linear(hh, 1)

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = nn.functional.softplus(self.fc2(x))
        return torch.mean(x)


class Critic_Network_Flatten_FTF(nn.Module):
    def __init__(self, h, hh):
        super(Critic_Network_Flatten_FTF, self).__init__()
        self.fc1 = nn.Linear(h ** 2, hh)
        self.fc2 = nn.Linear(hh, 1)

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = nn.functional.softplus(self.fc2(x))
        return torch.mean(x)


def freeze_layer(model):
    count = 0
    para_optim = []
    for k in model.children():

        count += 1
        # 6 should be changed properly
        if count > 0:  # 6:
            for param in k.parameters():
                para_optim.append(param)
        else:
            for param in k.parameters():
                param.requires_grad = False

    # print count
    return para_optim


def classifier(class_num):
    model = nn.Sequential(
        nn.Linear(512, class_num),
    )

    def init_weights(m):
        if type(m) == nn.Linear:
            torch.nn.init.xavier_uniform(m.weight)
            m.bias.data.fill_(0.01)

    model.apply(init_weights)
    return model.cuda()


def classifier_homo(class_num):
    model = nn.Sequential(
        nn.ReLU(),
        nn.Linear(4096, class_num),
    )

    def init_weights(m):
        if type(m) == nn.Linear:
            torch.nn.init.xavier_uniform(m.weight)
            m.bias.data.fill_(0.01)

    model.apply(init_weights)
    return model.cuda()


'''
def dg_net(x, param):
    return torch.mean(F.softplus(F.linear(F.relu(F.linear(x,param[0],param[1])),param[2],param[3]))).cuda()
    # x.view(1,-1)  ---> add one or two FC layer to a scalar.
'''


def compute_accuracy(predictions, labels, label_offset):
    if label_offset:
        accuracy = accuracy_score(y_true=np.argmax(labels, axis=-1),
                                  y_pred=np.argmax(predictions, axis=-1) - label_offset)
    else:
        accuracy = accuracy_score(y_true=np.argmax(labels, axis=-1), y_pred=np.argmax(predictions, axis=-1))
    return accuracy


def cos_dist(a, b):
    a, b = torch.Tensor(a).unsqueeze(0), torch.Tensor(b).unsqueeze(0)
    eps = 1e-8
    all_norm = a.norm()
    signal = True
    if signal:
        a_norm = a / (a.norm(dim=1, keepdim=True) + eps)
        b_norm = b / (b.norm(dim=1, keepdim=True) + eps)
    else:
        a_norm = a / all_norm
        b_norm = b / all_norm
    res = torch.mm(a_norm, b_norm.transpose(0, 1))
    return 1 - res


def write_log(log, log_path):
    f = open(log_path, mode='a')
    f.write(str(log))
    f.write('\n')
    f.close()


def unfold_label(labels, classes):
    new_labels = []

    assert len(np.unique(labels)) == classes
    # minimum value of labels
    mini = np.min(labels)

    for index in range(len(labels)):
        dump = np.full(shape=[classes], fill_value=0).astype(np.int8)
        _class = int(labels[index]) - mini
        dump[_class] = 1
        new_labels.append(dump)

    return np.array(new_labels)


def shuffle_data(samples, labels):
    num = len(labels)
    shuffle_index = np.random.permutation(np.arange(num))
    shuffled_samples = samples[shuffle_index]
    shuffled_labels = labels[shuffle_index]
    return shuffled_samples, shuffled_labels


def learning_rate(init, epoch):
    optim_factor = 0
    if (epoch > 160):
        optim_factor = 3
    elif (epoch > 120):
        optim_factor = 2
    elif (epoch > 60):
        optim_factor = 1

    return init * math.pow(0.2, optim_factor)
