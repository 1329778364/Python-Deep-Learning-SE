import gzip
import os
import pickle
import random

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torchsummary import summary

from ch10.behavioral_cloning.util import available_actions, data_transform

BATCH_SIZE = 32
DATA_DIR = 'data'
DATA_FILE = 'data.gzip'
MODEL_FILE = 'model.pt'
EPOCHS = 30
TRAIN_VAL_SPLIT = 0.85

MULTIPLY_RARE_EVENTS = 30


def read_data():
    """Read the data generated by keyboard_agent.py"""
    with gzip.open(os.path.join(DATA_DIR, DATA_FILE), 'rb') as f:
        data = pickle.load(f)

    # balance dataset by multiplying
    # brake, right+brake, left+brake events
    # since they are too few
    if MULTIPLY_RARE_EVENTS > 1:
        data_copy = data.copy()
        for d in data:
            for a in ([[-1, 0, 1], [1, 0, 1], [0, 0, 1]]):
                if np.array_equal(d[1], a):
                    data_copy += (d,) * MULTIPLY_RARE_EVENTS

        data = data_copy

    random.shuffle(data)

    # to numpy arrays
    states, actions, _, _, _ = map(np.array, zip(*data))

    # reverse one-hot, actions to classes
    act_classes = np.full((len(actions)), -1, dtype=np.int)
    for i, a in enumerate(available_actions):
        act_classes[np.all(actions == a, axis=1)] = i

    # drop non-actions
    states = np.array(states)
    states = states[act_classes != -1]

    # drop some of the acceleration actions to balance the dataset
    act_classes = act_classes[act_classes != -1]
    non_accel = act_classes != available_actions.index([0, 1, 0])
    drop_mask = np.random.rand(act_classes[~non_accel].size) > 0.7
    non_accel[~non_accel] = drop_mask
    states = states[non_accel]
    act_classes = act_classes[non_accel]

    for i, a in enumerate(available_actions):
        print("Actions of type {}: {}"
              .format(str(a), str(act_classes[act_classes == i].size)))

    print("Total transitions: " + str(len(act_classes)))

    return states, act_classes


read_data()


def create_datasets():
    """Create training and validation datasets"""

    class TensorDatasetTransforms(torch.utils.data.TensorDataset):
        """
        Helper class to allow transformations
        by default TensorDataset doesn't support them
        """

        def __init__(self, x, y):
            super().__init__(x, y)

        def __getitem__(self, index):
            tensor = data_transform(self.tensors[0][index])
            return (tensor,) + tuple(t[index] for t in self.tensors[1:])

    x, y = read_data()
    x = np.moveaxis(x, 3, 1)  # channel first (torch requirement)

    # train dataset
    x_train = x[:int(len(x) * TRAIN_VAL_SPLIT)]
    y_train = y[:int(len(y) * TRAIN_VAL_SPLIT)]

    train_set = TensorDatasetTransforms(
        torch.tensor(x_train),
        torch.tensor(y_train))

    train_loader = torch.utils.data.DataLoader(train_set,
                                               batch_size=BATCH_SIZE,
                                               shuffle=True,
                                               num_workers=2)

    # test dataset
    x_test, y_test = x[int(len(x_train)):], y[int(len(y_train)):]

    test_set = TensorDatasetTransforms(
        torch.tensor(x_test),
        torch.tensor(y_test))

    val_order = torch.utils.data.DataLoader(test_set,
                                            batch_size=BATCH_SIZE,
                                            shuffle=False,
                                            num_workers=2)

    return train_loader, val_order


def build_network():
    """Build the torch network"""

    class Flatten(nn.Module):
        """
        Helper class to flatten the tensor
        between the last conv and first fc layer
        """

        def forward(self, x):
            return x.view(x.size()[0], -1)

    # Same network as with the DQN example
    model = torch.nn.Sequential(
        torch.nn.Conv2d(1, 32, 8, 4),
        torch.nn.BatchNorm2d(32),
        torch.nn.ELU(),
        torch.nn.Dropout2d(0.5),
        torch.nn.Conv2d(32, 64, 4, 2),
        torch.nn.BatchNorm2d(64),
        torch.nn.ELU(),
        torch.nn.Dropout2d(0.5),
        torch.nn.Conv2d(64, 64, 3, 1),
        torch.nn.ELU(),
        Flatten(),
        torch.nn.BatchNorm1d(64 * 7 * 7),
        torch.nn.Dropout(),
        torch.nn.Linear(64 * 7 * 7, 120),
        torch.nn.ELU(),
        torch.nn.BatchNorm1d(120),
        torch.nn.Dropout(),
        torch.nn.Linear(120, len(available_actions)),
    )

    return model


def train_epoch(model, device, loss_function, optimizer, data_loader):
    """Train for a single epoch"""

    # set model to training mode
    model.train()

    current_loss = 0.0
    current_acc = 0

    # iterate over the training data
    for i, (inputs, labels) in enumerate(data_loader):
        # send the input/labels to the GPU
        inputs = inputs.to(device)
        labels = labels.to(device)

        # zero the parameter gradients
        optimizer.zero_grad()

        with torch.set_grad_enabled(True):
            # forward
            outputs = model(inputs)
            _, predictions = torch.max(outputs, 1)
            loss = loss_function(outputs, labels)

            # backward
            loss.backward()
            optimizer.step()

        # statistics
        current_loss += loss.item() * inputs.size(0)
        current_acc += torch.sum(predictions == labels.data)

    total_loss = current_loss / len(data_loader.dataset)
    total_acc = current_acc.double() / len(data_loader.dataset)

    print('Train Loss: {:.4f}; Accuracy: {:.4f}'.format(total_loss, total_acc))


def test(model, device, loss_function, data_loader):
    """Test over the whole dataset"""

    model.eval()  # set model in evaluation mode

    current_loss = 0.0
    current_acc = 0

    # iterate over the validation data
    for i, (inputs, labels) in enumerate(data_loader):
        # send the input/labels to the GPU
        inputs = inputs.to(device)
        labels = labels.to(device)

        # forward
        with torch.set_grad_enabled(False):
            outputs = model(inputs)
            _, predictions = torch.max(outputs, 1)
            loss = loss_function(outputs, labels)

        # statistics
        current_loss += loss.item() * inputs.size(0)
        current_acc += torch.sum(predictions == labels.data)

    total_loss = current_loss / len(data_loader.dataset)
    total_acc = current_acc.double() / len(data_loader.dataset)

    print('Test Loss: {:.4f}; Accuracy: {:.4f}'.format(total_loss, total_acc))


def train(model, device):
    """
    Training main method
    """

    summary(model, (1, 84, 84))

    loss_function = nn.CrossEntropyLoss()

    optimizer = optim.Adam(model.parameters())

    train_loader, val_order = create_datasets()  # read datasets

    # train
    for epoch in range(EPOCHS):
        print('Epoch {}/{}'.format(epoch + 1, EPOCHS))

        train_epoch(model, device, loss_function, optimizer, train_loader)
        test(model, device, loss_function, val_order)

        # save model
        model_path = os.path.join(DATA_DIR, MODEL_FILE)
        torch.save(model.state_dict(), model_path)
