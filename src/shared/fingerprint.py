"""TLS certificate SHA-256 fingerprint helpers."""

from __future__ import annotations

import hashlib
import hmac
import ssl


def _normalize_fingerprint(fingerprint: str) -> str:
    value = fingerprint.strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    return value


def cert_sha256_fingerprint(cert_pem_or_der: bytes) -> str:
    """Return bare lowercase hex SHA-256 fingerprint of a PEM or DER certificate."""
    if b"-----BEGIN" in cert_pem_or_der:
        der = ssl.PEM_cert_to_DER_cert(cert_pem_or_der.decode("ascii"))
    else:
        der = cert_pem_or_der
    return hashlib.sha256(der).hexdigest()


def fingerprints_match(expected: str, actual: str) -> bool:
    return hmac.compare_digest(_normalize_fingerprint(expected), _normalize_fingerprint(actual))
