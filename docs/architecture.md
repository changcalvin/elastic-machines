# Build plan

How the [proposal](proposal.md) maps onto this repo. To run it, see the [README](../README.md).

## Backend language: Python

Python, because the gateway spends almost all its time waiting on the network. Measured on a laptop with a real uvicorn server, one worker, against a fake upstream that answers instantly, so these numbers are what the gateway itself adds:

| Case | Added latency, p50 | Added, p99 |
| --- | --- | --- |
| Later turn in a conversation, the common case | 0.41 ms | 0.54 ms |
| First turn, including a call to a fake Jev | 0.76 ms | 1.18 ms |
| 2 MB conversation, about 500K tokens | 7.24 ms | 9.17 ms |

- **Rust's possible gain is capped at those numbers.** A gateway that added nothing would save under half a millisecond on a request that then waits on Jev and Claude. Real Jev and Claude latency isn't in this table.
- **Capacity isn't the limit.** One worker held 753 requests per second, a floor because the Python load generator shared the machine. With 300 streams open, a small request took 1.4 ms at p50 and 14.3 ms at p99.
- **The eval decides it.** It has to replay the routing policy exactly, and the test set and judging work are Python. In Rust, the policy would need a second copy or a language bridge.
- **Revisit Rust if** sustained traffic reaches thousands of requests per second, or very large bodies start stalling other streams. `policy.py` and `pricing.py` are small enough to port.

## Layout

```
backend/
  gateway/
    main.py       HTTP: /v1/messages, /v1/models, count_tokens, /usage, /admin, the streaming relay
    providers.py  OpenAI and Gemini behind the Claude Messages API, text only
    router.py     conversation tracking, re-ask detection, Jev client
    policy.py     each provider's tiers, Jev questions, answers to tier (pure, the eval imports this)
    pricing.py    prices and cost from a usage block (the eval imports this too)
    store.py      SQLite: conversation state, request log, quality checks
    sse.py        reads Claude's event stream for the relay and for checks
    shadow.py     samples answers, re-asks the requested model, feeds the tuner
    judge.py      blind comparison of two answers (the eval can grade with this)
    tuning.py     the sequential test and the strictness ladder (pure)
  tests/
  simulate_drift.py   drift demo and sample rate table, through the real gateway code
frontend/         chat and per-chat savings: React, Vite
```

The eval can run `pip install -e backend`, then `from gateway.policy import choose_tier`.

## Providers

Every provider has four tiers, one per Jev complexity level, and a request is only ever routed within the provider of the model it asked for. A GPT request is never answered by Claude, so "as good as the model you picked" stays a comparison within one family.

OpenAI and Gemini sit behind `providers.py`. It translates a Claude Messages request into Chat Completions or `generateContent`, and translates the answer, streamed or not, back into Claude's message and event format. Everything downstream, from the relay to cost logging to quality checks, sees Claude-shaped data and doesn't branch on provider.

- **Text only.** Tools, images and files are refused for GPT and Gemini with a 400, rather than silently dropped. Claude requests with them pass through unrouted, as before.
- **Thinking is billed, not shown.** Gemini thought parts are dropped from the answer, and their tokens count as output, as Google bills them.
- **No effort settings.** Claude's two Haiku tiers differ by effort. The GPT and Gemini tiers are four distinct models, so no provider-specific reasoning parameters are sent.
- **The judge stays on Claude.** Quality checks re-ask the requested GPT or Gemini model through the same translation, and the Claude judge compares the two answers.

## Request flow

1. Check the gateway key, which identifies the team.
2. Pass through untouched if the model isn't a routable ceiling (`CEILINGS` in `policy.py`), or the request has images or files. GPT and Gemini requests that aren't plain text are refused.
3. Identify the conversation from the team, the Claude Code session and the first message.
4. **Known conversation:** reuse its tier, so no Jev call. If the same prompt was already answered, move up one tier and log it.
5. **New conversation:** ask Jev three questions and apply the policy at the current strictness for that task and team. If Jev fails, times out, or the prompt is too long, keep the requested model.
6. Send to Claude. If a cheaper model rejects the request with a 400 or 404, resend the original bytes on the requested model and stay there.
7. Relay the answer event by event with the requested model's name in it, then log metadata only.
8. If this answer was sampled and finished normally, start a quality check in the background.

