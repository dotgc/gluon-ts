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
import shutil
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sized,
    cast,
)

# Third-party imports
import numpy as np
import pandas as pd
import pydantic
import ujson as json
from pandas.tseries.frequencies import to_offset

# First-party imports
from gluonts.core.exception import GluonTSDataError
from gluonts.dataset import jsonl, util
from gluonts.dataset.stat import (
    DatasetStatistics,
    calculate_dataset_statistics,
)

# Dictionary used for data flowing through the transformations.
# A Dataset is an iterable over such dictionaries.
DataEntry = Dict[str, Any]


class Timestamp(pd.Timestamp):
    # we need to sublcass, since pydantic otherwise converts the value into
    # datetime.datetime instead of using pd.Timestamp
    @classmethod
    def __get_validators__(cls):
        def conv(val):
            if isinstance(val, pd.Timestamp):
                return val
            else:
                return pd.Timestamp(val)

        yield conv


class TimeSeriesItem(pydantic.BaseModel):
    class Config:
        arbitrary_types_allowed = True
        json_encoders = {np.ndarray: np.ndarray.tolist}

    start: Timestamp
    target: np.ndarray
    item: Optional[str] = None

    feat_static_cat: List[int] = []
    feat_static_real: List[float] = []
    feat_dynamic_cat: List[List[int]] = []
    feat_dynamic_real: List[List[float]] = []

    # A dataset can use this field to include information about the origin of
    # the item (e.g. the file name and line). If an exception in a
    # transformation occurs the content of the field will be included in the
    # error message (if the field is set).
    metadata: dict = {}

    @pydantic.validator("target", pre=True)
    def validate_target(cls, v):
        return np.asarray(v)

    def __eq__(self, other: Any) -> bool:
        # we have to overwrite this function, since we can't just compare
        # numpy ndarrays, but have to call all on it
        if isinstance(other, TimeSeriesItem):
            return (
                self.start == other.start
                and (self.target == other.target).all()
                and self.item == other.item
                and self.feat_static_cat == other.feat_static_cat
                and self.feat_static_real == other.feat_static_real
                and self.feat_dynamic_cat == other.feat_dynamic_cat
                and self.feat_dynamic_real == other.feat_dynamic_real
            )
        return False

    def gluontsify(self, metadata: "MetaData") -> dict:
        data: dict = {
            "item": self.item,
            "start": self.start,
            "target": self.target,
        }

        if metadata.feat_static_cat:
            data["feat_static_cat"] = self.feat_static_cat
        if metadata.feat_static_real:
            data["feat_static_real"] = self.feat_static_real
        if metadata.feat_dynamic_cat:
            data["feat_dynamic_cat"] = self.feat_dynamic_cat
        if metadata.feat_dynamic_real:
            data["feat_dynamic_real"] = self.feat_dynamic_real

        return data


class BasicFeatureInfo(pydantic.BaseModel):
    name: str


class CategoricalFeatureInfo(pydantic.BaseModel):
    name: str
    cardinality: str


class MetaData(pydantic.BaseModel):
    freq: str = pydantic.Schema(..., alias="time_granularity")  # type: ignore
    target: Optional[BasicFeatureInfo] = None

    feat_static_cat: List[CategoricalFeatureInfo] = []
    feat_static_real: List[BasicFeatureInfo] = []
    feat_dynamic_real: List[BasicFeatureInfo] = []
    feat_dynamic_cat: List[CategoricalFeatureInfo] = []

    prediction_length: Optional[int] = None

    class Config(pydantic.BaseConfig):
        allow_population_by_alias = True


class SourceContext(NamedTuple):
    source: str
    row: int


class Dataset(Sized, Iterable[DataEntry]):
    """
    An abstract class for datasets, i.e., iterable collection of DataEntry.
    """

    def __iter__(self) -> Iterator[DataEntry]:
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def calc_stats(self) -> DatasetStatistics:
        return calculate_dataset_statistics(self)


class Channel(pydantic.BaseModel):
    metadata: Path
    train: Path
    test: Optional[Path] = None

    def get_datasets(self) -> "TrainDatasets":
        return load_datasets(self.metadata, self.train, self.test)


class TrainDatasets(NamedTuple):
    """
    A dataset containing two subsets, one to be used for training purposes,
    and the other for testing purposes, as well as metadata.
    """

    metadata: MetaData
    train: Dataset
    test: Optional[Dataset] = None


