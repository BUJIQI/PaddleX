# copyright (c) 2024 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import pickle
from pathlib import Path
import faiss
import numpy as np

from ....utils import logging
from ..base import BaseComponent


class FaissIndexer(BaseComponent):

    INPUT_KEYS = "feature"
    OUTPUT_KEYS = ["label", "score"]
    DEAULT_INPUTS = {"feature": "feature"}
    DEAULT_OUTPUTS = {"label": "label", "score": "score"}

    ENABLE_BATCH = True

    def __init__(
        self,
        index_bytes=None,
        vector_path=None,
        id_map=None,
        metric_type="IP",
        return_k=1,
        score_thres=None,
        hamming_radius=None,
    ):
        super().__init__()

        if metric_type == "hamming":
            if index_bytes is not None:
                self._indexer = faiss.deserialize_index(index_bytes)
            else:
                self._indexer = faiss.read_index_binary(vector_path)
            self.hamming_radius = hamming_radius
        else:
            if index_bytes is not None:
                self._indexer = faiss.deserialize_index(index_bytes)
            else:
                self._indexer = faiss.read_index(vector_path)
            self.score_thres = score_thres

        self.id_map = id_map
        self.metric_type = metric_type
        self.return_k = return_k

    def apply(self, feature):
        """apply"""
        scores_list, ids_list = self._indexer.search(np.array(feature), self.return_k)
        preds = []
        for scores, ids in zip(scores_list, ids_list):
            labels = []
            for id in ids:
                if id > 0:
                    labels.append(self.id_map[id])
            preds.append({"score": scores, "label": labels})

        if self.metric_type == "hamming":
            idxs = np.where(scores_list[:, 0] > self.hamming_radius)[0]
        else:
            idxs = np.where(scores_list[:, 0] < self.score_thres)[0]
        for idx in idxs:
            preds[idx] = {"score": None, "label": None}
        return preds


