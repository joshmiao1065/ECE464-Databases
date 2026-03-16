import csv
import io
from typing import List

import librosa
import numpy as np
import tensorflow as tf
import tensorflow_hub as hub

_MODEL_URL = "https://tfhub.dev/google/yamnet/1"


class YAMNetWorker:
    """
    Wraps Google YAMNet for coarse sound classification (kick, snare, synth, etc.).
    Used to auto-populate sample_tags with source='auto'.
    """

    def __init__(self):
        self.model = hub.load(_MODEL_URL)
        class_map_path = self.model.class_map_path().numpy().decode()
        self.class_names: List[str] = []
        with tf.io.gfile.GFile(class_map_path) as f:
            for row in csv.DictReader(f):
                self.class_names.append(row["display_name"])

    def predict(self, audio_bytes: bytes, top_k: int = 5) -> List[str]:
        """
        Return the top-k YAMNet class labels for the given audio.
        These become candidate tags (still filtered before being written to the DB).
        """
        # YAMNet requires 16 kHz mono float32 waveform
        y, _ = librosa.load(io.BytesIO(audio_bytes), sr=16_000, mono=True)
        waveform = tf.constant(y, dtype=tf.float32)

        scores, _, _ = self.model(waveform)
        mean_scores = tf.reduce_mean(scores, axis=0).numpy()
        top_indices = np.argsort(mean_scores)[::-1][:top_k]
        return [self.class_names[i] for i in top_indices]