class FileDataset(Dataset):
    """
    Dataset that loads JSON Lines files contained in a path.

    Parameters
    ----------
    path
        Path containing the dataset files. Each file is considered
        and should be valid to the exception of files starting with '.'
        or ending with '_SUCCESS'. A valid line in a file can be for
        instance: {"start": "2014-09-07", "target": [0.1, 0.2]}.
    freq
        Frequency of the observation in the time series.
        Must be a valid Pandas frequency.
    one_dim_target
        Whether to accept only univariate target time series.
    """

    def __init__(
        self, path: Path, freq: str, one_dim_target: bool = True
    ) -> None:
        self.path = path
        self.process = ProcessDataEntry(freq, one_dim_target=one_dim_target)
        if not self.files():
            raise OSError(f"no valid file found in {path}")

    def __iter__(self) -> Iterator[DataEntry]:
        for path in self.files():
            for line in jsonl.JsonLinesFile(path):
                data = self.process(line.content)
                data["source"] = SourceContext(
                    source=line.span, row=line.span.line
                )
                yield data

    def __len__(self):
        return sum([len(jsonl.JsonLinesFile(path)) for path in self.files()])

    def files(self) -> List[Path]:
        """
        List the files that compose the dataset.

        Returns
        -------
        List[Path]
            List of the paths of all files composing the dataset.
        """
        return util.find_files(self.path, FileDataset.is_valid)

    @staticmethod
    def is_valid(path: Path) -> bool:
        # TODO: given that we only support json, should we also filter json
        # TODO: in the extension?
        return not (path.name.startswith(".") or path.name == "_SUCCESS")


class ListDataset(Dataset):
    """
    Dataset backed directly by an array of dictionaries.

    data_iter
        Iterable object yielding all items in the dataset.
        Each item should be a dictionary mapping strings to values.
        For instance: {"start": "2014-09-07", "target": [0.1, 0.2]}.
    freq
        Frequency of the observation in the time series.
        Must be a valid Pandas frequency.
    one_dim_target
        Whether to accept only univariate target time series.
    """

    def __init__(
        self,
        data_iter: Iterable[DataEntry],
        freq: str,
        one_dim_target: bool = True,
    ) -> None:
        process = ProcessDataEntry(freq, one_dim_target)
        self.list_data = [process(data) for data in data_iter]

    def __iter__(self) -> Iterator[DataEntry]:
        source_name = "list_data"
        for row_number, data in enumerate(self.list_data, start=1):
            data["source"] = SourceContext(source=source_name, row=row_number)
            yield data

    def __len__(self):
        return len(self.list_data)


class ProcessStartField:
    """
    Transform the start field into a Timestamp with the given frequency.

    Parameters
    ----------
    name
        Name of the field to transform.
    freq
        Frequency to use. This must be a valid Pandas frequency string.
    """

    def __init__(self, name: str, freq: str) -> None:
        self.name = name
        self.freq = freq

    def __call__(self, data: DataEntry) -> DataEntry:
        try:
            value = ProcessStartField.process(data[self.name], self.freq)
        except (TypeError, ValueError) as e:
            raise GluonTSDataError(
                f'Error "{e}" occurred, when reading field "{self.name}"'
            )

        if value.tz is not None:
            raise GluonTSDataError(
                f'Timezone information is not supported, but provided in the "{self.name}" field'
            )

        data[self.name] = value

        return data

    @staticmethod
    @lru_cache(maxsize=10000)
    def process(string: str, freq: str) -> pd.Timestamp:
        timestamp = pd.Timestamp(string, freq=freq)
        # 'W-SUN' is the standardized freqstr for W
        if timestamp.freq.name in ("M", "W-SUN"):
            offset = to_offset(freq)
            timestamp = timestamp.replace(
                hour=0, minute=0, second=0, microsecond=0, nanosecond=0
            )
            return pd.Timestamp(
                offset.rollback(timestamp), freq=offset.freqstr
            )
        if timestamp.freq == "B":
            # does not floor on business day as it is not allowed
            return timestamp
        return pd.Timestamp(
            timestamp.floor(timestamp.freq), freq=timestamp.freq
        )


class ProcessTimeSeriesField:
    """
    Converts a time series field identified by `name` from a list of numbers
    into a numpy array.

    Constructor parameters modify the conversion logic in the following way:

    If `is_required=True`, throws a `GluonTSDataError` if the field is not
    present in the `Data` dictionary.

    If `is_cat=True`, the array type is `np.int32`, otherwise it is
    `np.float32`.

    If `is_static=True`, asserts that the resulting array is 1D,
    otherwise asserts that the resulting array is 2D. 2D dynamic arrays of
    shape (T) are automatically expanded to shape (1,T).

    Parameters
    ----------
    name
        Name of the field to process.
    is_required
        Whether the field must be present.
    is_cat
        Whether the field refers to categorical (i.e. integer) values.
    is_static
        Whether the field is supposed to have a time dimension.
    """

    # TODO: find a fast way to assert absence of nans.

    def __init__(
        self, name, is_required: bool, is_static: bool, is_cat: bool
    ) -> None:
        self.name = name
        self.is_required = is_required
        self.req_ndim = 1 if is_static else 2
        self.dtype = np.int32 if is_cat else np.float32

    def __call__(self, data: DataEntry) -> DataEntry:
        value = data.get(self.name, None)
        if value is not None:
            value = np.asarray(value, dtype=self.dtype)
            ddiff = self.req_ndim - value.ndim

            if ddiff == 1:
                value = np.expand_dims(a=value, axis=0)
            elif ddiff != 0:
                raise GluonTSDataError(
                    f"JSON array has bad shape - expected {self.req_ndim} "
                    f"dimensions, got {ddiff}"
                )

            data[self.name] = value

            return data
        elif not self.is_required:
            return data
        else:
            raise GluonTSDataError(
                f"JSON object is missing a required field `{self.name}`"
            )


