"""Fake Jev and Claude API behind httpx.MockTransport. Run with: pytest"""
import asyncio
import json
import os

os.environ.update(
    ANTHROPIC_API_KEY="sk-upstream",
    MODEL_ACCESS_KEY="jev-key",
    ADMIN_TOKEN="admin-token",
    GATEWAY_KEYS="good-key:platform,other-key:support",
    DB_PATH=":memory:",
    SHADOW_RATE="0",
)

import random
import subprocess
import sys

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic

from gateway import judge, main, shadow, store, tuning
from gateway.policy import CONFIDENCE, TIERS, choose_tier
from gateway.pricing import cost_usd
from gateway.router import JEV_URL
from gateway.sse import Meter
from gateway.tuning import DEFAULT_RUNG, LADDER, TOP_RUNG, Segment, observe, wilson


def jev_answers(levels=(1, 0, 0, 0), task="transform", stakes=0.0):
    return {
        "complexity": {"type": "score", "probabilities": {str(i): p for i, p in enumerate(levels)}},
        "task": {"type": "choice", "choice": task, "probabilities": {task: 1}},
        "stakes": {"type": "noul", "noul": stakes},
    }


class FakeNetwork:
    """Stands in for both Jev and the Claude API, and records what each received."""

    def __init__(self, answers=None):
        self.answers = answers or jev_answers()
        self.jev_status = 200
        self.stop_reason = "end_turn"
        self.claude_status = [200]  # consumed one per call; the last value repeats
        self.jev_calls = []
        self.claude_calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == JEV_URL:
            self.jev_calls.append(json.loads(request.content))
            return httpx.Response(self.jev_status, json={"answers": self.answers, "usage": {"input_tokens": 1000}})

        self.claude_calls.append(request)
        status = self.claude_status.pop(0) if len(self.claude_status) > 1 else self.claude_status[0]
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"message": "nope"}}, headers={"retry-after": "7"})
        body = json.loads(request.content)
        stop = self.stop_reason
        if body.get("stream"):
            return httpx.Response(200, content=chunked(sse(body["model"], stop)), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "content": [{"type": "text", "text": f"answer from {body['model']}"}],
                "stop_reason": stop,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    def sent(self, index=-1) -> dict:
        return json.loads(self.claude_calls[index].content)


def sse(model: str, stop_reason: str = "end_turn") -> bytes:
    delta = {"type": "text_delta", "text": f"answer from {model}"}
    events = [
        ("message_start", {"message": {"model": model, "usage": {"input_tokens": 100, "output_tokens": 1}}}),
        ("ping", {"type": "ping"}),
        ("content_block_delta", {"delta": delta}),
        ("message_delta", {"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 50}}),
        ("message_stop", {}),
    ]
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)


async def chunked(data: bytes, size: int = 7):
    """Network chunks don't line up with SSE events."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


class FakeJudge:
    """Answers the SDK's call. `winner` is 'routed', 'reference' or 'same'; the test never needs to
    know which side was labelled A, because the judge reads the answers it's given."""

    def __init__(self):
        self.winner = "same"
        self.status = 200
        self.calls = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"type": "error", "error": {"type": "api_error", "message": "x"}})
        prompt = self.calls[-1]["messages"][0]["content"]
        first = prompt.split("<answer_a>")[1].split("</answer_a>")[0]
        routed_side = "B" if "claude-opus-5-5" in first else "A"
        better = {"same": "same", "routed": routed_side, "reference": "A" if routed_side == "B" else "B"}[self.winner]
        return httpx2.Response(
            200,
            json={
                "id": "msg_judge",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps({"better": better})}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 400, "output_tokens": 20},
            },
        )


@pytest.fixture
def net():
    for table in ("conversations", "requests", "checks", "segments", "moves"):
        store.db.execute(f"DELETE FROM {table}")
    fake = FakeNetwork()
    main.http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return fake


