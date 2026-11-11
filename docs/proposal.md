# Elastic Machines: Project Proposal

Oct 30, 2026 · @Calvin Chang

## Problem

Employees on a company Claude plan tend to send every request to the most capable model, and under usage-based billing the company pays top-tier rates for work a cheaper model would do just as well.

- **Usage is now a direct cost.** Claude Enterprise is billed at $20 per seat per month plus usage at API rates, with no included token allowance ([pricing summary](https://justinmckelvey.com/blog/claude-enterprise-pricing), [usage details](https://www.gosearch.ai/faqs/what-is-claude-enterprise-pricing-in-2026/)).
- **The price gap between models is large.** Opus 5.5 costs 40 times as much per token as Haiku 5.5, and Fable 5.1 costs 2.5 times Opus.
- **Most requests probably don't need the top model.** Rewording an email, fixing a formula or summarizing a short document are tasks a smaller model handles well. People default to the strongest model because choosing is friction and nobody wants a worse answer. How large this share is in real traffic is our central hypothesis, and the project measures it.

| Model | Input ($ per 1M tokens) | Output ($ per 1M tokens) |
| --- | --- | --- |
| Fable 5.1 | 10.00 | 50.00 |
| Opus 5.5 | 4.00 | 20.00 |
| Sonnet 5.5 | 2.00 | 10.00 |
| Haiku 5.5 (prompts up to 100K tokens) | 0.10 | 0.50 |

List prices as of October 2026 ([price guide](https://claudecoworkcourse.com/ai-api-pricing/claude), [Haiku 5.5 launch](https://techenclave.com/news/items/claude-haiku-5-5-launches-with-0-10-input-pricing-for-shorter-prompts)); to be confirmed against Anthropic's pricing page.

## Proposed solution

Elastic Machines is a drop-in gateway between employees' Claude tools and the Claude API that sends each request to the cheapest model likely to answer it as well as the top model would.

- **Drop-in.** It accepts the same request format as the Claude API, so internal tools switch to it by changing one address. Claude Code officially supports routing through a company gateway this way ([Claude Code gateway docs](https://code.claude.com/docs/en/llm-gateway)).
- **Invisible to employees.** Employees know the company routes requests, but never see which model answered a given request or when routing happens. They keep asking as they do today.
- **Always round up.** When a request sits between two tiers, or the router is unsure, it goes to the higher tier. Savings come only from requests that are clearly simple, so answer quality stays first.
- **Silent escalation.** If an employee regenerates an answer or asks the same thing again, the follow-up quietly goes one tier up, and the case is logged as a possible routing mistake.
- **Admin view.** Spend, savings against an "everything on the top model" baseline, and silent-escalation rate, broken down by team. Only admins see which model handled each request.

## How it works

Jev answers three quick questions about each request, a fixed policy turns those answers into a model choice, and Claude models do all the actual work.

&#91;embedded content: request flow · 3 Jev questions, 4 model tiers, silent escalation\]

- **Why Jev as the router.** It returns typed answers with calibrated probabilities rather than text, so the policy can round up whenever confidence is low ([Jev API docs](https://docs.digitalocean.com/products/inference/how-to/use-system-one-api/index.html.md)).
- **What stays in code.** Token counts, context-length limits and the routing table itself, so every decision can be traced and replayed in the eval.

**Four tiers.** Haiku 5.5, Haiku 5.5 with a higher effort or thinking budget, Sonnet 5.5 and Opus 5.5. The extra Haiku tier sits in the 20-fold price gap between Haiku and Sonnet, where rounding up costs the most; which effort settings Haiku 5.5 supports is to be confirmed in Week 3. Fable 5.1 joins as a fifth tier only if employees already use it today.

## Users and value

The buyer is the company's AI platform or IT team, which owns the Claude bill; employees benefit without changing how they work.

| User | What they get |
| --- | --- |
| Employee | Same tools and answer quality; routing happens out of sight |
| AI platform or IT admin | Routing policy, per-team spend and savings, escalation rates, per-request routing detail |
| Finance | A lower and more predictable usage bill |

**Illustrative savings.** Because the policy rounds up, the mix is conservative. If 50% of requests go to Haiku, 30% to Sonnet and 20% stay on Opus, spend still falls by about 64% compared with sending everything to Opus. This mix is a placeholder; measuring the real one is the project's main result.

**Router cost.** Jev is listed at $0.042 per million input tokens with free output, under half of Haiku's input price ([Jev 1.14 pricing](https://pasqualepillitteri.it/en/news/26048/jev-1-14-image-input-box-typesafe-warns)). Routing every request costs little next to what it saves, but we will measure it.

## Evaluation plan

We measure how much cost the router saves while matching the top model's answer quality, using a table of every test request answered by every model.

1. **Test set of about 1,000 workplace requests.** Work-related prompts filtered from WildChat, a public set of real chatbot conversations; tasks with checkable answers (math, code with unit tests); and about 150 requests we write from interviews with people who use Claude at work. A held-out test split is never used for tuning.
2. **Answer table.** Every request goes to all four tiers once; results are cached and committed so graders can rerun the eval without new model calls. Checkable answers are graded automatically. Open-ended answers are compared with Opus's answer by a Claude judge, and we check about 100 of those judgments by hand. Each request's correct route is the cheapest model whose answer is as good as Opus's.
3. **Baselines.** Always Opus, always Sonnet, always Haiku, simple rules (length and keywords), Haiku acting as the router, our Jev router, and a perfect router using the table's labels as the ceiling.
4. **Metrics.**
   - Cost against quality across routing thresholds, as one curve per strategy
   - Share of Opus's quality kept, and cost as a share of Opus's
   - Under-routing, the error that matters most (a hard request sent to a model that failed it) reported separately from over-routing (an easy request sent to an expensive model)
   - Added latency and cost of the router itself
   - Calibration: how often Jev is right at each confidence level
5. **Experiments that decide the design.** Route by task type vs. difficulty vs. both; Jev vs. Haiku as the router; which tiers to keep (two, three or four, plus other effort settings); and how much saving the round-up rule gives up in exchange for fewer quality misses.

Target, to be confirmed in Week 3: no drop in quality employees could notice (the judge rates the routed answer worse than Opus's on under 2% of requests), at the lowest cost that allows.

## Scope and milestones

The first version routes text requests sent through the Claude API or Claude Code among four tiers built from three Claude models; the Claude app, images and files, and non-Claude models are out of scope.

&#91;embedded content: course plan · 4 phases, 3 deadlines\]

Week 5 needs one path working end to end, so that Weeks 6 and 7 go to measurement rather than plumbing.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Users get a worse answer without knowing why | Always round up; escalate silently when an employee re-asks; track the re-ask rate as a quality signal |
| Employees may object to not knowing which model answered | Disclose that routing exists in the company's AI usage policy; admins can see per-request routing |
| Requests made in the Claude app itself can't pass through an outside gateway | Scope to API traffic: Claude Code, internal assistants, automations |
| Switching models mid-conversation can lose cached prompts and change the tone of replies | Route once per conversation and only escalate upward; measure the effect |
| Public test prompts may not look like a real company's traffic | Add interview-based requests; state this limitation in the brief |
| A Claude judge grading Claude answers may be biased | Hand-check about 100 judgments; prefer automatic grading where possible |
| Jev is new, released September 2026 | Pin an exact model version; measure its latency and accuracy rather than relying on published claims |
| The gateway sees every prompt | Log only metadata by default; use synthetic data in the demo |

## Team and roles

Each workstream has one owner so ownership shows clearly in the git history; names to be added once the team is set.

| Role | Owns |
| --- | --- |
| Gateway and integration | The API-compatible gateway, Claude Code setup, silent escalation |
| Router | Jev questions, routing policy, confidence thresholds |
| Eval data | Test set, answer table, held-out split |
| Grading and experiments | Judge prompts, hand checks, cost-quality curves |
| Product and demo | Admin view, user interviews, deck and demo video |

With three members, combine gateway with router and eval data with grading.
