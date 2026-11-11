"""OpenAI and Gemini behind the Claude Messages API. Requests go out in each provider's format and
answers come back as Claude messages and events, so routing, the relay, cost logging and quality
checks never need to know which provider answered. Text chat only.
"""
import json
import os
import uuid
from typing import AsyncIterator

import httpx

from .router import text_of

OPENAI_URL = os.environ.get("OPENAI_URL", "https://api.openai.com/v1/chat/completions")
GEMINI_URL = os.environ.get("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/models")
KEYS = {"openai": os.environ.get("OPENAI_API_KEY", ""), "gemini": os.environ.get("GEMINI_API_KEY", "")}

# Anything else (Gemini's SAFETY, RECITATION, OpenAI's content_filter...) means the model declined.
STOP_REASONS = {"stop": "end_turn", "STOP": "end_turn", "length": "max_tokens", "MAX_TOKENS": "max_tokens"}
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
}


def configured() -> list[str]:
    return ["anthropic"] + [provider for provider, key in KEYS.items() if key]


def text_only(body: dict) -> bool:
    """These providers get plain text conversations. Tools, images and files stay on Claude."""
    if body.get("tools"):
        return False
    for message in body.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            return False
        content = message.get("content")
        if isinstance(content, list):
            if not all(isinstance(block, dict) and block.get("type") == "text" for block in content):
                return False
        elif not isinstance(content, str):
            return False
    return True


def build(http: httpx.AsyncClient, provider: str, body: dict, model: str) -> httpx.Request:
    system = text_of({"content": body.get("system")})
    turns = [(message["role"], text_of(message)) for message in body["messages"]]
    stream = bool(body.get("stream"))

    if provider == "openai":
        messages = [{"role": "system", "content": system}] if system else []
        messages += [{"role": role, "content": text} for role, text in turns]
        payload = {"model": model, "messages": messages, "max_completion_tokens": body.get("max_tokens"), "stream": stream}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return http.build_request("POST", OPENAI_URL, headers={"authorization": f"Bearer {KEYS['openai']}"}, json=payload)

    payload = {
        "contents": [{"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]} for role, text in turns],
        "generationConfig": {"maxOutputTokens": body.get("max_tokens")},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"{GEMINI_URL}/{model}:{'streamGenerateContent' if stream else 'generateContent'}"
    return http.build_request(
        "POST", url, params={"alt": "sse"} if stream else None, headers={"x-goog-api-key": KEYS["gemini"]}, json=payload
    )


def read(provider: str, data: dict) -> tuple[str, str | None, dict | None]:
    """The text, stop reason and usage in one response or stream chunk, in Claude's terms."""
    if provider == "openai":
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("delta") or choice.get("message") or {}).get("content") or ""
        reason = choice.get("finish_reason")
        usage = data.get("usage")
        if usage:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            usage = {
                "input_tokens": usage["prompt_tokens"] - cached,
                "cache_read_input_tokens": cached,
                "output_tokens": usage["completion_tokens"],  # includes reasoning, which is billed as output
            }
    else:
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        reason = candidate.get("finishReason")
        usage = data.get("usageMetadata")
        if usage:
            cached = usage.get("cachedContentTokenCount") or 0
            usage = {
                "input_tokens": (usage.get("promptTokenCount") or 0) - cached,
                "cache_read_input_tokens": cached,
                "output_tokens": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0),
            }
    return text, reason and STOP_REASONS.get(reason, "refusal"), usage


def claude_message(provider: str, data: dict) -> dict:
    text, stop_reason, usage = read(provider, data)
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {},
    }


def event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}".encode()


async def claude_events(provider: str, upstream: httpx.Response) -> AsyncIterator[bytes]:
    """The provider's stream as Claude's event stream, one event at a time."""
    message = {"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "content": [], "usage": {}}
    yield event("message_start", {"message": message})
    yield event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
    stop_reason, usage = None, {}
    async for line in upstream.aiter_lines():  # handles Gemini's \r\n as well as OpenAI's \n
        data = line.removeprefix("data:").strip()
        if not line.startswith("data:") or data == "[DONE]":
            continue
        text, reason, latest = read(provider, json.loads(data))
        if text:
            yield event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}})
        stop_reason, usage = reason or stop_reason, latest or usage  # usage arrives last, or grows as it goes
    yield event("content_block_stop", {"index": 0})
    yield event("message_delta", {"delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage})
    yield event("message_stop", {})


def claude_error(status: int, content: bytes) -> bytes:
    try:
        message = json.loads(content)["error"]["message"]
    except (KeyError, TypeError, ValueError):
        message = content.decode(errors="replace")[:500] or "the provider returned an error"
    return json.dumps({"type": "error", "error": {"type": ERROR_TYPES.get(status, "api_error"), "message": message}}).encode()
