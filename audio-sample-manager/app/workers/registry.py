"""
Lazy module-level singletons for every MIR worker.

Import the getter functions here so heavy model weights (CLAP ~900 MB,
YAMNet, MusiCNN) are loaded exactly once per process regardless of how
many routers or background tasks use them.

Usage:
    from app.workers import registry
    embedding = registry.clap().encode_audio(audio_bytes)
    tags      = registry.yamnet().predict(audio_bytes)   # may return None if TF unavailable
    tags      = registry.musicnn().predict(audio_bytes)  # may return None if MusiCNN unavailable
"""

import functools
import logging
import os

# MusiCNN uses tf.compat.v1.layers (Keras 2 API). TF 2.16+ defaults to Keras 3,
# which removed these legacy layers. Setting this env var before TF is first
# imported forces TF to use tf-keras (Keras 2) instead.
os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")

log = logging.getLogger(__name__)

_YAMNET_UNAVAILABLE = False
_MUSICNN_UNAVAILABLE = False


@functools.lru_cache(maxsize=None)
def clap():
    """Return the shared CLAPWorker instance (weights load on first call)."""
    from app.workers.clap_worker import CLAPWorker
    return CLAPWorker()


@functools.lru_cache(maxsize=None)
def yamnet():
    """
    Return the shared YAMNetWorker instance, or None if TensorFlow is unavailable.
    The pipeline skips YAMNet tagging gracefully when None is returned.
    """
    global _YAMNET_UNAVAILABLE
    if _YAMNET_UNAVAILABLE:
        return None
    try:
        from app.workers.yamnet_worker import YAMNetWorker
        return YAMNetWorker()
    except Exception as exc:
        log.warning("YAMNet unavailable (%s: %s) — skipping in pipeline", type(exc).__name__, exc)
        _YAMNET_UNAVAILABLE = True
        return None


@functools.lru_cache(maxsize=None)
def musicnn():
    """
    Return the shared MusiCNNWorker instance, or None if MusiCNN is unavailable.
    The pipeline skips MusiCNN tagging gracefully when None is returned.

    IMPORTANT: do NOT import musicnn.tagger here.  That module calls
    tf.compat.v1.disable_eager_execution() at import time which would silently
    break YAMNet (a TF2 eager-mode SavedModel) in the same process.
    MusiCNNWorker runs musicnn in a subprocess instead — see musicnn_worker.py.
    """
    global _MUSICNN_UNAVAILABLE
    if _MUSICNN_UNAVAILABLE:
        return None
    try:
        from app.workers.musicnn_worker import MusiCNNWorker
        return MusiCNNWorker()
    except Exception as exc:
        log.warning("MusiCNN unavailable (%s: %s) — skipping in pipeline", type(exc).__name__, exc)
        _MUSICNN_UNAVAILABLE = True
        return None
