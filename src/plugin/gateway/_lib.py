"""Resolve shared helpers for both monorepo and standalone plugin installs."""

from __future__ import annotations

try:
    from shared.fingerprint import cert_sha256_fingerprint, fingerprints_match
    from shared.ndjson import (
        HerdrApiError,
        HerdrProtocolError,
        decode_line,
        encode_line,
        make_request,
        unwrap_result,
    )
except ImportError:  # pragma: no cover - monorepo / bot PYTHONPATH=.
    from src.shared.fingerprint import cert_sha256_fingerprint, fingerprints_match
    from src.shared.ndjson import (
        HerdrApiError,
        HerdrProtocolError,
        decode_line,
        encode_line,
        make_request,
        unwrap_result,
    )

__all__ = [
    "HerdrApiError",
    "HerdrProtocolError",
    "cert_sha256_fingerprint",
    "decode_line",
    "encode_line",
    "fingerprints_match",
    "make_request",
    "unwrap_result",
]
