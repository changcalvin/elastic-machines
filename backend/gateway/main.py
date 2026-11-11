"""Drop-in Claude API gateway. Forwards everything it doesn't need to change, byte for byte, and
answers GPT and Gemini requests in the same Claude format."""
import hmac
import json
import os

import anyio
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import providers, shadow, store
from .policy import CEILINGS, LABEL_OF, LEVEL_OF, TIERS, Tier, model_label, provider_of
from .pricing import cost_usd
from .router import decide, text_of
from .sse import Meter, events

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://api.anthropic.com")
API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
TEAMS = dict(pair.split(":", 1) for pair in os.environ["GATEWAY_KEYS"].split(","))
if not ADMIN_TOKEN:
    raise SystemExit("ADMIN_TOKEN must not be empty")

# Everything else is forwarded: Claude Code's gateway docs say to treat headers as an open list.
DROP_REQUEST_HEADERS = {"host", "content-length", "connection", "authorization", "x-api-key", "accept-encoding"}
DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection", "date", "server"}
PROVIDER_NAMES = {"anthropic": "Claude", "openai": "OpenAI", "gemini": "Gemini"}

app = FastAPI(title="Elastic Machines")
http = httpx.AsyncClient(
    timeout=httpx.Timeout(600, connect=10),
    limits=httpx.Limits(max_connections=None, max_keepalive_connections=50),
)


def error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status)


def credential(request: Request) -> str:
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
    return request.headers.get("x-api-key") or bearer


def require_admin(request: Request) -> None:
    if not hmac.compare_digest(credential(request).encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(401, "invalid admin token")


def upstream_request(request: Request, content: bytes) -> httpx.Request:
    headers = [(k, v) for k, v in request.headers.items() if k not in DROP_REQUEST_HEADERS]
    headers += [("x-api-key", API_KEY), ("accept-encoding", "identity")]
    return http.build_request(
        "POST", UPSTREAM_URL + request.url.path, params=request.query_params, headers=headers, content=content
    )


def client_headers(upstream: httpx.Response) -> dict:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}


def rewrite(body: dict, tier: Tier) -> bytes:
    out = {**body, "model": tier.model}
    if tier.effort:
        out["output_config"] = {**body.get("output_config", {}), "effort": tier.effort}
    return json.dumps(out).encode()


