"""Replays a simulated request stream through the real gateway code, with a drift partway through.

    DB_PATH=sim.db python simulate_drift.py             # fills sim.db for the admin view
    DB_PATH=:memory: python simulate_drift.py --sweep   # the sample rate trade-off table

Everything here is made up: Jev, Claude and the judge are stand-ins with miss rates chosen below.
It shows that the feedback loop detects and corrects a drift. It says nothing about how well real
models do. That's the eval's job, and until then the numbers in sim.db are a demo, not a result.
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import sys
from pathlib import Path

if __name__ == "__main__":
    if not os.environ.get("DB_PATH") or os.environ["DB_PATH"] == "gateway.db":
        raise SystemExit("Set DB_PATH to a separate file, such as DB_PATH=sim.db")
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--sweep", action="store_true")
    arguments.add_argument("--requests", type=int, default=9000)
    arguments.add_argument("--rate", type=float, default=0.25)
    arguments.add_argument("--seed", type=int, default=7)
    options = arguments.parse_args()
    if not options.sweep and Path(os.environ["DB_PATH"]).exists():
        raise SystemExit(f"{os.environ['DB_PATH']} already exists. Delete it or pick another DB_PATH")

TEAMS = ["Platform", "Support", "Sales ops", "Finance", "Legal"]
os.environ.update(
    ANTHROPIC_API_KEY="simulated",
    MODEL_ACCESS_KEY="simulated",
    ADMIN_TOKEN="simulated",
    GATEWAY_KEYS=",".join(f"{team}:{team}" for team in TEAMS),
)

import httpx
import httpx2
from anthropic import AsyncAnthropic

from gateway import judge, main, shadow, store
from gateway.router import JEV_URL
from gateway.tuning import DEFAULT_RUNG, Segment, observe

# (complexity probabilities Jev reports, task, share of traffic before and during the drift).
# Hard code is the drift: Jev mistakes it for routine, but with some spread, so a stricter bar helps.
CASES = {
    "easy_transform": ((0.93, 0.06, 0.01, 0.0), "transform", 0.42, 0.25),
    "routine_answer": ((0.30, 0.65, 0.05, 0.0), "answer", 0.30, 0.20),
    "routine_code": ((0.20, 0.72, 0.07, 0.01), "code", 0.27, 0.15),
    "hard_code": ((0.10, 0.82, 0.06, 0.02), "code", 0.01, 0.40),
}

# Chance the judge finds each tier worse than the requested model, by case.
MISS_RATE = {
    "easy_transform": {"haiku": 0.003, "haiku-high": 0.002, "sonnet": 0.001},
    "routine_answer": {"haiku": 0.02, "haiku-high": 0.005, "sonnet": 0.002},
    "routine_code": {"haiku": 0.04, "haiku-high": 0.006, "sonnet": 0.002},
    "hard_code": {"haiku": 0.45, "haiku-high": 0.28, "sonnet": 0.01},
}
EFFORTS = {"high": "haiku-high", "medium": "haiku"}


def tier_of(body: dict) -> str:
    model = body["model"]
    if "haiku" in model:
        return EFFORTS[body["output_config"]["effort"]]
    return "sonnet" if "sonnet" in model else "opus"


def world(rng: random.Random):
    """Stand-ins for Jev and the Claude API, plus the judge's handler."""

    def network(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if str(request.url) == JEV_URL:
            case = body["state"]["request"].split()[0]
            levels, task, *_ = CASES[case]
            answers = {
                "complexity": {"type": "score", "probabilities": {str(i): p for i, p in enumerate(levels)}},
                "task": {"type": "choice", "choice": task, "probabilities": {task: 1.0}},
                "stakes": {"type": "noul", "noul": 0.05},
            }
            return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 600}})

        case = body["messages"][-1]["content"].split()[0]
        text = f"{case} {tier_of(body)}"
        usage = {"input_tokens": rng.randint(800, 6000), "output_tokens": rng.randint(150, 1200)}
        if not body.get("stream"):
            message = {"model": body["model"], "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": usage}
            return httpx.Response(200, json=message)
        events = [
            ("message_start", {"message": {"model": body["model"], "usage": {"input_tokens": usage["input_tokens"]}}}),
            ("content_block_delta", {"delta": {"type": "text_delta", "text": text}}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": usage["output_tokens"]}}),
        ]
        content = b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    def judging(request: httpx2.Request) -> httpx2.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        first = prompt.split("<answer_a>\n")[1].split("\n")[0]
        second = prompt.split("<answer_b>\n")[1].split("\n")[0]
        case, tier = (second if first.endswith("opus") else first).split()
        reference_side = "A" if first.endswith("opus") else "B"
        better = reference_side if rng.random() < MISS_RATE[case][tier] else "same"
        return httpx2.Response(
            200,
            json={
                "id": "simulated",
                "type": "message",
                "role": "assistant",
                "model": judge.JUDGE_MODEL,
                "content": [{"type": "text", "text": json.dumps({"better": better})}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": rng.randint(500, 2500), "output_tokens": 60},
            },
        )

    return network, judging


def pick_case(rng: random.Random, drifting: bool) -> str:
    weights = [case[3 if drifting else 2] for case in CASES.values()]
    return rng.choices(list(CASES), weights)[0]


async def replay(requests: int, rate: float, seed: int) -> None:
    rng = random.Random(seed)
    random.seed(seed)
    network, judging = world(rng)
    main.http = httpx.AsyncClient(transport=httpx.MockTransport(network))
    judge.client = AsyncAnthropic(
        api_key="simulated", base_url="http://judge.simulated", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(judging))
    )
    shadow.RATE = rate
    shadow.DAILY_BUDGET_USD = float("inf")

    drift_start, drift_end = int(requests * 0.35), int(requests * 0.60)
    checks_at = {}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        for n in range(requests):
            if n in (drift_start, drift_end):
                checks_at[n] = store.db.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
            case = pick_case(rng, drift_start <= n < drift_end)
            body = {
                "model": "claude-opus-5-5",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": f"{case} request {n}"}],
            }
            await client.post("/v1/messages", json=body, headers={"x-api-key": rng.choice(TEAMS)})
            await shadow.drain()

    report = store.quality()
    print(f"Replayed {requests} simulated requests, checking {rate:.0%} of cheaper answers.")
    print(f"Drift ran from check {checks_at[drift_start]} to check {checks_at[drift_end]}.\n")
    for move in report["moves"]:
        direction = "stricter" if move["to_rung"] > move["from_rung"] else "looser"
        print(f"  check {move['position']:>5}: {move['segment']:<16} {direction}, rung {move['from_rung']} to {move['to_rung']}, {move['worse']} worse in {move['checks']} checks")
    first = next((m for m in report["moves"] if m["to_rung"] > m["from_rung"] and m["position"] > checks_at[drift_start]), None)
    if first:
        print(f"\nTightened {first['position'] - checks_at[drift_start]} checks after the drift began")
    lowest = min(report["trend"], key=lambda point: point["quality"])
    print(f"\nLowest rolling quality: {lowest['quality']:.1%} at check {lowest['check']}")
    print(f"Latest rolling quality: {report['trend'][-1]['quality']:.1%}")
    totals = store.summary()["total"]
    gross = totals["baseline_usd"] - totals["spend_usd"]
    print(f"Quality checks cost ${totals['checks_usd']:.2f}, {totals['checks_usd'] / gross:.1%} of gross savings at this demo rate")


def checks_to_detect(miss_rate: float, seed: int) -> int:
    rng = random.Random(seed)
    segment, checks = Segment(), 0
    while segment.rung == DEFAULT_RUNG and checks < 100_000:
        segment = observe(segment, rng.random() < miss_rate)
        checks += 1
    return checks


def sweep(requests_per_day: int = 10_000, eligible: float = 0.5) -> None:
    """More checks mean more confidence and faster detection, but they eat into savings. Assumes half
    of requests are eligible, and that a shadow call plus its judge costs two baseline requests."""
    for_margin = 753  # checks for a +/-1 point interval at a 2% miss rate
    median = {rate: statistics.median(checks_to_detect(rate, seed) for seed in range(500)) for rate in (0.25, 0.10, 0.06)}
    print(f"Assuming {requests_per_day:,} requests a day, {eligible:.0%} eligible.\n")
    print("Sample rate | Share of baseline spend | Share of a 60% saving | Days to +/-1 point | Days to catch 25% / 10% / 6% misses")
    for rate in (0.01, 0.02, 0.03, 0.05, 0.10):
        checks_per_day = requests_per_day * eligible * rate
        cost = rate * eligible * 2
        catch = " / ".join(f"{median[miss] / checks_per_day:.2f}" for miss in median)
        print(f"{rate:>11.0%} | {cost:>23.1%} | {cost / 0.60:>21.1%} | {for_margin / checks_per_day:>18.1f} | {catch}")


if __name__ == "__main__":
    if options.sweep:
        sweep()
    else:
        asyncio.run(replay(options.requests, options.rate, options.seed))