## Quality checks

The gateway checks a sample of cheaper answers against the requested model, and the results tune the router. The eval measures quality once. This keeps measuring it in production.

1. **Sample.** After a cheaper answer finishes, 3% (`SHADOW_RATE`) are sent again to the requested model in the background. The employee never waits for this and never sees it. Measured with the reference slowed by 300 ms, a check on every request moved response time from 6.01 ms to 6.36 ms.
2. **Judge.** A Claude judge compares the two answers without knowing which is which. A and B are assigned at random, and small differences in style count as the same.
3. **Learn.** Each verdict updates a sequential test (CUSUM) for that task type and team. CUSUM suits this because it stays reliable when watched continuously, which repeatedly checking a confidence interval doesn't.
4. **Act.** Each task type and team sits on a seven-rung strictness ladder. Lower rungs change the confidence bar. The top two also set a minimum tier, because a higher bar changes nothing when Jev is confidently wrong. Strictness only ever applies to new conversations.
5. **Report.** The admin view shows quality with a 95% interval beside the savings, and a rolling trend with every automatic change marked. Check costs are included in spend, so the savings figure is net of them.

**Keeping the loop stable:**

- **One-sided caution.** A false tighten costs money, while a false loosen costs quality, so loosening takes far more evidence: about 400 clean checks in a row, against 4 misses in a row to tighten.
- **Stale evidence is dropped.** Every sample carries the rung it was routed under. A verdict from an older setting counts toward the quality estimate but doesn't move the router. Without this, delayed verdicts would keep pushing after a fix and cause oscillation.
- **Evidence restarts after each move,** and a move on a task type clears team evidence, so one cause doesn't tighten both.
- **The ladder stops at a Sonnet floor.** If everything went to Opus, nothing cheaper would run, nothing could be measured, and the segment could never loosen again.

**Measured behaviour of the controller** (2,000 simulated runs of up to 4,000 checks each):

| True rate of worse answers | Tightened | Median checks | Loosened | Median checks |
| --- | --- | --- | --- | --- |
| 0.5% | 0% | | 100% | 533 |
| 1% | 0.9% | 527 | 99.0% | 886 |
| 2%, the target | 56.2% | 1,460 | 12.5% | 1,896 |
| 3% | 99.6% | 583 | 0% | |
| 6% | 100% | 104 | 0% | |
| 10% | 100% | 48 | 0% | |
| 25% | 100% | 15 | 0% | |

**The sample rate trade-off.** More checks give a tighter estimate and faster detection, but they eat into savings. This assumes 10,000 requests a day, half of them eligible, and that a shadow call plus its judge costs about two ordinary requests. That last figure is an assumption until there's real traffic.

| Sample rate | Share of a 60% saving spent on checks | Days to ±1 point | Days to catch a 25% miss rate | Days to catch a 6% miss rate |
| --- | --- | --- | --- | --- |
| 1% | 1.7% | 15.1 | 0.32 | 2.17 |
| 2% | 3.3% | 7.5 | 0.16 | 1.08 |
| 3% | 5.0% | 5.0 | 0.11 | 0.72 |
| 5% | 8.3% | 3.0 | 0.06 | 0.43 |
| 10% | 16.7% | 1.5 | 0.03 | 0.22 |

**Drift demo.** `simulate_drift.py` replays 9,000 requests through the real gateway code, with a surge of hard coding requests that Jev mistakes for routine. Rolling quality falls to 90.0%, the router tightens code routing 45 checks after the drift begins, quality recovers, and routing loosens again once the drift has passed. Jev, Claude and the judge are stand-ins with invented miss rates, so this shows the loop works, not how good real models are. The eval's test set can replace the simulated stream once it exists.

**Costs and safety:**

