from src.shared.ndjson import make_request, encode_line, decode_line, unwrap_result, HerdrApiError
import pytest

def test_roundtrip_request_line():
    req = make_request("ping", {}, req_id="req_1")
    assert req["method"] == "ping" and req["id"] == "req_1"
    line = encode_line(req)
    assert line.endswith(b"\n")
    assert decode_line(line)["method"] == "ping"

def test_unwrap_error():
    with pytest.raises(HerdrApiError) as ei:
        unwrap_result({"id": "1", "error": {"code": "x", "message": "nope"}})
    assert ei.value.code == "x"
