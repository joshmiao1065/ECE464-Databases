import io

import librosa
import numpy as np


KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def extract_features(audio_bytes: bytes) -> dict:
    """
    Extract MIR features from raw audio bytes using Librosa.
    Returns a dict matching the audio_metadata table columns.
    """
    y, sr = librosa.load(io.BytesIO(audio_bytes), sr=None, mono=True)

    # BPM
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])

    # Key — dominant chroma bin (root note only; mode detection is a future enhancement)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key = KEY_NAMES[int(np.argmax(chroma.mean(axis=1)))]

    # Energy (RMS mean)
    energy_level = float(np.mean(librosa.feature.rms(y=y)))

    # Approximate loudness in dBFS (not true LUFS without a proper meter)
    rms = np.sqrt(np.mean(y ** 2))
    loudness_lufs = float(20 * np.log10(rms + 1e-9))

    # Spectral centroid (brightness proxy)
    spectral_centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))

    # Zero-crossing rate (texture proxy — high for noisy/percussive sounds)
    zero_crossing_rate = float(np.mean(librosa.feature.zero_crossing_rate(y)))

    return {
        "bpm": bpm,
        "key": key,
        "energy_level": energy_level,
        "loudness_lufs": loudness_lufs,
        "spectral_centroid": spectral_centroid,
        "zero_crossing_rate": zero_crossing_rate,
        "sample_rate": sr,
    }
