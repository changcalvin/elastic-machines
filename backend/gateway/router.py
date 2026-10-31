"""Picks the tier for a request. Jev's answers go through policy.py; the rest is plain code."""
import hashlib
import json
import logging
import os
import time
from typing import NamedTuple

import httpx

from . import store
from .policy import CEILINGS, QUESTIONS, choose_tier
from .pricing import JEV_INPUT
from .tuning import LADDER

JEV_URL = os.environ.get("JEV_URL", "https://inference.do-ai.run/v1/systemone")
JEV_KEY = os.environ["MODEL_ACCESS_KEY"]
JEV_MODEL = "typesafe-jev-1.13.0"
JEV_TIMEOUT_S = 2.0
MAX_PROMPT_CHARS = 20_000  # stays under Jev's 32K-token state limit even at 1 token per char
REASK_WINDOW_S = 1800

logger = logging.getLogger(__name__)


class Decision(NamedTuple):
    tier: int | None  # None passes the request through untouched
    route: str
    key: str | None = None
    prompt: str | None = None
    router_ms: float | None = None
    router_usd: float | None = None
    answers: dict | None = None
    task: str | None = None
    rung: int | None = None  # the strictness this conversation was routed under


class JevResult(NamedTuple):
    tier: int
    answers: dict
    input_tokens: int
    task: str
    rung: int


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def normalized(text: str) -> str:
    return " ".join(text.lower().split())


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def in_scope(body: dict) -> bool:
    """Text requests only. Malformed ones pass through so the Claude API gives its own error."""
    messages = body.get("messages")
    if not messages or not isinstance(messages, list) or not isinstance(body.get("output_config", {}), dict):
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") in ("image", "document") for block in content
        ):
            return False
    return True


def latest_prompt(messages: list) -> str:
    """What the person last typed. Empty on tool-loop turns, which nobody typed."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            ):
                return ""
            return text_of(message)
    return ""


def is_reask(known, prompt: str | None) -> bool:
    # ponytail: exact match after normalizing, so paraphrases aren't caught. Add a Jev similarity question if the eval shows misses.
    return bool(prompt) and prompt == known["last_prompt"] and time.time() - known["last_at"] < REASK_WINDOW_S


async def ask_jev(http: httpx.AsyncClient, body: dict, prompt: str, team: str) -> JevResult | None:
    """The tier for this request at the current strictness, or None so the caller rounds up."""
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        return None
    try:
        state = {
            "system_prompt": text_of({"content": body.get("system")})[:2000],
            "request": prompt,
            "earlier_turns": len(body["messages"]) - 1,
            "tools_available": len(body.get("tools") or []),
        }
        response = await http.post(
            JEV_URL,
            headers={"Authorization": f"Bearer {JEV_KEY}"},
            json={"model": JEV_MODEL, "state": state, "questions": QUESTIONS},
            timeout=JEV_TIMEOUT_S,
        )
        response.raise_for_status()
        result = response.json()
        answers = result["answers"]
        task = answers["task"]["choice"]
        rung = store.current_rung(task, team)
        return JevResult(choose_tier(answers, *LADDER[rung]), answers, result["usage"]["input_tokens"], task, rung)
    except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Jev unavailable, rounding up: %r", exc)
        return None


async def decide(http: httpx.AsyncClient, body: dict, team: str, session_id: str | None) -> Decision:
    ceiling = CEILINGS.get(body["model"])
    if ceiling is None or not in_scope(body):
        return Decision(None, "passthrough")

    messages = body["messages"]
    # ponytail: compaction rewrites the first message, so it starts a new conversation that can route lower.
    # Only cached context is lost at that point anyway. Key on the session alone if the eval says otherwise.
    key = digest(team, session_id, normalized(text_of(messages[0])))
    typed = latest_prompt(messages)
    prompt = digest(normalized(typed)) if typed else None

    known = store.conversation(key)
    if known:
        tier = min(known["tier"], ceiling)
        if is_reask(known, prompt) and tier < ceiling:
            store.set_tier(key, tier + 1)
            return Decision(tier + 1, "escalated", key, prompt, task=known["task"], rung=known["rung"])
        return Decision(tier, "sticky", key, prompt, task=known["task"], rung=known["rung"])

    started = time.perf_counter()
    result = await ask_jev(http, body, typed, team)
    router_ms = (time.perf_counter() - started) * 1000
    if result is None:
        store.add_conversation(key, ceiling)
        return Decision(ceiling, "round_up", key, prompt, router_ms)

    tier = min(result.tier, ceiling)
    store.add_conversation(key, tier, result.task, result.rung)
    router_usd = result.input_tokens * JEV_INPUT / 1_000_000
    return Decision(tier, "routed", key, prompt, router_ms, router_usd, result.answers, result.task, result.rung)
