# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os

from typing import Dict, List, Optional, Union

import torch
from omegaconf import DictConfig, OmegaConf, open_dict
import pickle as pkl

# from pyannote.core import Annotation, Segment
# from pyannote.metrics.diarization import DiarizationErrorRate
from sklearn.cluster import SpectralClustering

from nemo.collections.asr.models.diarization_model import DiarizationModel
from nemo.utils import logging
from nemo.collections.asr.models import EncDecClassificationModel

try:
    from torch.cuda.amp import autocast
except ImportError:
    from contextlib import contextmanager


    @contextmanager
    def autocast(enabled=None):
        yield

__all__ = ['ClusteringSDModel']

def combine_stamps(stamps):
    combine = []
    idx, end, speaker = (0, 0, 'unknown')
    prev_start, prev_end, prev_speaker = stamps[idx].split()
    while idx < len(stamps) - 1:
        idx += 1
        start, end, speaker = stamps[idx].split()
        if speaker == prev_speaker and start <= prev_end:
            prev_end = end
        else:
            combine.append("{} {} {}".format(prev_start, prev_end, prev_speaker))
            prev_start = start
            prev_end = end
            prev_speaker = speaker

    combine.append("{} {} {}".format(prev_start, end, speaker))
    return combine


def write_label_file(labels, filename):
    with open(filename, 'w') as f:
        for line in labels:
            start, end, speaker = line.strip().split()
            f.write("{}\t{}\t{}\n".format(start, end, speaker))
    print("wrote labels to audacity type label file at ", filename)


def rttm_to_labels(rttm_filename, write=False):
    outname = rttm_filename.split('/')[-1]
    outname = outname[:-5] + '.txt'
    labels = []
    if write:
        g = open(outname, 'w')
    with open(rttm_filename, 'r') as f:
        for line in f.readlines():
            rttm = line.strip().split()
            start, end, speaker = float(rttm[3]), float(rttm[4]) + float(rttm[3]), rttm[7]
            labels.append('{} {} {}'.format(start, end, speaker))
            if write:
                g.write("{}\t{}\t{}\n".format(start, end, speaker))
    if write:
        logging.info("wrote to {}".format(outname))
        g.close()
    else:
        return labels


def labels_to_pyannote_object(labels, identifier='file1'):
    annotation = Annotation(uri=identifier)
    for label in labels:
        start, end, speaker = label.strip().split()
        start, end = float(start), float(end)
        annotation[Segment(start, end)] = speaker

    return annotation


def get_score(embeddings_file, window, shift, num_speakers, truth_rttm_dir, write_labels=True):
    window = window
    shift = shift
    embeddings = pkl.load(open(embeddings_file, 'rb'))
    embeddings_dir = os.path.dirname(embeddings_file)
    num_speakers = num_speakers
    metric = DiarizationErrorRate(collar=0.0)
    DER = 0

    for uniq_key in embeddings.keys():
        logging.info("Diarizing {}".format(uniq_key))
        identifier = uniq_key.split('@')[-1].split('.')[0]
        emb = embeddings[uniq_key]
        cluster_method = SpectralClustering(n_clusters=num_speakers, random_state=42)
        cluster_method.fit(emb)
        lines = []
        for idx, label in enumerate(cluster_method.labels_):
            start_time = idx * shift
            end_time = start_time + window
            tag = 'speaker_' + str(label)
            line = "{} {} {}".format(start_time, end_time, tag)
            lines.append(line)
        # ReSegmentation -> VAD and Segmented Results
        labels = combine_stamps(lines)
        if os.path.exists(truth_rttm_dir):
            truth_rttm = os.path.join(truth_rttm_dir, identifier + '.rttm')
            truth_labels = rttm_to_labels(truth_rttm)
            reference = labels_to_pyannote_object(truth_labels, identifier=identifier)
            DER = metric(reference, hypothesis)
        hypothesis = labels_to_pyannote_object(labels, identifier=identifier)
        if write_labels:
            filename = os.path.join(embeddings_dir, identifier + '.txt')
            write_label_file(labels, filename)
            logging.info("Wrote {} to {}".format(uniq_key, filename))

    return abs(DER)


