from __future__ import annotations

from src.plugin.gateway.tls_util import fingerprint_from_cert_file, generate_self_signed


def test_generate_writes_files_and_fingerprint(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    fp = generate_self_signed(cert, key)
    assert cert.is_file() and key.is_file()
    assert len(fp) == 64
    assert fingerprint_from_cert_file(cert) == fp