@app.post("/v1/messages")
async def messages(request: Request):
    team = TEAMS.get(credential(request))
    if team is None:
        return error(401, "authentication_error", "invalid gateway key")
    raw = await request.body()
    try:
        body = json.loads(raw)
    except ValueError:
        return error(400, "invalid_request_error", "request body is not valid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        return error(400, "invalid_request_error", "model: field required")

    requested = body["model"]
    provider = provider_of(requested)
    claude = provider == "anthropic"
    if not claude and not providers.KEYS[provider]:
        return error(400, "invalid_request_error", f"{requested} needs {provider.upper()}_API_KEY set on the gateway")
    if not claude and not providers.text_only(body):
        return error(400, "invalid_request_error", f"{requested} only takes text conversations through this gateway")

    tiers = TIERS[provider]
    ceiling = CEILINGS.get(requested)
    session = request.headers.get("x-claude-code-session-id")
    decision = await decide(http, body, team, session)
    routed = decision.tier is not None and decision.tier != ceiling
    model = tiers[decision.tier].model if routed else requested

    def outgoing(cheaper: bool, stream: bool | None = None) -> httpx.Request:
        """This request in its provider's format, on the routed model or as received."""
        sent = body if stream is None else {**body, "stream": stream}
        if not claude:
            return providers.build(http, provider, sent, tiers[decision.tier].model if cheaper else requested)
        if cheaper:
            return upstream_request(request, rewrite(sent, tiers[decision.tier]))
        return upstream_request(request, raw if stream is None else json.dumps(sent).encode())

    check = None
    if shadow.wanted(body, decision.tier, ceiling):
        check = shadow.Check(
            request=outgoing(False, stream=True),
            body=body,
            team=team,
            task=decision.task,
            rung=decision.rung,
            tier=tiers[decision.tier].name,
            requested=requested,
            provider=provider,
        )

    def finish(status: int, usage: dict, stop_reason: str | None, answer: str = "") -> None:
        store.log(
            team=team,
            session=session,
            conversation=decision.key,
            requested_model=requested,
            model=model,
            tier=None if decision.tier is None else tiers[decision.tier].name,
            route=decision.route,
            answers=decision.answers,
            router_ms=decision.router_ms,
            router_usd=decision.router_usd,
            status=status,
            stop_reason=stop_reason,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_write_tokens=usage.get("cache_creation_input_tokens"),
            cost_usd=cost_usd(model, usage),
            # ponytail: same token counts at the requested model's rates. The eval measures the true counterfactual.
            baseline_usd=cost_usd(requested, usage),
        )
        if status == 200 and stop_reason and decision.key and decision.prompt:
            store.answered(decision.key, decision.prompt)
        # Only after the answer is complete, so failed or abandoned requests cost nothing extra.
        if check and model != requested and stop_reason == "end_turn" and answer:
            shadow.start(http, check, answer, usage)

    try:
        upstream = await http.send(outgoing(routed), stream=True)
        if upstream.status_code in (400, 404) and routed:
            # A cheaper model rejected something the requested one accepts. Round up and resend as received.
            await upstream.aclose()
            store.set_tier(decision.key, ceiling)
            decision = decision._replace(tier=ceiling, route="retried")
            model, routed = requested, False
            upstream = await http.send(outgoing(False), stream=True)

        if upstream.status_code != 200 or not body.get("stream"):
            content = await upstream.aread()
            usage, stop_reason, answer = {}, None, ""
            if upstream.status_code == 200:
                message = json.loads(content) if claude else providers.claude_message(provider, json.loads(content))
                usage, stop_reason = message.get("usage") or {}, message.get("stop_reason")
                answer = text_of(message)
                message["model"] = requested
                content = json.dumps(message).encode()
            elif not claude:
                content = providers.claude_error(upstream.status_code, content)
            finish(upstream.status_code, usage, stop_reason, answer)
            headers = client_headers(upstream) if claude else {"content-type": "application/json"}
            return Response(content, upstream.status_code, headers)
    except httpx.HTTPError:
        finish(502, {}, None)
        return error(502, "api_error", f"could not reach the {PROVIDER_NAMES[provider]} API")

    source = events(upstream) if claude else providers.claude_events(provider, upstream)

    async def relay():
        meter = Meter(requested, capture=check is not None)
        try:
            async for event in source:
                yield meter.read(event) + b"\n\n"
        finally:
            finish(200, meter.usage, meter.stop_reason, "".join(meter.text))
            with anyio.CancelScope(shield=True):  # a cancelled client must not leak the upstream connection
                await upstream.aclose()

    if claude:
        return StreamingResponse(relay(), headers=client_headers(upstream))
    return StreamingResponse(relay(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    if credential(request) not in TEAMS:
        return error(401, "authentication_error", "invalid gateway key")
    try:
        upstream = await http.send(upstream_request(request, await request.body()))
    except httpx.HTTPError:
        return error(502, "api_error", "could not reach the Claude API")
    return Response(upstream.content, upstream.status_code, client_headers(upstream))


@app.get("/v1/models")
async def models(request: Request):
    """The models a client can ask for and have routed, from every provider with a key set."""
    if credential(request) not in TEAMS:
        return error(401, "authentication_error", "invalid gateway key")
    data = [
        {"type": "model", "id": model, "display_name": model_label(model)}
        for model in CEILINGS
        if provider_of(model) in providers.configured()
    ]
    return {"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}


@app.get("/usage/{session}")
async def usage(session: str, request: Request):
    """One chat's requests, for its savings page. A gateway key only sees its own team's sessions."""
    team = TEAMS.get(credential(request))
    if team is None:
        return error(401, "authentication_error", "invalid gateway key")
    return [
        {
            **row,
            "answered_by": LABEL_OF.get(row["tier"]) or model_label(row["model"]),
            "level": LEVEL_OF.get(row["tier"]),
            "asked_for": model_label(row["requested_model"]),
        }
        for row in store.session_requests(team, session)
    ]


@app.get("/admin/summary", dependencies=[Depends(require_admin)])
async def admin_summary():
    return store.summary()


@app.get("/admin/quality", dependencies=[Depends(require_admin)])
async def admin_quality():
    return {**store.quality(), "sample_rate": shadow.RATE, "daily_budget_usd": shadow.DAILY_BUDGET_USD}


@app.get("/admin/requests", dependencies=[Depends(require_admin)])
async def admin_requests(limit: int = Query(100, ge=1, le=500)):
    return store.recent(limit)
