from __future__ import annotations

import os
import ipaddress
from dataclasses import dataclass
from pathlib import Path

MAX_UPLOAD_BYTES = 5_000_000_000


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    upload_chunk_bytes: int = 8 * 1024 * 1024
    segment_seconds: int = 300
    overlap_seconds: float = 2.0
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self) -> None:
        if self.max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        if self.max_upload_bytes > MAX_UPLOAD_BYTES:
            raise ValueError(f"max_upload_bytes cannot exceed the product limit of {MAX_UPLOAD_BYTES} bytes")
        if not 1 <= self.upload_chunk_bytes <= 32 * 1024 * 1024:
            raise ValueError("upload_chunk_bytes must be between 1 byte and 32 MiB")
        if not 1 <= self.segment_seconds <= 3600:
            raise ValueError("segment_seconds must be between 1 and 3600")
        if not 0 <= self.overlap_seconds < self.segment_seconds:
            raise ValueError("overlap_seconds must be non-negative and shorter than a segment")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.host != "localhost" and not ipaddress.ip_address(self.host).is_loopback:
            raise ValueError("AudioScribe can bind only to a loopback address")

    @classmethod
    def from_env(cls) -> "Settings":
        base = Path(os.environ.get("AUDIOSCRIBE_HOME", Path.home() / ".audioscribe"))
        return cls(
            data_dir=base,
            max_upload_bytes=int(os.environ.get("AUDIOSCRIBE_MAX_UPLOAD", MAX_UPLOAD_BYTES)),
            segment_seconds=int(os.environ.get("AUDIOSCRIBE_SEGMENT_SECONDS", "300")),
            overlap_seconds=float(os.environ.get("AUDIOSCRIBE_OVERLAP_SECONDS", "2")),
            host=os.environ.get("AUDIOSCRIBE_HOST", "127.0.0.1"),
            port=int(os.environ.get("AUDIOSCRIBE_PORT", "8765")),
        )

