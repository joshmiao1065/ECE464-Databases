import io
import os
import tempfile

import torch
import laion_clap
import librosa
import soundfile as sf


class CLAPWorker:
    """
    Wraps the LAION-CLAP model for text→vector and audio→vector encoding.
    Instantiate once at module level; weights are ~900 MB and load on first call.
    """

    def __init__(self):
        self.model = laion_clap.CLAP_Module(enable_fusion=False)
        self.model.load_ckpt()  # downloads pretrained weights if not cached

    def encode_text(self, text: str) -> list[float]:
        with torch.no_grad():
            embedding = self.model.get_text_embedding([text])
        return embedding[0].tolist()

    def encode_audio(self, audio_bytes: bytes) -> list[float]:
        """
        Encode raw audio bytes (any format librosa can read) into a 512-dim vector.
        CLAP requires mono audio at 48 kHz; we resample here.
        """
        audio, _ = librosa.load(io.BytesIO(audio_bytes), sr=48_000, mono=True)

        # Get the temp path and close the file handle before soundfile writes to it.
        # Writing while the NamedTemporaryFile handle is still open causes a double-open
        # on the same fd which is fragile (wrong seek position on some platforms).
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        sf.write(tmp_path, audio, 48_000)

        try:
            with torch.no_grad():
                embedding = self.model.get_audio_embedding_from_filelist([tmp_path])
            return embedding[0].tolist()
        finally:
            os.unlink(tmp_path)

    def encode_audio_file(self, file_path: str) -> list[float]:
        """Encode a local audio file directly (skips the bytes→wav conversion)."""
        with torch.no_grad():
            embedding = self.model.get_audio_embedding_from_filelist([file_path])
        return embedding[0].tolist()
