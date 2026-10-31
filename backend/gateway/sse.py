"""Reading the Claude API's event stream, for the live relay and for quality checks."""
import json
from typing import AsyncIterator

import httpx


async def events(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Whole SSE events as they arrive, however the network splits them."""
    pending = b""
    async for chunk in upstream.aiter_bytes():
        pending += chunk
        *complete, pending = pending.split(b"\n\n")
        for event in complete:
            yield event
    if pending:
        yield pending


class Meter:
    """Reads usage off the stream and swaps in the requested model, so routing stays invisible."""

    def __init__(self, requested: str, capture: bool = False):
        self.requested = requested
        self.capture = capture
        self.usage: dict = {}
        self.stop_reason: str | None = None
        self.text: list[str] = []

    def read(self, event: bytes) -> bytes:
        name = event.split(b"\n", 1)[0]
        if name not in (b"event: message_start", b"event: message_delta", b"event: content_block_delta"):
            return event
        if name == b"event: content_block_delta" and not self.capture:
            return event
        try:
            data = json.loads(event.split(b"data:", 1)[1])
            if name == b"event: content_block_delta":
                if data["delta"].get("type") == "text_delta":
                    self.text.append(data["delta"]["text"])
                return event
            if name == b"event: message_delta":
                self.usage.update(data.get("usage") or {})
                self.stop_reason = data["delta"].get("stop_reason")
                return event
            self.usage.update(data["message"].get("usage") or {})
            data["message"]["model"] = self.requested
            return b"event: message_start\ndata: " + json.dumps(data).encode()
        except (IndexError, KeyError, TypeError, ValueError):
            return event  # a bad event must not break the stream the employee is reading
