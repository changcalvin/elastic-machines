# Project Proposal: Elastic Machines
> team elastic machines

## One-line summary

Elastic Machines is a gateway that sends each LLM request to the cheapest model that can answer it as well as the model the user picked. It shows the user how much that saved.

## Problem

Teams send most of their LLM traffic to each provider's top model because picking a model for every request is too much effort. Most of those requests don't need a top model. Formatting text, short lookups and simple edits would get the same answer from a model that costs a fraction as much. So the team pays more than it needs to, and nobody can see how much more.

## Who it's for

Enterprise teams and with real LLM spend across Claude, GPT and Gemini, especially those using Claude Code or the Claude SDKs every day. These users want lower bills without lower quality, and they won't change their tools to get them.

## What we're building

A drop-in gateway, a chat app and an analytics view.

1. **Gateway.** GPT, Claude and Gemini models are available through the same API.
2. **Router.** For each new conversation, a router (Jev) rates how hard the request is, from trivial to expert. The gateway answers with the cheapest model from the same provider that clears that bar.
3. **The picked model is a ceiling.** Routing never goes above the model the user asked for. When the router is unsure, it rounds up. If the router fails, the request goes to the picked model.
4. **Quality self-check.** A small share of cheaper answers (3% by default, with a daily budget cap) is compared against the picked model's answer by a blind judge. Routing tightens automatically where quality slips.
5. **Chat and analytics.** A web chat where each message shows its actual cost next to what the picked model would have cost.
6. **Privacy.** The gateway stores metadata only, never prompts or answers.

| Difficulty | Claude | OpenAI | Gemini |
| --- | --- | --- | --- |
| Trivial | Haiku 5.5, medium effort | GPT-6 Luna | Gemini 3.1 Flash-Lite |
| Routine | Haiku 5.5, high effort | GPT-6.1 Terra | Gemini 3.5 Flash-Lite |
| Involved | Sonnet 5.5 | GPT-6.1 Sol | Gemini 3.8 Flash |
| Expert | Opus 5.5 | GPT-6 Astra | Gemini 4 Carbon |

A conversation stays on one rung. If the user asks the same thing again, the conversation moves up one rung.

## Scope

**In scope:** text requests through the Messages API, routing within a single provider, a cost and savings view per conversation and per team, quality checks with automatic tightening, and admin endpoints for spend, quality and request logs.

**Out of scope:** routing across providers, images and other non-text input for GPT and Gemini, sign-in and billing, and training our own router.

## Evaluation plan
The eval set is the product spec. It needs to show that we save money without losing quality, and it needs to include hard cases, not only easy ones.

- **Eval set:** realistic requests across all four difficulty levels and several task types (coding, writing, lookup, reasoning). It will deliberately include cases that are easy to misjudge, such as requests that look short but need real reasoning.
- **Savings:** the cost of each routed answer compared with the cost of sending it to the picked model, using list prices.
- **Quality:** blind pairwise judging of routed answers against the picked model's answers, reported as win, tie or loss rates by difficulty level. We will hand-check a sample to make sure the judge agrees with people.
- **Router accuracy:** how often the router's difficulty rating matches a labeled rating, and in particular how often it rates a request too low.
- **Overhead:** added latency per request, and the cost of the router and the judge themselves.
- **Decisions the eval will drive:** the router's thresholds, the quality-check sample rate (`simulate_drift.py --sweep` already models this trade-off), and which rungs are safe to use.

## Business case
Savings are easy to measure, so the price can follow them, for example a share of verified savings or a per-seat fee for teams. The pitch is a lower LLM bill with no change in tools and no prompts stored.

## Risks
- **Bad routing:** a hard request rated as easy gets a worse answer. We limit this by rounding up when the router is unsure, moving a repeated question up a rung, and running quality checks.
- **Judge reliability:** an LLM judge can be biased. We will check it against human labels.
- **Price changes:** list prices change often. Gemini 4's price doubles on December 1, 2026, so our pricing table has to be kept current.
- **Dependence on the router:** if the router is unavailable, there are no savings, although requests still get answered.

## Milestones
| Week | Milestone |
| --- | --- |
| 5 | Working gateway, router, chat and analytics against fake providers; README and status note |
| 6 | Run against the live APIs; build the eval set; first measured savings and quality results |
| 7 | Final repository with eval set and results, product brief, deck, demo video and live demo |