class ClusteringSDModel(DiarizationModel):
    """Base class for encoder decoder CTC-based models."""

    def __init__(self, cfg: DictConfig):
        super().__init__(cfg=cfg)
        # init vad model
        self._vad_model = EncDecClassificationModel.restore_from(self._cfg.vad.model_path)
        self._vad_time_length = self._cfg.vad.time_length
        self._vad_shift_length = self._cfg.vad.shift_length

        # init speaker model
        self._speaker_model = EncDecClassificationModel.restore_from(
            self._cfg.speaker_embeddings.model_path)

        # Clustering method
        self._clustering_method = self._cfg.diarizer.cluster_method
        self._num_speakers = self._cfg.diarizer.num_speakers

        self._out_dir = self._cfg.diarizer.out_dir

        self._manifest_file = self._cfg.manifest_filepath
        self._device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    @classmethod
    def list_available_models(cls):
        pass

    def setup_training_data(self, train_data_config: Optional[Union[DictConfig, Dict]]):
        pass

    def setup_validation_data(self, val_data_config: Optional[Union[DictConfig, Dict]]):
        pass

    def _setup_test_data(self, config):
        vad_dl_config = {
            'manifest_filepath': config['manifest'],
            'sample_rate': self._cfg.sample_rate,
            'batch_size': 1,
            'vad_stream': True,
            'labels': ['infer', ],
            'time_length': self._cfg.vad.time_length,
            'shift_length': self._cfg.vad.shift_length,
            'trim_silence': False,
        }

        self._vad_model.setup_test_data(
            test_data_config=vad_dl_config
        )
        # spk_dl_config = {
        #     'manifest_filepath': config['manifest'],
        #     'sample_rate': self._cfg.sample_rate,
        #     'batch_size': 1,
        #     'time_length': self._cfg.speaker_emb.time_length,
        #     'shift_length': self._cfg.speaker_emb.shift_length,
        #     'trim_silence': False,
        # }
        # speaker_model.setup_test_data(spk_dl_config)

    def _eval_vad(self, manifest_file):
        self._vad_model = self._vad_model.to(self._device)
        self._vad_model.eval()

        time_unit = int(self._cfg.vad.time_length / self._cfg.vad.shift_length)
        trunc = int(time_unit / 2)
        trunc_l = time_unit - trunc
        all_len = 0
        data = []
        for line in open(manifest_file, 'r'):
            file = os.path.basename(json.loads(line)['audio_filepath'])
            data.append(os.path.splitext(file)[0])
        for i, test_batch in enumerate(self._vad_model.test_dataloader()):
            if i == 0:
                status = 'start' if data[i] == data[i + 1] else 'single'
            elif i == len(data) - 1:
                status = 'end' if data[i] == data[i - 1] else 'single'
            else:
                if data[i] != data[i - 1] and data[i] == data[i + 1]:
                    status = 'start'
                elif data[i] == data[i - 1] and data[i] == data[i + 1]:
                    status = 'next'
                elif data[i] == data[i - 1] and data[i] != data[i + 1]:
                    status = 'end'
                else:
                    status = 'single'
            print(data[i], status)

            test_batch = [x.to(self._device) for x in test_batch]
            with autocast():
                log_probs = self._vad_model(input_signal=test_batch[0], input_signal_length=test_batch[1])
                probs = torch.softmax(log_probs, dim=-1)
                pred = probs[:, 1]

                if status == 'start':
                    to_save = pred[:-trunc]
                elif status == 'next':
                    to_save = pred[trunc:-trunc_l]
                elif status == 'end':
                    to_save = pred[trunc_l:]
                else:
                    to_save = pred
                all_len += len(to_save)

                outpath = os.path.join(self._out_dir, data[i] + ".frame")
                with open(outpath, "a") as fout:
                    for f in range(len(to_save)):
                        fout.write('{0:0.4f}\n'.format(to_save[f]))

            del test_batch
            if status == 'end' or status == 'single':
                print(f"Overall length of prediction of {data[i]} is {all_len}!")
                all_len = 0

    def _extract_embeddings(self, manifest_file):
        # create unique labels
        uniq_names=[]
        out_embeddings = {}
        with open(self.test_manifest, 'r') as manifest:
            for idx, line in enumerate(manifest.readlines()):
                line = line.strip()
                dic = json.loads(line)
                structure = dic['audio_filepath'].split('/')[-3:]
                uniq_names.append('@'.join(structure))
                if uniq_names[-1] in out_embeddings:
                    raise KeyError("Embeddings for label {} already present in emb dictionary".format(uniq_name))

        for i, test_batch in enumerate(self._speaker_model.test_dataloader()):
            with autocast():
                audio_signal, audio_signal_len, labels, slices = test_batch
                _, embs = self._speaker_model(input_signal=audio_signal, input_signal_length=audio_signal_len)
                emb_shape = embs.shape[-1]
                embs = embs.view(-1, emb_shape).cpu().numpy()
                out_embeddings[uniq_names[i]] = embs.mean(axis=0)

        embedding_dir = os.path.join(self._out_dir, 'embeddings')
        if not os.path.exists(embedding_dir):
            os.mkdir(embedding_dir)

        prefix = manifest_file.split('/')[-1].split('.')[-2]

        name = os.path.join(embedding_dir, prefix)
        self._embeddings_file = name + '_embeddings.pkl'
        pkl.dump(out_embeddings, open(self._embeddings_file, 'wb'))
        logging.info("Saved embedding files to {}".format(embedding_dir))

    @torch.no_grad()
    def diarize(self, paths2audio_files: List[str] = None, batch_size: int = 1) -> List[str]:
        """
        """
        if (paths2audio_files is None or len(paths2audio_files) == 0) and self._manifest_file is None:
            return {}

        if not os.path.exists(self._out_dir):
            os.mkdir(self._out_dir)

        # setup_test_data

        logging.set_verbosity(logging.WARNING)
        # Work in tmp directory - will store manifest file there
        if paths2audio_files is not None:
            mfst_file = os.path.join(self._out_dir, 'manifest.json')
            with open(mfst_file, 'w') as fp:
                for audio_file in paths2audio_files:
                    entry = {'audio_filepath': audio_file, 'duration': 100000, 'text': '-'}
                    fp.write(json.dumps(entry) + '\n')
        else:
            mfst_file = self._manifest_file

        config = {'paths2audio_files': paths2audio_files, 'batch_size': batch_size, 'manifest': mfst_file}

        self._setup_test_data(config)
        self._eval_vad(mfst_file)

        # get manifest for speaker embeddings

        self._extract_embeddings("vad_output_file")
        DER = get_score(self._embeddings_file, self.emb_window, self._emb_shift, self._num_speakers, self._rttm_dir,
                        write_labels=True)
        logging.info("Cumulative DER of all the files is {:.3f}".format(DER))