class FaissBuilder:

    SUPPORT_MODE = ("new", "remove", "append")
    SUPPORT_METRIC_TYPE = ("hamming", "IP", "L2")
    SUPPORT_INDEX_TYPE = ("Flat", "IVF", "HNSW32")
    BINARY_METRIC_TYPE = ("hamming",)
    BINARY_SUPPORT_INDEX_TYPE = ("Flat", "IVF", "BinaryHash")

    def __init__(self, predict, mode="new", index_type="HNSW32", metric_type="IP"):
        super().__init__()
        assert (
            mode in self.SUPPORT_MODE
        ), f"Supported modes only: {self.SUPPORT_MODE}. But received {mode}!"
        assert (
            metric_type in self.SUPPORT_METRIC_TYPE
        ), f"Supported metric types only: {self.SUPPORT_METRIC_TYPE}!"
        assert (
            index_type in self.SUPPORT_INDEX_TYPE
        ), f"Supported index types only: {self.SUPPORT_INDEX_TYPE}!"

        self._predict = predict
        self._mode = mode
        self._metric_type = metric_type
        self._index_type = index_type

    def _get_index_type(self, num=None):
        # if IVF method, cal ivf number automaticlly
        if self._index_type == "IVF":
            index_type = self._index_type + str(min(int(num // 8), 65536))
            if self._metric_type in self.BINARY_METRIC_TYPE:
                index_type += ",BFlat"
            else:
                index_type += ",Flat"

        # for binary index, add B at head of index_type
        if self._metric_type in self.BINARY_METRIC_TYPE:
            assert (
                self._index_type in self.BINARY_SUPPORT_INDEX_TYPE
            ), f"The metric type({self._metric_type}) only support {self.BINARY_SUPPORT_INDEX_TYPE} index types!"
            index_type = "B" + index_type

        if self._index_type == "HNSW32":
            logging.warning("The HNSW32 method dose not support 'remove' operation")
            index_type = "HNSW32"

        if self._index_type == "Flat":
            index_type = "Flat"

        return index_type

    def _get_metric_type(self):
        if self._metric_type == "hamming":
            return faiss.METRIC_Hamming
        elif self._metric_type == "jaccard":
            return faiss.METRIC_Jaccard
        elif self._metric_type == "IP":
            return faiss.METRIC_INNER_PRODUCT
        elif self._metric_type == "L2":
            return faiss.METRIC_L2

    def build(
        self,
        label_file,
        image_root,
        index_dir,
    ):
        file_list, gallery_docs = get_file_list(label_file, image_root)

        features = [res["feature"] for res in self._predict(file_list)]
        dtype = np.uint8 if self._metric_type in self.BINARY_METRIC_TYPE else np.float32
        features = np.array(features).astype(dtype)
        vector_num, vector_dim = features.shape

        if self._metric_type in self.BINARY_METRIC_TYPE:
            index = faiss.index_binary_factory(
                vector_dim,
                self._get_index_type(vector_num),
                self._get_metric_type(),
            )
        else:
            index = faiss.index_factory(
                vector_dim,
                self._get_index_type(vector_num),
                self._get_metric_type(),
            )
            index = faiss.IndexIDMap2(index)
        ids = {}

        # calculate id for new data
        index, ids = self._add_gallery(index, ids, features, gallery_docs)
        self._save_gallery(index, ids, index_dir)
        return faiss.serialize_index(index), ids

    def remove(
        self,
        label_file,
        image_root,
        index_dir,
    ):
        file_list, gallery_docs = get_file_list(label_file, image_root)

        # load vector.index and id_map.pkl
        index, ids = self._load_index(index_dir)

        if self._index_type == "HNSW32":
            raise RuntimeError(
                "The index_type: HNSW32 dose not support 'remove' operation"
            )

        # remove ids in id_map, remove index data in faiss index
        index, ids = self._rm_id_in_galllery(index, ids, gallery_docs)
        self._save_gallery(index, ids, index_dir)
        return faiss.serialize_index(index), ids

    def append(
        self,
        label_file,
        image_root,
        index_dir,
    ):
        file_list, gallery_docs = get_file_list(label_file, image_root)
        features = [res["feature"] for res in self._predict(file_list)]
        dtype = np.uint8 if self._metric_type in self.BINARY_METRIC_TYPE else np.float32
        features = np.array(features).astype(dtype)

        # load vector.index and id_map.pkl
        index, ids = self._load_index(index_dir)

        # calculate id for new data
        index, ids = self._add_gallery(index, ids, features, gallery_docs)
        self._save_gallery(index, ids, index_dir)
        return faiss.serialize_index(index), ids

    def _load_index(self, index_dir):
        assert os.path.join(
            index_dir, "vector.index"
        ), "The vector.index dose not exist in {} when 'index_operation' is not None".format(
            index_dir
        )
        assert os.path.join(
            index_dir, "id_map.pkl"
        ), "The id_map.pkl dose not exist in {} when 'index_operation' is not None".format(
            index_dir
        )
        index = faiss.read_index(os.path.join(index_dir, "vector.index"))
        with open(os.path.join(index_dir, "id_map.pkl"), "rb") as fd:
            ids = pickle.load(fd)
        assert index.ntotal == len(
            ids.keys()
        ), "data number in index is not equal in in id_map"
        return index, ids

    def _add_gallery(self, index, ids, gallery_features, gallery_docs):
        start_id = max(ids.keys()) + 1 if ids else 0
        ids_now = (np.arange(0, len(gallery_docs)) + start_id).astype(np.int64)

        # only train when new index file
        if self._mode == "new":
            if self._metric_type in self.BINARY_METRIC_TYPE:
                index.add(gallery_features)
            else:
                index.train(gallery_features)

        if not self._metric_type in self.BINARY_METRIC_TYPE:
            index.add_with_ids(gallery_features, ids_now)

        for i, d in zip(list(ids_now), gallery_docs):
            ids[i] = d
        return index, ids

    def _rm_id_in_galllery(self, index, ids, gallery_docs):
        remove_ids = list(filter(lambda k: ids.get(k) in gallery_docs, ids.keys()))
        remove_ids = np.asarray(remove_ids)
        index.remove_ids(remove_ids)
        for k in remove_ids:
            del ids[k]

        return index, ids

    def _save_gallery(self, index, ids, index_dir):
        Path(index_dir).mkdir(parents=True, exist_ok=True)
        if self._metric_type in self.BINARY_METRIC_TYPE:
            faiss.write_index_binary(index, os.path.join(index_dir, "vector.index"))
        else:
            faiss.write_index(index, os.path.join(index_dir, "vector.index"))

        with open(os.path.join(index_dir, "id_map.pkl"), "wb") as fd:
            pickle.dump(ids, fd)


def get_file_list(data_file, root_dir, delimiter=" "):
    root_dir = Path(root_dir)
    files = []
    labels = []
    lines = []
    with open(data_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        path, label = line.strip().split(delimiter)
        file_path = root_dir / path
        files.append(file_path.as_posix())
        labels.append(label)

    return files, labels
