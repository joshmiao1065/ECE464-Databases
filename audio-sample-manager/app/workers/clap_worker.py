import io
import os
import tempfile

import torch
import laion_clap
from laion_clap.clap_module.factory import load_state_dict as clap_load_state_dict
import librosa
import soundfile as sf


class CLAPWorker:
    """
    Wraps the LAION-CLAP model for text→vector and audio→vector encoding.
    Instantiate once at module level; weights are ~900 MB and load on first call.
    """

    def __init__(self):
        self.model = laion_clap.CLAP_Module(enable_fusion=False)
        # Load with strict=False to tolerate extra keys (e.g. position_ids) in
        # older checkpoints that were removed from the current model architecture.
        package_dir = os.path.dirname(os.path.realpath(laion_clap.__file__))
        ckpt_path = os.path.join(package_dir, "630k-audioset-best.pt")
        if not os.path.exists(ckpt_path):
            self.model.load_ckpt()  # fall back to built-in downloader
        else:
            state_dict = clap_load_state_dict(ckpt_path, skip_params=True)
            self.model.model.load_state_dict(state_dict, strict=False)

    def encode_text(self, text: str) -> list[float]:
        # hook.py's tokenizer calls squeeze(0), which drops the batch dim when the
        # list has exactly 1 element (shape (1,77) → (77,)), breaking Roberta forward.
        # Passing a dummy duplicate forces shape (2,77); we take only the first result.
        with torch.no_grad():
            embedding = self.model.get_text_embedding([text, text])
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
