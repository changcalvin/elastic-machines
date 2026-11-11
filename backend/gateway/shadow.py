"""Quietly re-asks the requested model for a sample of cheaper answers, has a judge compare the
two, and feeds the verdict to the tuner. Nothing here can touch the employee's response.
"""
import asyncio
import logging
import os
import random
from typing import NamedTuple

import httpx

from . import judge, providers, store
from .pricing import cost_usd
from .router import text_of
from .sse import Meter, events
from .tuning import observe

RATE = float(os.environ.get("SHADOW_RATE", "0.03"))
DAILY_BUDGET_USD = float(os.environ.get("SHADOW_DAILY_BUDGET_USD", "25"))
MAX_PROMPT_TOKENS = 50_000  # shadow calls miss the prompt cache, so long prompts are paid in full
MAX_RUNNING = 4  # stays out of production's way under load; extra samples are dropped, not queued
MAX_CONTEXT_CHARS = 12_000

logger = logging.getLogger(__name__)
running: set[asyncio.Task] = set()


class Check(NamedTuple):
    request: httpx.Request
    body: dict
    team: str
    task: str | None
    rung: int | None
    tier: str
    requested: str
    provider: str = "anthropic"


def harmless(body: dict) -> bool:
    """Client tools are safe to repeat because nobody runs a shadow call's tool requests. Server
    tools and MCP connectors run on the provider's side, so a shadow call would really act."""
    tools = body.get("tools") or []
    if body.get("mcp_servers") or body.get("container") or not isinstance(tools, list):
        return False
    return all(isinstance(tool, dict) and tool.get("type", "custom") == "custom" for tool in tools)


def wanted(body: dict, tier: int | None, ceiling: int | None) -> bool:
    return tier is not None and tier < ceiling and harmless(body) and random.random() < RATE


def start(http: httpx.AsyncClient, check: Check, answer: str, usage: dict) -> None:
    prompt = sum(usage.get(field) or 0 for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if prompt > MAX_PROMPT_TOKENS or len(running) >= MAX_RUNNING or store.spent_today() >= DAILY_BUDGET_USD:
        return
    task = asyncio.create_task(run(http, check, answer))
    running.add(task)
    task.add_done_callback(running.discard)


async def drain() -> None:
    while running:
        await asyncio.gather(*running)


async def reference_answer(http: httpx.AsyncClient, check: Check) -> tuple[str, float]:
    """The requested model's answer to the same request, and what it cost. Streamed like any long
    request, so large max_tokens values don't time out."""
    upstream = await http.send(check.request, stream=True)
    try:
        if upstream.status_code != 200:
            return "", 0.0
        meter = Meter(check.requested, capture=True)
        claude = check.provider == "anthropic"
        async for event in events(upstream) if claude else providers.claude_events(check.provider, upstream):
            meter.read(event)
    finally:
        await upstream.aclose()
    answered = meter.stop_reason == "end_turn"
    return ("".join(meter.text) if answered else ""), cost_usd(check.requested, meter.usage) or 0.0


def context(body: dict) -> str:
    """The conversation so far, as text for the judge. Held in memory only, never stored."""
    parts = [text_of({"content": body.get("system")})[:2000]]
    parts += [f"{message.get('role')}: {text_of(message)}" for message in body["messages"] if text_of(message)]
    return "\n\n".join(part for part in parts if part)[-MAX_CONTEXT_CHARS:]


async def run(http: httpx.AsyncClient, check: Check, answer: str) -> None:
    verdict, shadow_usd, judge_usd = "failed", 0.0, 0.0
    try:
        reference, shadow_usd = await reference_answer(http, check)
        if reference:
            result, usage = await judge.compare(context(check.body), answer, reference)
            judge_usd = cost_usd(judge.JUDGE_MODEL, usage) or 0.0
            verdict = result or "failed"
        else:
            verdict = "no_reference"
    except Exception as exc:  # a quality check must never reach the employee or crash the worker
        logger.warning("Quality check failed: %s", type(exc).__name__)

    check_id = store.log_check(
        team=check.team,
        task=check.task,
        tier=check.tier,
        requested_model=check.requested,
        rung=check.rung,
        verdict=verdict,
        shadow_usd=shadow_usd,
        judge_usd=judge_usd,
    )
    if verdict in ("worse", "same", "better"):
        learn(check, verdict == "worse", check_id)


def learn(check: Check, worse: bool, check_id: int) -> None:
    task_name, team_name = f"task:{check.task}", f"team:{check.team}"
    task, team = store.segment(task_name), store.segment(team_name)
    if check.rung != max(task.rung, team.rung):
        return  # routed under an older setting, so it says nothing about this one

    moved = observe(task, worse)
    store.save_segment(task_name, moved)
    if moved.rung != task.rung:
        store.log_move(task_name, task, moved, worse, check_id)
        store.clear_team_evidence()  # those misses are explained now; don't punish the team too
        return

    moved = observe(team, worse)
    store.save_segment(team_name, moved)
    if moved.rung != team.rung:
        store.log_move(team_name, team, moved, worse, check_id)
