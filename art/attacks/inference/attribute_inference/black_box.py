# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2020
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements attribute inference attacks.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import logging
from typing import Optional, Union, Tuple, List, TYPE_CHECKING

import numpy as np
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.preprocessing import minmax_scale, OneHotEncoder, OrdinalEncoder
from sklearn.compose import ColumnTransformer

from art.estimators.estimator import BaseEstimator
from art.estimators.classification.classifier import ClassifierMixin
from art.attacks.attack import AttributeInferenceAttack
from art.estimators.regression import RegressorMixin
from art.utils import (
    check_and_transform_label_format,
    float_to_categorical,
    floats_to_one_hot,
    get_feature_values,
    remove_attacked_feature,
)

if TYPE_CHECKING:
    from art.utils import CLASSIFIER_TYPE, REGRESSOR_TYPE

logger = logging.getLogger(__name__)


class AttributeInferenceBlackBox(AttributeInferenceAttack):
    """
    Implementation of a simple black-box attribute inference attack.

    The idea is to train a simple neural network to learn the attacked feature from the rest of the features and the
    model's predictions. Assumes the availability of the attacked model's predictions for the samples under attack,
    in addition to the rest of the feature values. If this is not available, the true class label of the samples may be
    used as a proxy.
    """

    attack_params = AttributeInferenceAttack.attack_params + [
        "prediction_normal_factor",
        "scale_range",
        "attack_model_type",
        "is_continuous",
        "non_numerical_features",
        "encoder",
    ]
    _estimator_requirements = (BaseEstimator, (ClassifierMixin, RegressorMixin))

    def __init__(
        self,
        estimator: Union["CLASSIFIER_TYPE", "REGRESSOR_TYPE"],
        attack_model_type: str = "nn",
        attack_model: Optional[Union["CLASSIFIER_TYPE", "REGRESSOR_TYPE"]] = None,
        attack_feature: Union[int, slice] = 0,
        is_continuous: Optional[bool] = False,
        scale_range: Optional[Tuple[float, float]] = None,
        prediction_normal_factor: Optional[float] = 1,
        non_numerical_features: Optional[List[int]] = None,
        encoder: Optional[Union[OrdinalEncoder, OneHotEncoder, ColumnTransformer]] = None,
    ):
        """
        Create an AttributeInferenceBlackBox attack instance.

        :param estimator: Target estimator.
        :param attack_model_type: the type of default attack model to train, optional. Should be one of `nn` (for neural
                                  network, default) or `rf` (for random forest). If `attack_model` is supplied, this
                                  option will be ignored.
        :param attack_model: The attack model to train, optional. If the attacked feature is continuous, this should
                             be a regression model, and if the attacked feature is categorical it should be a
                             classifier.If none is provided, a default model will be created.
        :param attack_feature: The index of the feature to be attacked or a slice representing multiple indexes in
                               case of a one-hot encoded feature.
        :param is_continuous: Whether the attacked feature is continuous. Default is False (which means categorical).
        :param scale_range: If supplied, the class labels (both true and predicted) will be scaled to the given range.
                            Only applicable when `estimator` is a regressor.
        :param prediction_normal_factor: If supplied, the class labels (both true and predicted) are multiplied by the
                                         factor when used as inputs to the attack-model. Only applicable when
                                         `estimator` is a regressor and if `scale_range` is not supplied
        :param non_numerical_features: a list of feature indexes that require encoding in order to feed into an ML model
                                       (i.e., strings), not including the attacked feature. Should only be supplied if
                                       non-numeric features exist in the input data not including the attacked feature,
                                       and an encoder is not supplied.
        :param encoder: An already fit encoder that can be applied to the model's input features without the attacked
                        feature (i.e., should be fit for n-1 features).
        """
        super().__init__(estimator=estimator, attack_feature=attack_feature)

        self._values: Optional[list] = None
        self._attack_model_type = attack_model_type
        self._encoder = encoder
        self._non_numerical_features = non_numerical_features
        self._is_continuous = is_continuous
        self.prediction_normal_factor = prediction_normal_factor
        self.scale_range = scale_range

        self._check_params()

        if attack_model:
            if self._is_continuous:
                if RegressorMixin not in type(attack_model).__mro__:
                    raise ValueError("When attacking a continuous feature the attack model must be of type Regressor.")
            elif ClassifierMixin not in type(attack_model).__mro__:
                raise ValueError("When attacking a categorical feature the attack model must be of type Classifier.")
            self.attack_model = attack_model
            self._attack_model_type = None
        elif attack_model_type == "nn":
            import torch
            from torch import nn

            self.epochs = 100
            self.batch_size = 100
            self.learning_rate = 0.0001

            if self._is_continuous:
                class MembershipInferenceAttackModel(nn.Module):
                    """
                    Implementation of a pytorch model for learning a membership inference attack.

                    The features used are probabilities/logits or losses for the attack training data along with
                    its true labels.
                    """

                    def __init__(self, num_features):

                        self.num_features = num_features

                        super().__init__()

                        self.features = nn.Sequential(
                            nn.Linear(self.num_features, 100),
                            nn.ReLU(),
                            nn.Linear(100, 64),
                            nn.ReLU(),
                            nn.Linear(64, 1),
                        )

                    def forward(self, x):
                        """Forward the model."""
                        return self.features(x)
                self._attack_class = MembershipInferenceAttackModel
            else:
                class MembershipInferenceAttackModel(nn.Module):
                    """
                    Implementation of a pytorch model for learning an attribute inference attack.

                    The features used are the remaining n-1 features of the attack training data along with
                    the model's predictions.
                    """

                    def __init__(self, num_classes, num_features=None):

                        self.num_classes = num_classes
                        if num_features:
                            self.num_features = num_features
                        else:
                            self.num_features = num_classes

                        super().__init__()

                        self.features = nn.Sequential(
                            nn.Linear(self.num_features, 512),
                            nn.ReLU(),
                            nn.Linear(512, 100),
                            nn.ReLU(),
                            nn.Linear(100, 64),
                            nn.ReLU(),
                            nn.Linear(64, num_classes),
                        )

                        self.output = nn.Softmax()

                    def forward(self, x):
                        """Forward the model."""
                        out = self.features(x)
                        return self.output(out)
            self._attack_class = MembershipInferenceAttackModel
        elif attack_model_type == "rf":
            if self._is_continuous:
                self.attack_model = RandomForestRegressor()
            else:
                self.attack_model = RandomForestClassifier()
        else:
            raise ValueError("Illegal value for parameter `attack_model_type`.")

        remove_attacked_feature(self.attack_feature, self._non_numerical_features)

    def fit(self, x: np.ndarray, y: Optional[np.ndarray] = None) -> None:
        """
        Train the attack model.

        :param x: Input to training process. Includes all features used to train the original model.
        :param y: True labels for x.
        """

        # Checks:
        if self.estimator.input_shape is not None:
            if self.estimator.input_shape[0] != x.shape[1]:
                raise ValueError("Shape of x does not match input_shape of model")
        if isinstance(self.attack_feature, int) and self.attack_feature >= x.shape[1]:
            raise ValueError("`attack_feature` must be a valid index to a feature in x")

        # get model's predictions for x
        if ClassifierMixin in type(self.estimator).__mro__:
            predictions = np.array([np.argmax(arr) for arr in self.estimator.predict(x)]).reshape(-1, 1)
            if y is not None:
                y = check_and_transform_label_format(y, nb_classes=self.estimator.nb_classes, return_one_hot=True)
        else:  # Regression model
            if self.scale_range is not None:
                predictions = minmax_scale(self.estimator.predict(x).reshape(-1, 1), feature_range=self.scale_range)
                if y is not None:
                    y = minmax_scale(y, feature_range=self.scale_range)
            else:
                predictions = self.estimator.predict(x).reshape(-1, 1) * self.prediction_normal_factor
                if y is not None:
                    y = y * self.prediction_normal_factor
            if y is not None:
                y = y.reshape(-1, 1)

        # get vector of attacked feature
        y_attack = x[:, self.attack_feature]
        y_attack_ready = y_attack
        if not self._is_continuous:
            self._values = get_feature_values(y_attack, isinstance(self.attack_feature, int))
            nb_classes = len(self._values)
            if isinstance(self.attack_feature, int):
                y_one_hot = float_to_categorical(y_attack)
            else:
                y_one_hot = floats_to_one_hot(y_attack)
            y_attack_ready = check_and_transform_label_format(y_one_hot, nb_classes=nb_classes, return_one_hot=True)

        # create training set for attack model
        x_train = np.delete(x, self.attack_feature, 1)

        if self._non_numerical_features and self._encoder is None:
            if isinstance(self.attack_feature, int):
                compare_index = self.attack_feature
                size = 1
            else:
                compare_index = self.attack_feature.start
                size = (self.attack_feature.stop - self.attack_feature.start) // self.attack_feature.step
            new_indexes = [(f - size) if f > compare_index else f for f in self._non_numerical_features]
            categorical_transformer = OrdinalEncoder()
            self._encoder = ColumnTransformer(
                transformers=[
                    ("cat", categorical_transformer, new_indexes),
                ],
                remainder="passthrough",
            )
            self._encoder.fit(x_train)

        if self._encoder is not None:
            x_train = self._encoder.transform(x_train)
        x_train = np.concatenate((x_train, predictions), axis=1).astype(np.float32)

        if y is not None:
            x_train = np.concatenate((x_train, y), axis=1)

        # train attack model
        if self._attack_model_type == 'nn':
            if not self._is_continuous:
                self.attack_model = self._attack_class(len(self._values), x_train.shape[1])
            else:
                self.attack_model = self._attack_class(x_train.shape[1])

            import torch
            from torch import nn
            from torch import optim
            from torch.utils.data import DataLoader
            from art.utils import to_cuda

            if self._is_continuous:
                loss_fn = nn.MSELoss()
            else:
                loss_fn = nn.CrossEntropyLoss()
            optimizer = optim.Adam(self.attack_model.parameters(), lr=self.learning_rate)  # type: ignore

            attack_train_set = self._get_attack_dataset(f=x_train, label=y_attack_ready)
            train_loader = DataLoader(attack_train_set, batch_size=self.batch_size, shuffle=True, num_workers=0)

            self.attack_model = to_cuda(self.attack_model)  # type: ignore
            self.attack_model.train()  # type: ignore

            for _ in range(self.epochs):
                for (input1, targets) in train_loader:
                    input1, targets = to_cuda(input1), to_cuda(targets)
                    _, targets = torch.autograd.Variable(input1), torch.autograd.Variable(targets)

                    optimizer.zero_grad()
                    outputs = self.attack_model(input1)  # type: ignore
                    loss = loss_fn(outputs, targets)
                    print('MSE loss: ', loss)
                    loss.backward()
                    optimizer.step()
        else:
            self.attack_model.fit(x_train, y_attack_ready)

    def infer(self, x: np.ndarray, y: Optional[np.ndarray] = None, **kwargs) -> np.ndarray:
        """
        Infer the attacked feature.

        :param x: Input to attack. Includes all features except the attacked feature.
        :param y: True labels for x.
        :param pred: Original model's predictions for x.
        :type pred: `np.ndarray`
        :param values: Possible values for attacked feature. For a single column feature this should be a simple list
                       containing all possible values, in increasing order (the smallest value in the 0 index and so
                       on). For a multi-column feature (for example 1-hot encoded and then scaled), this should be a
                       list of lists, where each internal list represents a column (in increasing order) and the values
                       represent the possible values for that column (in increasing order). If not provided, is
                       computed from the training data when calling `fit`. Only relevant for categorical features.
        :type values: list, optional
        :return: The inferred feature values.
        """
        values: Optional[list] = kwargs.get("values")

        # if provided, override the values computed in fit()
        if values is not None:
            self._values = values

        pred: Optional[np.ndarray] = kwargs.get("pred")

        if pred is None:
            raise ValueError("Please provide param `pred` of model predictions.")

        if pred.shape[0] != x.shape[0]:
            raise ValueError("Number of rows in x and y do not match")
        if self.estimator.input_shape is not None:
            if isinstance(self.attack_feature, int) and self.estimator.input_shape[0] != x.shape[1] + 1:
                raise ValueError("Number of features in x + 1 does not match input_shape of model")

        x_test = x
        if self._encoder is not None:
            x_test = self._encoder.transform(x)

        if RegressorMixin in type(self.estimator).__mro__:
            if self.scale_range is not None:
                x_test = np.concatenate((x_test, minmax_scale(pred, feature_range=self.scale_range)), axis=1).astype(
                    np.float32
                )
                if y is not None:
                    y = minmax_scale(y, feature_range=self.scale_range)
            else:
                x_test = np.concatenate((x_test, pred * self.prediction_normal_factor), axis=1).astype(np.float32)
                if y is not None:
                    y = y * self.prediction_normal_factor
            if y is not None:
                y = y.reshape(-1, 1)
        else:
            x_test = np.concatenate((x_test, pred), axis=1).astype(np.float32)
            if y is not None:
                y = check_and_transform_label_format(y, nb_classes=self.estimator.nb_classes, return_one_hot=True)

        if y is not None:
            x_test = np.concatenate((x_test, y), axis=1)

        if self._attack_model_type == "nn":
            import torch
            from torch.utils.data import DataLoader
            from art.utils import to_cuda, from_cuda

            self.attack_model.eval()  # type: ignore
            predictions: Optional[np.ndarray] = None
            test_set = self._get_attack_dataset(f=x_test)
            test_loader = DataLoader(test_set, batch_size=self.batch_size, shuffle=False, num_workers=0)
            for input1, _ in test_loader:
                input1 = to_cuda(input1)
                outputs = self.attack_model(input1)  # type: ignore
                predicted = from_cuda(outputs)

                if predictions is None:
                    predictions = predicted.detach().numpy()
                else:
                    predictions = np.vstack((predictions, predicted.detach().numpy()))
                if not self._is_continuous:
                    idx = np.argmax(predictions, axis=-1)
                    predictions = np.zeros(predictions.shape)
                    predictions[np.arange(predictions.shape[0]), idx] = 1

        else:
            predictions = self.attack_model.predict(x_test)
        predictions = predictions.astype(np.float32)

        if not self._is_continuous and self._values is not None:
            if isinstance(self.attack_feature, int):
                predictions = np.array([self._values[np.argmax(arr)] for arr in predictions])
            else:
                i = 0
                for column in predictions.T:
                    for index in range(len(self._values[i])):
                        np.place(column, [column == index], self._values[i][index])
                    i += 1
        return np.array(predictions)

    def _get_attack_dataset(self, f, label=None):
        from torch.utils.data.dataset import Dataset

        class AttackDataset(Dataset):
            """
            Implementation of a pytorch dataset for membership inference attack.

            The features are probabilities/logits or losses for the attack training data (`x_1`) along with
            its true labels (`x_2`). The labels (`y`) are a boolean representing whether this is a member.
            """

            def __init__(self, x, y=None):
                import torch

                self.x = torch.from_numpy(x.astype(np.float64)).type(torch.FloatTensor)

                if y is not None:
                    self.y = torch.from_numpy(y.astype(np.float32)).type(torch.FloatTensor)
                else:
                    self.y = torch.zeros(x.shape[0])

            def __len__(self):
                return len(self.x)

            def __getitem__(self, idx):
                if idx >= len(self.x):  # pragma: no cover
                    raise IndexError("Invalid Index")

                return self.x[idx], self.y[idx]

        return AttackDataset(x=f, y=label)

    def _check_params(self) -> None:

        super()._check_params()

        if not isinstance(self._is_continuous, bool):
            raise ValueError("is_continuous must be a boolean.")

        if self._attack_model_type not in ["nn", "rf"]:
            raise ValueError("Illegal value for parameter `attack_model_type`.")

        if RegressorMixin not in type(self.estimator).__mro__:
            if self.prediction_normal_factor != 1:
                raise ValueError("Prediction normal factor is only applicable to regressor models.")

        if self._non_numerical_features and (
            (not isinstance(self._non_numerical_features, list))
            or (not all(isinstance(item, int) for item in self._non_numerical_features))
        ):
            raise ValueError("non_numerical_features must be a list of int.")

        if self._encoder is not None and (
            not isinstance(self._encoder, OrdinalEncoder)
            and not isinstance(self._encoder, OneHotEncoder)
            and not isinstance(self._encoder, ColumnTransformer)
        ):
            raise ValueError("encoder must be a OneHotEncoder, OrdinalEncoder or ColumnTransformer object.")
