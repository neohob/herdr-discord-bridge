"""NDJSON request/response helpers matching herdr's socket API."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


class HerdrProtocolError(Exception):
    """Transport or decode failure."""


class HerdrApiError(Exception):
    def __init__(self, code: str, message: str, request_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id


def make_request(method: str, params: dict[str, Any] | None = None, req_id: str | None = None) -> dict[str, Any]:
    return {
        "id": req_id or f"req_{uuid4().hex[:12]}",
        "method": method,
        "params": params or {},
    }


def encode_line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_line(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace").strip()
    else:
        text = line.strip()
    if not text:
        raise HerdrProtocolError("empty response line")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HerdrProtocolError(f"invalid json: {exc}") from exc
    if not isinstance(value, dict):
        raise HerdrProtocolError("response must be a JSON object")
    return value


def unwrap_result(payload: dict[str, Any]) -> Any:
    if "error" in payload and payload["error"] is not None:
        err = payload["error"] or {}
        raise HerdrApiError(
            code=str(err.get("code", "unknown")),
            message=str(err.get("message", "unknown error")),
            request_id=str(payload.get("id")) if payload.get("id") is not None else None,
        )
    if "result" not in payload:
        return payload
    return payload["result"]
