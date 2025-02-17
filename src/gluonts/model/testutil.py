# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

# Standard library imports
from typing import Iterator

# Third-party imports
import numpy as np

# First-party imports
from gluonts.core.component import validated
from gluonts.dataset.common import Dataset
from gluonts.model.estimator import Estimator
from gluonts.model.forecast import Forecast, SampleForecast
from gluonts.model.predictor import RepresentablePredictor


class IdentityPredictor(RepresentablePredictor):
    """
    A `Predictor` that uses the last `prediction_length` observations
    to predict the future.

    Parameters
    ----------
    prediction_length
        Prediction horizon.
    freq
        Frequency of the predicted data.
    num_samples
        Number of samples to include in the forecasts. Not that the samples
        produced by this predictor will all be identical.
    """

    @validated()
    def __init__(
        self, prediction_length: int, freq: str, num_samples: int
    ) -> None:
        super().__init__(prediction_length, freq)

        assert num_samples > 0, "The value of `num_samples` should be > 0"

        self.num_samples = num_samples

    def predict(self, dataset: Dataset, **kwargs) -> Iterator[Forecast]:
        for item in dataset:
            prediction = item["target"][-self.prediction_length :]
            samples = np.broadcast_to(
                array=np.expand_dims(prediction, 0),
                shape=(self.num_samples, self.prediction_length),
            )

            yield SampleForecast(
                samples=samples,
                start_date=item["start"],
                freq=self.freq,
                item_id=item["id"] if "id" in item else None,
            )


class ConstantPredictor(RepresentablePredictor):
    """
    A `Predictor` that always produces the same forecast.

    Parameters
    ----------
    samples
        Samples to use to construct SampleForecast objects for every
        prediction.
    freq
        Frequency of the predicted data.
    """

    @validated()
    def __init__(self, samples: np.ndarray, freq: str) -> None:
        super().__init__(samples.shape[1], freq)
        self.samples = samples

    def predict(self, dataset: Dataset, **kwargs) -> Iterator[SampleForecast]:
        for item in dataset:
            yield SampleForecast(
                samples=self.samples,
                start_date=item["start"],
                freq=self.freq,
                item_id=item["id"] if "id" in item else None,
            )


class MeanPredictor(RepresentablePredictor):
    """
    A :class:`Predictor` that predicts the mean of the last `context_length`
    elements of the input target.

    Parameters
    ----------
    context_length
        Length of the target context used to condition the predictions.
    prediction_length
        Length of the prediction horizon.
    num_eval_samples
        Number of samples to use to construct :class:`SampleForecast` objects
        for every prediction.
    freq
        Frequency of the predicted data.
    """

    @validated()
    def __init__(
        self,
        context_length: int,
        prediction_length: int,
        num_eval_samples: int,
        freq: str,
    ) -> None:
        super().__init__(prediction_length, freq)
        self.context_length = context_length
        self.num_eval_samples = num_eval_samples
        self.shape = (self.num_eval_samples, self.prediction_length)

    def predict(self, dataset: Dataset, **kwargs) -> Iterator[SampleForecast]:
        for item in dataset:
            mean = np.mean(item["target"][-self.context_length :])
            yield SampleForecast(
                samples=mean * np.ones(shape=self.shape),
                start_date=item["start"],
                freq=self.freq,
                item_id=item["id"] if "id" in item else None,
            )


class MeanEstimator(Estimator):
    """
    An `Estimator` that computes the mean targets in the training data,
    in the trailing `prediction_length` observations, and produces
    a `ConstantPredictor` that always predicts such mean value.

    Parameters
    ----------
    prediction_length
        Prediction horizon.
    freq
        Frequency of the predicted data.
    num_samples
        Number of samples to include in the forecasts. Not that the samples
        produced by this predictor will all be identical.
    """

    @validated()
    def __init__(
        self, prediction_length: int, freq: str, num_eval_samples: int
    ) -> None:
        assert (
            prediction_length > 0
        ), "The value of `prediction_length` should be > 0"
        assert num_eval_samples > 0, "The value of `num_samples` should be > 0"

        self.prediction_length = prediction_length
        self.freq = freq
        self.num_eval_samples = num_eval_samples

    def train(self, training_data: Dataset) -> ConstantPredictor:
        # import itertools
        # from gluonts.dataset.common import ListDataset
        #
        # training_data = ListDataset(
        #     data_iter=itertools.chain(training_data, training_data),
        #     freq=self.freq,
        # )

        contexts = np.broadcast_to(
            array=[
                item["target"][-self.prediction_length :]
                for item in training_data
            ],
            shape=(len(training_data), self.prediction_length),
        )

        samples = np.broadcast_to(
            array=contexts.mean(axis=0),
            shape=(self.num_eval_samples, self.prediction_length),
        )

        return ConstantPredictor(samples=samples, freq=self.freq)