@pytest.fixture
def checked(net, monkeypatch):
    """Every eligible answer gets a quality check."""
    monkeypatch.setattr(shadow, "RATE", 1.0)
    monkeypatch.setattr(shadow, "DAILY_BUDGET_USD", 25.0)
    fake = FakeJudge()
    monkeypatch.setattr(
        judge,
        "client",
        AsyncAnthropic(api_key="x", base_url="http://judge.test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake))),
    )
    return fake


def call(method: str, path: str, **kwargs) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            response = await client.request(method, path, **kwargs)
        await shadow.drain()
        return response

    return asyncio.run(go())


def ask(prompt="reword this email", model="claude-opus-5-5", key="good-key", headers=None, **fields) -> httpx.Response:
    body = {"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": prompt}], **fields}
    return call("POST", "/v1/messages", json=body, headers={"x-api-key": key, **(headers or {})})


def last_logged() -> dict:
    return dict(store.db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone())


@pytest.mark.parametrize(
    "answers, expected",
    [
        (jev_answers((0.95, 0.05, 0, 0)), 0),
        (jev_answers((0.5, 0.45, 0.05, 0)), 1),  # torn between two tiers: rounds up
        (jev_answers((0.5, 0.3, 0.2, 0)), 2),
        (jev_answers(task="code"), 1),
        (jev_answers(task="analysis"), 2),
        (jev_answers(stakes=0.5), 1),
        (jev_answers((0, 0, 0, 1), stakes=0.9), 3),  # never past the top tier
    ],
)
def test_policy_rounds_up(answers, expected):
    assert choose_tier(answers) == expected


def test_lower_confidence_bar_routes_lower():
    answers = jev_answers((0.7, 0.25, 0.05, 0))
    assert choose_tier(answers, confidence=0.6) == 0
    assert choose_tier(answers, confidence=0.9) == 1


def test_cost_uses_the_long_prompt_haiku_rates_and_cache_rates():
    assert cost_usd("claude-haiku-5-5", {"input_tokens": 1_000_000}) == pytest.approx(0.50 * 1)
    assert cost_usd("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 1000}) == pytest.approx(0.0105)
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 2000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 1000},
    }
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx((4 + 0.2 + 4 * 1.25 + 4 * 2) / 1000)
    assert cost_usd("claude-opus-4-8", usage) is None


def test_routes_down_and_keeps_the_real_model_invisible(net):
    response = ask()
    assert net.sent()["model"] == "claude-haiku-5-5"
    assert net.sent()["output_config"]["effort"] == "medium"
    assert response.json()["model"] == "claude-opus-5-5"

    logged = last_logged()
    assert (logged["tier"], logged["route"], logged["requested_model"]) == ("haiku", "routed", "claude-opus-5-5")
    assert logged["cost_usd"] == pytest.approx((100 * 0.10 + 50 * 0.50) / 1e6)
    assert logged["baseline_usd"] == pytest.approx((100 * 4 + 50 * 20) / 1e6)
    assert logged["router_usd"] == pytest.approx(1000 * 0.042 / 1e6)


def test_streaming_relays_every_event_and_hides_the_model(net):
    response = ask(stream=True)
    assert response.headers["content-type"].startswith("text/event-stream")
    events = response.content.split(b"\n\n")
    assert [e.split(b"\n")[0].removeprefix(b"event: ").decode() for e in events[:-1]] == [
        "message_start", "ping", "content_block_delta", "message_delta", "message_stop",
    ]
    assert b'"model": "claude-haiku-5-5"' not in response.content
    assert b'"model": "claude-opus-5-5"' in events[0]

    logged = last_logged()
    assert (logged["input_tokens"], logged["output_tokens"], logged["stop_reason"]) == (100, 50, "end_turn")


def test_effort_keeps_other_output_config_fields(net):
    ask(output_config={"effort": "xhigh", "format": {"type": "json_schema"}})
    assert net.sent()["output_config"] == {"effort": "medium", "format": {"type": "json_schema"}}


def test_sonnet_request_is_never_sent_above_sonnet(net):
    net.answers = jev_answers((0, 0, 0, 1))
    ask(model="claude-sonnet-5-5")
    assert net.sent()["model"] == "claude-sonnet-5-5"


def test_conversation_stays_on_one_model_without_asking_jev_again(net):
    # Claude Code moves cache_control between turns, so the first message's JSON changes while its text doesn't.
    first = {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}
    ask(messages=[first])
    ask(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}])
    assert len(net.jev_calls) == 1
    assert net.sent()["model"] == "claude-haiku-5-5"
    assert last_logged()["route"] == "sticky"


