"""Shared Herdr data models and protocol errors.

Gateway RPC replaced the former SSH transport clients in Task 10.
"""

from .protocol import HerdrApiError, HerdrProtocolError

__all__ = [
    "HerdrApiError",
    "HerdrProtocolError",
]
