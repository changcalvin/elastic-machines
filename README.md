# Elastic Machines

A drop-in gateway for the Claude API that sends each request to the cheapest model likely to answer it as well as the model that was asked for. Employees keep using their Claude tools as they do today, and an admin view shows the savings and the quality behind them.

> **Status:** one path works end to end against fake services. It hasn't run against the live Claude API or Jev yet, and the eval hasn't started, so there are no measured results yet. See [Current status](#current-status).

## The problem

Employees on a company Claude plan tend to send every request to the most capable model, and under usage-based billing the company pays top-tier rates for work a cheaper model would do just as well.

- **Usage is a direct cost.** Claude Enterprise is billed at $20 per seat per month plus usage at API rates, with no included token allowance ([pricing summary](https://justinmckelvey.com/blog/claude-enterprise-pricing), [usage details](https://www.gosearch.ai/faqs/what-is-claude-enterprise-pricing-in-2026/)).
- **The price gap is large.** Opus 5.5 costs 40 times as much per token as Haiku 5.5.
- **Most requests probably don't need the top model.** Rewording an email or summarizing a short document are tasks a smaller model handles well. People default to the strongest model because choosing is friction and nobody wants a worse answer. How large this share is in real traffic is the project's central hypothesis.

| Model | Input ($ per 1M tokens) | Output ($ per 1M tokens) |
| --- | --- | --- |
| Fable 5.1 | 10.00 | 50.00 |
| Opus 5.5 | 4.00 | 20.00 |
| Sonnet 5.5 | 2.00 | 10.00 |
| Haiku 5.5 (prompts up to 100K tokens) | 0.10 | 0.50 |

List prices as of October 2026, set in `backend/gateway/pricing.py`. Confirm them against Anthropic's pricing page before quoting savings.

## Who it's for

The buyer is the company's AI platform or IT team, which owns the Claude bill.

| User | What they get |
| --- | --- |
| Employee | The same tools, with routing out of sight |
| AI platform or IT admin | Per-team spend and savings, a live quality estimate, escalation rates and per-request routing detail |
| Finance | A lower usage bill |

As an illustration only, sending 50% of requests to Haiku, 30% to Sonnet and keeping 20% on Opus would cut spend by about 64% compared with sending everything to Opus. Measuring the real mix is the project's main result.

## How it works

1. **Change one address.** The gateway accepts the Claude API's request format, so tools switch to it by changing their base URL. Claude Code [supports this directly](https://code.claude.com/docs/en/llm-gateway).
2. **Ask Jev.** For each new conversation, [Jev](https://docs.digitalocean.com/products/inference/how-to/use-system-one-api/index.html.md), a decision model that returns calibrated probabilities rather than text, answers three questions: how complex the request is, what kind of task it is, and how costly a subtle mistake would be.
3. **Apply a fixed policy.** Plain code turns those answers into a tier, so every decision can be traced and replayed in the eval. If the request sits between two tiers, it goes to the higher one.
4. **Answer.** Claude models do all the work. The conversation stays on one tier, and the response carries the requested model's name, so employees never see which model answered.

| Tier | Model | Effort |
| --- | --- | --- |
| Haiku | `claude-haiku-5-5` | `medium` |
| Haiku, high effort | `claude-haiku-5-5` | `high` |
| Sonnet | `claude-sonnet-5-5` | As requested |
| Opus | `claude-opus-5-5` | As requested |

The rules around that:

- **The requested model is a ceiling.** A request is never sent above what the client asked for.
- **Only Opus 5.5 and Sonnet 5.5 requests are routed.** Other models, and requests with images or files, pass through unchanged. Fable 5.1 is priced but not routed.
- **Failures round up.** If Jev fails, times out or can't take the prompt, the request stays on the requested model. If a cheaper model rejects a request, the original is resent to the requested model.
- **Silent escalation.** If someone regenerates an answer or asks the same thing again within 30 minutes, it quietly moves up one tier and is logged as a possible routing mistake. Automatic retries and tool-loop turns don't count.
- **Savings are net.** They're measured against the model each request asked for, after routing and quality check costs.
- **Metadata only.** The database never stores prompts or answers.

## Quality checks

The gateway checks its own work in production and tunes itself from the results.

1. **Sample.** After a cheaper answer finishes, 3% are quietly sent to the requested model as well. Employees never wait for this or see it.
2. **Judge.** A Claude judge compares the two answers without knowing which is which.
3. **Tune.** A sequential test tracks each task type and team against the target: rated worse on under 2% of requests. A clear problem makes routing stricter for that segment within a few dozen checks. Loosening takes about 400 clean checks in a row.
4. **Report.** The admin view shows quality with a 95% range beside the savings, plus a rolling trend with every automatic change marked.

Spending is capped at $25 a day by default. Prompts over 50K tokens are skipped, and requests with server-side tools or MCP connectors are never sampled, because repeating them would really run those tools. [PLAN.md](PLAN.md) has the statistics, the sample rate trade-off and the known limits.

## Run it

You need Python 3.12 or later and Node 20.19 or later. Commands are for macOS and Linux.

### Try the demo, no API keys needed

This replays a simulated request stream with a drift partway through, using stand-ins for Jev and Claude. The numbers are made up, so it shows the system working rather than how good the models are.

```sh
# Terminal 1
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
DB_PATH=sim.db .venv/bin/python simulate_drift.py
DB_PATH=sim.db SHADOW_DAILY_BUDGET_USD=100 .venv/bin/uvicorn gateway.main:app --env-file .env.example

# Terminal 2
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and sign in with the token `change-me`.

### Run it for real

Requires a Claude API key and a [DigitalOcean inference key](https://docs.digitalocean.com/products/inference/how-to/use-system-one-api/index.html.md) for Jev. This path hasn't been tested with live keys yet.

```sh
cd backend
cp .env.example .env    # then fill it in
.venv/bin/uvicorn gateway.main:app --env-file .env

# Point Claude Code at the gateway, using a key from GATEWAY_KEYS
export ANTHROPIC_BASE_URL=http://localhost:8000
export ANTHROPIC_AUTH_TOKEN=<gateway key>
```

### Run the tests

```sh
cd backend
.venv/bin/pytest
```

To print the sample rate trade-off table:

```sh
DB_PATH=:memory: .venv/bin/python simulate_drift.py --sweep
```

### Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes | Claude API key the gateway uses upstream |
| `MODEL_ACCESS_KEY` | Yes | DigitalOcean inference key for Jev |
| `GATEWAY_KEYS` | Yes | One credential per person or tool, as `key:team` pairs separated by commas |
| `ADMIN_TOKEN` | Yes | Unlocks the admin view |
| `SHADOW_RATE` | No | Share of cheaper answers to check. Default `0.03`, and `0` turns checks off |
| `SHADOW_DAILY_BUDGET_USD` | No | Checks stop for the day at this spend. Default `25` |
| `DB_PATH` | No | SQLite file. Default `gateway.db` |
| `UPSTREAM_URL`, `JEV_URL` | No | Override the Claude API and Jev addresses, for testing against a fake service |

## Repository layout

```
backend/
  gateway/
    main.py       HTTP routes and the streaming relay
    router.py     conversation tracking, re-ask detection, the Jev client
    policy.py     Jev questions, tiers, and the routing decision
    pricing.py    prices and cost from a usage block
    store.py      SQLite: conversation state, request log, quality checks
    sse.py        reads the event stream
    shadow.py     samples answers and re-asks the requested model
    judge.py      blind comparison of two answers
    tuning.py     the sequential test and the strictness ladder
  test_gateway.py
  simulate_drift.py
frontend/         admin view: React and Vite
eval/             empty until the eval starts
```

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/messages` | The Claude Messages API, routed |
| `POST /v1/messages/count_tokens` | Token counting, passed through |
| `GET /admin/summary`, `/admin/quality`, `/admin/requests` | Admin data, which needs `ADMIN_TOKEN` |

[PLAN.md](PLAN.md) has the architecture, the decisions behind it and why the backend is Python rather than Rust. [COURSE_OVERVIEW.md](COURSE_OVERVIEW.md) has the course requirements and deadlines.

## Current status

**Working, tested against fake services:**

- The Claude-compatible gateway, including streaming and hiding the real model
- Routing, one model per conversation, silent escalation and rounding up on failure
- Quality checks, the judge and automatic tuning
- Cost tracking and the admin view
- The drift simulation

**Not done yet:**

- [ ] Run against the live Claude API and Jev, and through real Claude Code
- [ ] Build the eval. Until then there are no measured savings or quality figures, and every number in the demo is simulated
- [ ] Measure how often the judge rates two Opus answers as different. If that's near 2%, the tuner would tighten forever
- [ ] Replay real eval answers in the demo instead of invented miss rates, paced so the drift can be watched live
- [ ] Pin dependencies or add a container, which the final submission requires
- [ ] Support Python 3.11, which the course specifies. The backend currently requires 3.12
- [ ] Confirm Jev's price on DigitalOcean. The code pins `typesafe-jev-1.13.0` and uses a $0.042 per million token rate from an article about version 1.14

## Evaluation plan

The eval measures how much the router saves while matching the top model's answer quality, using a table of every test request answered by every tier.

1. **Test set of about 1,000 workplace requests.** Work-related prompts filtered from WildChat, a public set of real chatbot conversations; tasks with checkable answers (math, code with unit tests); and about 150 requests written from interviews with people who use Claude at work. A held-out split is never used for tuning.
2. **Answer table.** Every request goes to all four tiers once. Results are cached and committed so graders can rerun the eval without new model calls. Checkable answers are graded automatically. Open-ended answers are compared with Opus's answer by the judge in `gateway/judge.py`, and about 100 of those judgments are checked by hand. Each request's correct route is the cheapest model whose answer is as good as Opus's.
3. **Baselines.** Always Opus, always Sonnet, always Haiku, simple rules (length and keywords), Haiku as the router, the Jev router, and a perfect router using the table's labels as the ceiling.
4. **Metrics.**
   - Cost against quality across routing thresholds, as one curve per strategy
   - Share of Opus's quality kept, and cost as a share of Opus's
   - Under-routing, the error that matters most, reported separately from over-routing
   - Added latency and cost of the router itself
   - Calibration: how often Jev is right at each confidence level
5. **Experiments that decide the design.** Route by task type vs. difficulty vs. both; Jev vs. Haiku as the router; which tiers to keep, including other effort settings; and how much saving the round-up rule gives up in exchange for fewer quality misses.

The target is no drop in quality employees could notice, meaning the judge rates the routed answer worse than Opus's on under 2% of requests, at the lowest cost that allows.

## Scope

The first version routes text requests sent through the Claude API or Claude Code. The Claude app, images and files, and non-Claude models are out of scope.

## Risks

| Risk | Mitigation |
| --- | --- |
| Users get a worse answer without knowing why | Always round up, escalate silently on a re-ask, and check a sample against the requested model |
| Employees may object to not knowing which model answered | Disclose routing in the company's AI usage policy; admins can see per-request routing |
| Requests in the Claude app can't pass through an outside gateway | Scope to API traffic: Claude Code, internal assistants, automations |
| Switching models mid-conversation can lose cached prompts and change the tone of replies | Route once per conversation and only escalate upward |
| Public test prompts may not look like a real company's traffic | Add interview-based requests; state this limitation in the brief |
| A Claude judge grading Claude answers may be biased or noisy | Keep it blind, hand-check about 100 judgments, prefer automatic grading, and measure its Opus-vs-Opus rate |
| Jev is new, released September 2026 | Pin an exact model version; measure its latency and accuracy rather than relying on published claims |
| The gateway sees every prompt | Store only metadata; use synthetic data in the demo |
| Jev, a third party hosted on DigitalOcean, receives prompt text | Not mitigated yet. Needs a decision before any real traffic |

## Team

Each workstream has one owner, so ownership shows clearly in the git history. Names to be added once the team is set.

| Role | Owns |
| --- | --- |
| Gateway and integration | The API-compatible gateway, Claude Code setup, silent escalation |
| Router | Jev questions, routing policy, confidence thresholds |
| Eval data | Test set, answer table, held-out split |
| Grading and experiments | Judge prompts, hand checks, cost-quality curves |
| Product and demo | Admin view, user interviews, deck and demo video |

With three members, combine gateway with router and eval data with grading.