def test_same_first_message_in_two_sessions_routes_separately(net):
    ask(headers={"x-claude-code-session-id": "a"})
    ask(headers={"x-claude-code-session-id": "b"})
    assert len(net.jev_calls) == 2


def test_regenerate_escalates_one_tier_and_logs_it(net):
    ask()
    ask("  Reword THIS email ")
    assert net.sent()["output_config"]["effort"] == "high"
    assert last_logged()["route"] == "escalated"
    assert last_logged()["tier"] == "haiku-high"

    ask()
    assert net.sent()["model"] == "claude-sonnet-5-5"
    assert "output_config" not in net.sent()


def test_client_retry_after_a_failure_is_not_a_reask(net):
    net.claude_status = [529, 200]
    ask()
    assert last_logged()["status"] == 529
    ask()
    assert last_logged()["route"] == "sticky"
    assert last_logged()["tier"] == "haiku"


def test_tool_loop_turns_are_not_reasks(net):
    turn = [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "run", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
    ]
    for _ in range(3):
        ask(messages=turn)
    assert last_logged()["route"] == "sticky"


def test_system_reminder_after_the_prompt_does_not_hide_a_reask(net):
    messages = [{"role": "user", "content": "reword this"}, {"role": "system", "content": "reminder"}]
    for _ in range(2):
        ask(messages=messages)
    assert last_logged()["route"] == "escalated"


def test_jev_failure_rounds_up_to_the_requested_model(net):
    net.jev_status = 500
    ask()
    assert net.sent()["model"] == "claude-opus-5-5"
    assert last_logged()["route"] == "round_up"


def test_malformed_jev_answer_rounds_up(net):
    net.answers = {"complexity": {"type": "score"}}
    ask()
    assert last_logged()["route"] == "round_up"


def test_oversized_prompt_skips_jev_and_rounds_up(net):
    ask("x" * 20_001)
    assert net.jev_calls == []
    assert net.sent()["model"] == "claude-opus-5-5"


def test_cheaper_model_rejection_resends_the_original_bytes(net):
    net.claude_status = [400, 200]
    response = ask(tools=[{"type": "web_search_20260209", "name": "web_search"}])
    assert response.status_code == 200
    assert net.sent(0)["model"] == "claude-haiku-5-5"
    assert net.sent(1)["model"] == "claude-opus-5-5"
    assert "output_config" not in net.sent(1)
    assert last_logged()["route"] == "retried"
    assert last_logged()["tier"] == "opus"

    ask(tools=[{"type": "web_search_20260209", "name": "web_search"}])
    assert net.sent()["model"] == "claude-opus-5-5"
    assert len(net.claude_calls) == 3


def test_upstream_errors_reach_the_client_unchanged(net):
    net.claude_status = [429]
    response = ask(model="claude-haiku-5-5")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json() == {"type": "error", "error": {"message": "nope"}}


def test_unrouted_models_and_images_pass_through_as_received(net):
    ask(model="claude-fable-5-1")
    assert net.sent()["model"] == "claude-fable-5-1"
    assert last_logged()["route"] == "passthrough"

    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
    body = '{"model":   "claude-opus-5-5", "max_tokens": 9, "messages": [{"role": "user", "content": [%s]}]}' % json.dumps(image)
    call("POST", "/v1/messages", content=body, headers={"x-api-key": "good-key"})
    assert net.claude_calls[-1].content == body.encode()
    assert net.jev_calls == []


def test_gateway_key_is_swapped_and_anthropic_headers_are_forwarded(net):
    ask(headers={"anthropic-beta": "new-capability-2099", "authorization": "Bearer also-client"})
    sent = net.claude_calls[-1]
    assert sent.headers["x-api-key"] == "sk-upstream"
    assert "authorization" not in sent.headers
    assert sent.headers["anthropic-beta"] == "new-capability-2099"