class ProcessDataEntry:
    def __init__(self, freq: str, one_dim_target: bool = True) -> None:
        # TODO: create a FormatDescriptor object that can be derived from a
        # TODO: Metadata and pass it instead of freq.
        # TODO: In addition to passing freq, the descriptor should be carry
        # TODO: information about required features.
        self.trans = cast(
            List[Callable[[DataEntry], DataEntry]],
            [
                ProcessStartField("start", freq=freq),
                # The next line abuses is_static=True in case of 1D targets.
                ProcessTimeSeriesField(
                    "target",
                    is_required=True,
                    is_cat=False,
                    is_static=one_dim_target,
                ),
                ProcessTimeSeriesField(
                    "feat_dynamic_cat",
                    is_required=False,
                    is_cat=True,
                    is_static=False,
                ),
                ProcessTimeSeriesField(
                    "feat_dynamic_real",
                    is_required=False,
                    is_cat=False,
                    is_static=False,
                ),
                ProcessTimeSeriesField(
                    "feat_static_cat",
                    is_required=False,
                    is_cat=True,
                    is_static=True,
                ),
                ProcessTimeSeriesField(
                    "feat_static_real",
                    is_required=False,
                    is_cat=False,
                    is_static=True,
                ),
            ],
        )

    def __call__(self, data: DataEntry) -> DataEntry:
        for t in self.trans:
            data = t(data)
        return data


def load_datasets(
    metadata: Path, train: Path, test: Optional[Path]
) -> TrainDatasets:
    """
    Loads a dataset given metadata, train and test path.

    Parameters
    ----------
    metadata
        Path to the metadata file
    train
        Path to the training dataset files.
    test
        Path to the test dataset files.

    Returns
    -------
    TrainDatasets
        An object collecting metadata, training data, test data.
    """
    meta = MetaData.parse_file(Path(metadata) / "metadata.json")
    train_ds = FileDataset(train, meta.freq)
    test_ds = FileDataset(test, meta.freq) if test else None

    return TrainDatasets(metadata=meta, train=train_ds, test=test_ds)


def save_datasets(
    dataset: TrainDatasets, path_str: str, overwrite=True
) -> None:
    """
    Saves an TrainDatasets object to a JSON Lines file.

    Parameters
    ----------
    dataset
        The training datasets.
    path_str
        Where to save the dataset.
    overwrite
        Whether to delete previous version in this folder.
    """
    path = Path(path_str)

    if overwrite:
        shutil.rmtree(path, ignore_errors=True)

    def dump_line(f, line):
        f.write(json.dumps(line).encode("utf-8"))
        f.write("\n".encode("utf-8"))

    (path / "metadata").mkdir(parents=True)
    with open(path / "metadata/metadata.json", "wb") as f:
        dump_line(f, dataset.metadata.dict())

    (path / "train").mkdir(parents=True)
    with open(path / "train/data.json", "wb") as f:
        for entry in dataset.train:
            dump_line(f, serialize_data_entry(entry))

    if dataset.test is not None:
        (path / "test").mkdir(parents=True)
        with open(path / "test/data.json", "wb") as f:
            for entry in dataset.test:
                dump_line(f, serialize_data_entry(entry))


def serialize_data_entry(data):
    """
    Encode the numpy values in the a DataEntry dictionary into lists so the
    dictionary can be JSON serialized.

    Parameters
    ----------
    data
        The dictionary to be transformed.

    Returns
    -------
    Dict
        The transformed dictionary, where all fields where transformed into
        strings.
    """

    def serialize_field(field):
        if isinstance(field, np.ndarray):
            # circumvent https://github.com/micropython/micropython/issues/3511
            nan_ix = np.isnan(field)
            field = field.astype(np.object_)
            field[nan_ix] = "NaN"
            return field.tolist()
        return str(field)

    return {k: serialize_field(v) for k, v in data.items() if v is not None}
