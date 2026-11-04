"""
Audio Sources Module

The audio source contract and the S3 recording replay that implements it.
"""

from .base import AudioSource
from .s3_source import S3AudioSource, S3AudioSourceFactory

__all__ = [
    "AudioSource",
    "S3AudioSource",
    "S3AudioSourceFactory",
]