def test_query_string_is_forwarded(net):
    body = {"model": "claude-opus-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    call("POST", "/v1/messages?beta=true", json=body, headers={"x-api-key": "good-key"})
    assert net.claude_calls[-1].url.query == b"beta=true"


@pytest.mark.parametrize(
    "fields",
    [
        {"messages": [{"role": "user", "content": 5}]},
        {"messages": [{"role": "user"}]},
        {"system": 7},
        {"system": [7, {"type": "text"}]},
        {"tools": 3},
        {"output_config": "high"},
        {"messages": ["not a message"]},
        {"messages": []},
    ],
)
def test_malformed_bodies_get_the_claude_api_error_instead_of_crashing_the_gateway(net, fields):
    net.claude_status = [400]
    response = ask(**fields)
    assert response.status_code == 400
    assert response.json() == {"type": "error", "error": {"message": "nope"}}
    assert net.sent()["model"] == "claude-opus-5-5"


def test_rejects_bad_credentials(net):
    assert ask(key="wrong").status_code == 401
    assert net.claude_calls == []
    assert call("GET", "/admin/summary").status_code == 401
    assert call("GET", "/admin/summary", headers={"x-api-key": "good-key"}).status_code == 401


def test_client_errors_get_the_anthropic_error_shape(net):
    response = call("POST", "/v1/messages", content="not json", headers={"x-api-key": "good-key"})
    assert response.status_code == 400
    assert response.json()["type"] == "error"


def test_admin_summary_rolls_up_by_team(net):
    ask()
    ask()
    ask("something else")
    ask(model="claude-opus-4-8")
    response = call("GET", "/admin/summary", headers={"authorization": "Bearer admin-token"})
    total = response.json()["total"]
    assert total["requests"] == 4
    assert total["unpriced"] == 1
    assert total["conversations"] == 2
    assert total["escalated"] == 1
    assert total["baseline_usd"] > total["spend_usd"] > 0
    assert response.json()["teams"][0]["tiers"] == {"haiku": 2, "haiku-high": 1}

    rows = call("GET", "/admin/requests?limit=2", headers={"x-api-key": "admin-token"}).json()
    assert [row["requested_model"] for row in rows] == ["claude-opus-4-8", "claude-opus-5-5"]
    assert "prompt" not in rows[0]


def test_a_chat_sees_only_its_own_session_costs(net):
    ask(headers={"x-claude-code-session-id": "chat-1"})
    ask("something else", headers={"x-claude-code-session-id": "chat-2"})
    ask(key="other-key", headers={"x-claude-code-session-id": "chat-1"})  # another team reusing the id
    rows = call("GET", "/usage/chat-1", headers={"x-api-key": "good-key"}).json()
    assert len(rows) == 1
    assert (rows[0]["tier"], rows[0]["route"]) == ("haiku", "routed")
    assert rows[0]["baseline_usd"] > rows[0]["cost_usd"] > 0
    assert call("GET", "/usage/chat-1", headers={"x-api-key": "nope"}).status_code == 401


def test_tiers_line_up_with_jev_levels():
    from gateway.policy import QUESTIONS

    assert len(QUESTIONS["complexity"]["criteria"]) == len(TIERS)


def test_a_bad_stream_event_does_not_break_the_relay():
    meter = Meter("claude-opus-5-5")
    broken = b'event: message_start\ndata: {"truncated'
    assert meter.read(broken) == broken
    assert meter.read(b"event: message_delta") == b"event: message_delta"


# The feedback loop


def run_verdicts(rate, checks, segment=None, seed=1):
    rng = random.Random(seed)
    segment = segment or Segment()
    start = segment.rung
    for _ in range(checks):
        segment = observe(segment, rng.random() < rate)
        if segment.rung != start:
            break
    return segment


def test_default_rung_is_the_policys_default():
    assert LADDER[DEFAULT_RUNG] == (CONFIDENCE, 0)


def test_top_rung_keeps_cheaper_than_opus_traffic_to_measure():
    assert LADDER[TOP_RUNG][1] < len(TIERS) - 1


def test_a_clear_quality_problem_tightens_quickly():
    moved = run_verdicts(0.25, 40)
    assert moved.rung == DEFAULT_RUNG + 1
    assert (moved.tighten, moved.loosen, moved.checks) == (0, 0, 0)


def test_healthy_traffic_does_not_tighten():
    tightened = sum(run_verdicts(0.01, 1000, seed=seed).rung > DEFAULT_RUNG for seed in range(200))
    assert tightened <= 2


def test_loosening_needs_a_long_clean_run():
    segment = Segment()
    for _ in range(390):
        segment = observe(segment, False)
    assert segment.rung == DEFAULT_RUNG
    assert run_verdicts(0.0, 500).rung == DEFAULT_RUNG - 1


def test_a_single_miss_sets_loosening_back():
    segment = Segment()
    for _ in range(300):
        segment = observe(segment, False)
    assert observe(segment, True).loosen < segment.loosen - 0.5


def test_ladder_has_ends():
    segment = Segment(rung=TOP_RUNG)
    for _ in range(50):
        segment = observe(segment, True)
    assert segment.rung == TOP_RUNG
    assert segment.tighten == tuning.TIGHTEN_AT

    segment = Segment(rung=0)
    for _ in range(2000):
        segment = observe(segment, False)
    assert segment.rung == 0


def test_every_rung_is_at_least_as_strict_as_the_last():
    answers = jev_answers((0.5, 0.3, 0.15, 0.05))
    tiers = [choose_tier(answers, confidence, floor) for confidence, floor in LADDER]
    assert tiers == sorted(tiers)
    assert tiers[-1] > tiers[0]


def test_a_tier_floor_works_when_jev_is_certain():
    assert choose_tier(jev_answers((1, 0, 0, 0)), 0.99, 2) == 2


def test_wilson_interval():
    assert wilson(0, 0) == (0.0, 1.0)
    low, high = wilson(0, 100)
    assert low == 0.0 and high == pytest.approx(0.0370, abs=0.0005)
    low, high = wilson(10, 100)
    assert low == pytest.approx(0.0552, abs=0.0005) and high == pytest.approx(0.1744, abs=0.0005)


# Shadow checks


def check_rows():
    return [dict(row) for row in store.db.execute("SELECT * FROM checks ORDER BY id")]


def test_check_compares_the_routed_answer_with_the_requested_model(net, checked):
    response = ask()
    assert response.json()["model"] == "claude-opus-5-5"

    shadow_call = net.sent()
    assert shadow_call["model"] == "claude-opus-5-5"
    assert shadow_call["stream"] is True
    assert "output_config" not in shadow_call
    assert net.claude_calls[-1].headers["x-api-key"] == "sk-upstream"

    prompt = checked.calls[0]["messages"][0]["content"]
    assert "answer from claude-haiku-5-5" in prompt and "answer from claude-opus-5-5" in prompt
    assert "reword this email" in prompt

    row = check_rows()[0]
    assert (row["verdict"], row["task"], row["tier"], row["team"]) == ("same", "transform", "haiku", "platform")
    assert row["shadow_usd"] == pytest.approx((100 * 4 + 50 * 20) / 1e6)
    assert row["judge_usd"] == pytest.approx((400 * 4 + 20 * 20) / 1e6)


def test_judge_sdk_request_is_structured_and_blind(net, checked):
    ask()
    sent = checked.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["fallbacks"] == "default"
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "haiku" not in sent["system"].lower()


@pytest.mark.parametrize("winner, verdict", [("routed", "better"), ("reference", "worse"), ("same", "same")])
def test_verdict_maps_to_the_right_answer_whichever_side_it_was_on(net, checked, winner, verdict):
    checked.winner = winner
    sides = set()
    for n in range(12):
        ask(f"request {n}")
        prompt = checked.calls[-1]["messages"][0]["content"]
        sides.add("haiku" in prompt.split("</answer_a>")[0])
    assert sides == {True, False}  # the cheap answer really did appear on both sides
    assert {row["verdict"] for row in check_rows()} == {verdict}


def test_streamed_answers_are_checked_too(net, checked):
    ask(stream=True)
    assert "answer from claude-haiku-5-5" in checked.calls[0]["messages"][0]["content"]
    assert check_rows()[0]["verdict"] == "same"


def test_failed_check_never_reaches_the_employee(net, checked):
    checked.status = 500
    response = ask()
    assert response.status_code == 200
    assert response.json()["model"] == "claude-opus-5-5"
    assert check_rows()[0]["verdict"] == "failed"
    assert check_rows()[0]["shadow_usd"] > 0  # the shadow call was still paid for


def test_unreachable_upstream_during_a_check_is_recorded(net, checked):
    net.claude_status = [200, 500]
    assert ask().status_code == 200
    assert check_rows()[0]["verdict"] == "no_reference"
    assert checked.calls == []


def test_prompt_and_answers_are_never_stored(net, checked):
    ask("the quarterly numbers are confidential")
    for table in ("conversations", "requests", "checks", "segments", "moves"):
        for row in store.db.execute(f"SELECT * FROM {table}"):
            stored = json.dumps([str(value) for value in tuple(row)])
            assert "confidential" not in stored and "answer from" not in stored


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": [{"type": "web_search_20260209", "name": "web_search"}]},
        {"tools": [{"type": "code_execution_20260521", "name": "code_execution"}]},
        {"mcp_servers": [{"type": "url", "url": "https://tickets.example", "name": "tickets"}]},
        {"container": "container_1"},
    ],
)
def test_server_side_tools_are_never_shadowed_because_they_would_really_run(net, checked, fields):
    ask(**fields)
    assert check_rows() == []
    assert len(net.claude_calls) == 1


