"""
Lazy module-level singletons for every MIR worker.

Import the getter functions here so heavy model weights (CLAP ~900 MB,
YAMNet, MusiCNN) are loaded exactly once per process regardless of how
many routers or background tasks use them.

Usage:
    from app.workers import registry
    embedding = registry.clap().encode_audio(audio_bytes)
    tags      = registry.yamnet().predict(audio_bytes)
    tags      = registry.musicnn().predict(audio_bytes)
"""

import functools


@functools.lru_cache(maxsize=None)
def clap():
    """Return the shared CLAPWorker instance (weights load on first call)."""
    from app.workers.clap_worker import CLAPWorker
    return CLAPWorker()


@functools.lru_cache(maxsize=None)
def yamnet():
    """Return the shared YAMNetWorker instance (TF Hub model loads on first call)."""
    from app.workers.yamnet_worker import YAMNetWorker
    return YAMNetWorker()


@functools.lru_cache(maxsize=None)
def musicnn():
    """Return the shared MusiCNNWorker instance (MTT model loads on first call)."""
    from app.workers.musicnn_worker import MusiCNNWorker
    return MusiCNNWorker()