- **Spending has limits.** Checks stop for the day at `SHADOW_DAILY_BUDGET_USD`, $25 by default. Prompts over 50K tokens are skipped because shadow calls miss the prompt cache. At most 4 checks run at once, and extra samples are dropped rather than queued. Setting `SHADOW_RATE=0` turns checks off.
- **Shadow calls can't cause side effects.** Client tools are safe to repeat because nobody runs what a shadow call asks for. Requests with server-side tools or MCP connectors are never sampled, because those would really run.
- **Nothing is stored.** Prompts and answers stay in memory for the judge. Only verdicts and costs reach the database, and a test scans every table to confirm it.

**Known limits:**

- **Judge noise is the biggest risk.** Two Opus answers to the same prompt will sometimes be rated as different. If that happens on 2% or more of requests, the loop tightens forever regardless of routing. See the open items.
- **Coverage is partial.** Only finished text answers are checked. Tool-call turns, refusals and long prompts aren't, so a coding agent's traffic is mostly represented by its final answers.
- **Quality here means down-routed answers.** Requests that stayed on the requested model aren't in the estimate, which makes it stricter than a figure over all traffic.
- **Busy periods are under-sampled,** because checks drop under load.
- **Low-traffic segments move slowly.** A segment needs hundreds of checks to loosen, which can take weeks at 3%. It stays on the safe side until then.

## Decisions the proposal left open

- **The three Jev questions** are a first draft in `policy.py`, because the proposal's request-flow diagram didn't survive export to Markdown: complexity (a score, one level per tier), task type (a choice that sets a minimum tier) and stakes (yes or no, moves up one tier). The Router owner should compare them with the original diagram.
- **Round-up rule:** pick the lowest tier Jev is 90% sure is enough, using its cumulative probabilities. `CONFIDENCE` is the one number to sweep for the cost-quality curves.
- **The requested model is a ceiling.** The gateway never routes above what a client asked for, including on escalation.
- **Haiku effort is settled:** Haiku 5.5 supports `low` to `max`, default `medium`. The two Haiku tiers use `medium` and `high`. The eval should test `low`.
- **A retry isn't a re-ask.** A prompt only counts as answered once a response finishes, so automatic retries after errors or dropped connections don't escalate. Tool-loop turns don't count either.
- **Re-asks are exact matches** after ignoring case and whitespace, within 30 minutes. Paraphrases aren't caught yet.
- **Savings use the model each request asked for,** not always Opus. When everyone asks for Opus these are identical, and requests that were never going to Opus don't count as savings.
- **The relay uses httpx and the judge uses the Anthropic SDK.** Claude Code's gateway guide requires unknown headers, fields and errors to pass through unchanged, which a typed client doesn't do. Shadow calls follow the relay for the same reason. The judge is an ordinary new API call, so it gets the SDK and validated structured output.
- **The judge runs on Opus 5.5** with server-side refusal fallback on. `JUDGE_MODEL` in `judge.py` is one line to change if the cost matters more than consistency with the proposal.
- **The check's reference is the requested model,** matching the savings baseline. For Opus requests this is exactly the proposal's comparison.

## Open items

- [ ] Run against the live Claude API and Jev. Everything so far used fakes, so no real keys have been exercised.
- [ ] Measure the judge's noise before trusting automatic tightening. In the eval, answer each request with Opus twice and have the judge compare the two. If that rate is near 2%, raise `TARGET` and `ALARM_RATE` in `tuning.py` to sit above it.
- [ ] Replace the assumed cost of a check in the sample rate table with measured shadow and judge costs.
- [ ] Run GPT and Gemini against their live APIs. The translation in `providers.py` is tested against fakes built from their documented formats.
- [ ] Add a row to the proposal's risk table: the gateway sends prompt text to Jev, a third party hosted on DigitalOcean. That goes beyond "log only metadata".
- [ ] Confirm Jev's price on DigitalOcean. Its docs list `typesafe-jev-1.13.0`, while the proposal's $0.042 comes from an article about TypeSafe's own 1.14 pricing.
- [ ] Test the Haiku tiers with real traffic. Requests that use tools Haiku rejects fall back through the 400 path, which pins that conversation to the requested model and gives up its savings.
- [ ] Decide how admins and gateway keys are managed beyond the `.env` file.
