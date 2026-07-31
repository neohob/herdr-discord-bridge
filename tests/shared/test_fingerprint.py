from src.shared.fingerprint import fingerprints_match

def test_fingerprint_match_normalizes_prefix():
    bare = "ab" * 32
    assert fingerprints_match(bare, f"sha256:{bare}")
    assert not fingerprints_match(bare, "cd" * 32)

def test_fingerprint_match_rejects_length_mismatch():
    bare = "ab" * 32
    assert not fingerprints_match(bare, bare[:-2])
    assert not fingerprints_match(bare, "")