def test_client_tools_are_shadowed_because_nobody_runs_the_shadow_call(net, checked):
    ask(tools=[{"name": "run_query", "input_schema": {"type": "object"}}])
    assert len(check_rows()) == 1


def test_only_finished_cheaper_answers_are_checked(net, checked):
    net.answers = jev_answers((0, 0, 0, 1))
    ask("hard")  # already on the requested model
    assert check_rows() == []

    net.answers = jev_answers()
    net.stop_reason = "tool_use"
    ask("uses a tool")
    net.stop_reason = "max_tokens"
    ask("cut off")
    assert check_rows() == []

    net.stop_reason = "end_turn"
    net.claude_status = [529, 200]
    ask("fails")
    assert check_rows() == []


def test_a_rejected_cheap_request_is_not_checked(net, checked):
    net.claude_status = [400, 200]
    ask()
    assert len(net.claude_calls) == 2
    assert check_rows() == []


def test_long_prompts_are_skipped_because_shadow_calls_miss_the_cache(net, checked, monkeypatch):
    monkeypatch.setattr(shadow, "MAX_PROMPT_TOKENS", 99)
    ask()
    assert check_rows() == []


def test_daily_budget_stops_checks(net, checked, monkeypatch):
    ask("first")
    assert len(check_rows()) == 1
    monkeypatch.setattr(shadow, "DAILY_BUDGET_USD", store.spent_today())
    ask("second")
    assert len(check_rows()) == 1


