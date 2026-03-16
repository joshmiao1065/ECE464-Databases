import os
import tempfile


class MusiCNNWorker:
    """
    Music tagger using MTG's MusiCNN (MTT_musicnn checkpoint).

    Produces high-level semantic tags — genre, mood, instrumentation — from the
    MagnaTagATune label set (~50 classes: 'guitar', 'classical', 'ambient', etc.).
    These complement YAMNet's fine-grained sound-event labels with musical context.

    MusiCNN loads the model on first call; subsequent calls reuse the loaded weights.
    It requires a file path rather than raw bytes, so audio is written to a temp file.
    """

    def predict(self, audio_bytes: bytes, top_k: int = 5) -> list[str]:
        """
        Return the top-k MagnaTagATune tags for the given audio.

        Args:
            audio_bytes: Raw audio file bytes (MP3 or WAV).
            top_k:       Number of top tags to return (default 5).

        Returns:
            List of tag name strings ordered by confidence, e.g.
            ['guitar', 'classical', 'slow', 'strings', 'not rock'].
        """
        # musicnn.tagger.top_tags operates on a file path, not in-memory bytes.
        # Write to a named temp file with the correct extension so librosa can
        # infer the format when musicnn loads it internally.
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            # Import here so the TF graph is only built when actually needed
            # (avoids slowing down the process on import if musicnn isn't installed).
            from musicnn.tagger import top_tags  # type: ignore[import]

            tags = top_tags(tmp_path, model="MTT_musicnn", topN=top_k)
            return list(tags)
        finally:
            os.unlink(tmp_path)