def test_a_verdict_moves_the_router_end_to_end(net, checked, monkeypatch):
    checked.winner = "reference"
    monkeypatch.setattr(tuning, "TIGHTEN_AT", 2.0)
    for n in range(3):
        ask(f"request {n}")

    moves = [dict(row) for row in store.db.execute("SELECT * FROM moves")]
    assert moves[0]["segment"] == "task:transform"
    assert (moves[0]["from_rung"], moves[0]["to_rung"]) == (DEFAULT_RUNG, DEFAULT_RUNG + 1)
    assert moves[0]["worse"] == 2
    assert store.segment("task:transform").rung == DEFAULT_RUNG + 1


def test_tightening_changes_what_the_next_conversation_gets(net):
    net.answers = jev_answers((0.5, 0.5, 0, 0))
    ask("before")
    assert net.sent()["output_config"]["effort"] == "high"

    net.answers = jev_answers((0.93, 0.07, 0, 0))
    ask("easy")
    assert net.sent()["output_config"]["effort"] == "medium"

    store.save_segment("task:transform", Segment(rung=TOP_RUNG))
    ask("easy again")
    assert net.sent()["model"] == "claude-sonnet-5-5"


def test_a_conversation_already_under_way_keeps_its_model(net):
    ask("hi")
    store.save_segment("task:transform", Segment(rung=TOP_RUNG))
    ask(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x"}, {"role": "user", "content": "more"}])
    assert net.sent()["model"] == "claude-haiku-5-5"


def test_evidence_from_an_older_setting_is_ignored(net, checked):
    checked.winner = "reference"
    conversation = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x"}, {"role": "user", "content": "more"}]
    ask("hi")
    assert store.segment("task:transform").worse == 1

    # The setting moves while this conversation carries on under the old one.
    store.save_segment("task:transform", Segment(rung=DEFAULT_RUNG + 1))
    ask(messages=conversation)
    assert len(check_rows()) == 2  # still counted in the quality estimate
    assert store.segment("task:transform").worse == 0  # but not fed to the tuner


def test_a_task_move_clears_team_evidence_so_one_cause_is_not_punished_twice(net, checked, monkeypatch):
    checked.winner = "reference"
    store.save_segment("team:platform", Segment(tighten=1.5))
    monkeypatch.setattr(tuning, "TIGHTEN_AT", 1.0)
    ask()
    assert store.segment("task:transform").rung == DEFAULT_RUNG + 1
    assert store.segment("team:platform") == Segment()


def test_a_team_problem_tightens_that_team_only(net, checked, monkeypatch):
    checked.winner = "reference"
    monkeypatch.setattr(tuning, "TIGHTEN_AT", 3.0)
    monkeypatch.setattr(tuning, "evidence", lambda rate, worse: 1.0 if worse else -0.05)
    for task in ("transform", "answer", "code"):  # spread across tasks, so only the team stands out
        net.answers = jev_answers(task=task)
        ask(f"a {task} request")
    assert store.segment("team:platform").rung == DEFAULT_RUNG + 1
    assert store.segment("team:support").rung == DEFAULT_RUNG
    assert store.segment("task:transform").rung == DEFAULT_RUNG

    net.answers = jev_answers((0.92, 0.05, 0.03, 0))  # clears the default bar, not the stricter one
    ask("from support", key="other-key")
    support = json.loads(net.claude_calls[-2].content)  # the last call is the shadow check
    ask("from platform", key="good-key")
    platform = json.loads(net.claude_calls[-2].content)
    assert support["output_config"]["effort"] == "medium"
    assert platform["output_config"]["effort"] == "high"


def test_savings_include_what_the_checks_cost(net, checked):
    ask()
    response = call("GET", "/admin/summary", headers={"x-api-key": "admin-token"}).json()
    assert response["total"]["checks_usd"] == pytest.approx(check_rows()[0]["shadow_usd"] + check_rows()[0]["judge_usd"])
    assert response["teams"][0]["checks_usd"] == response["total"]["checks_usd"]


def test_admin_quality_reports_the_estimate_trend_and_moves(net, checked, monkeypatch):
    monkeypatch.setattr(store, "ROLLING_CHECKS", 3)
    monkeypatch.setattr(tuning, "TIGHTEN_AT", 2.0)
    for n in range(4):
        checked.winner = "reference" if n < 3 else "same"
        ask(f"request {n}")

    assert call("GET", "/admin/quality").status_code == 401
    report = call("GET", "/admin/quality", headers={"x-api-key": "admin-token"}).json()
    assert report["target"] == 0.02
    assert report["sample_rate"] == 1.0
    assert report["overall"]["checks"] == 4
    assert report["overall"]["worse"] == 3
    assert report["overall"]["quality"] == 0.25
    assert report["overall"]["low"] < 0.25 < report["overall"]["high"]
    assert [point["check"] for point in report["trend"]] == [3, 4]
    assert report["trend"][0]["quality"] == 0
    assert report["trend"][-1]["quality"] == pytest.approx(1 / 3)
    assert report["moves"][0]["position"] == 2
    assert report["spent_today_usd"] > 0
    transform = next(s for s in report["segments"] if s["name"] == "transform")
    assert transform["kind"] == "task"
    assert transform["rung"] > transform["default_rung"]
    assert transform["checks"] == 4
    assert next(s for s in report["segments"] if s["name"] == "platform")["kind"] == "team"


def test_drift_demo_detects_and_corrects(tmp_path):
    result = subprocess.run(
        [sys.executable, "simulate_drift.py", "--requests", "5000", "--rate", "0.5"],
        env={**os.environ, "DB_PATH": str(tmp_path / "sim.db")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "task:code" in result.stdout and "stricter" in result.stdout
    latest = float(result.stdout.split("Latest rolling quality: ")[1].split("%")[0])
    lowest = float(result.stdout.split("Lowest rolling quality: ")[1].split("%")[0])
    assert lowest < 98 <= latest


def test_admin_quality_before_any_checks(net):
    report = call("GET", "/admin/quality", headers={"x-api-key": "admin-token"}).json()
    assert report["overall"]["checks"] == 0
    assert report["overall"]["quality"] is None
    assert report["trend"] == [] and report["moves"] == [] and report["segments"] == []
