# Project Proposal: Elastic Machines
> team elastic machines · revised after instructor feedback

## One-line summary

For engineering teams that pay for Claude Code by the token, Elastic Machines sends each coding session to the cheapest Claude model that can finish it. It reports cost per completed task, with evidence from the team's own tests that success rates held.

## What we're building

The gateway from our first proposal stays as it is:
- It speaks the Claude Messages API, so Claude Code connects by changing one base URL.
- A router (Jev) rates each new conversation, and the gateway answers with the cheapest Claude tier that clears the bar: Haiku 5.5 at medium effort, Haiku 5.5 at high effort, Sonnet 5.5 or Opus 5.5.
- Routing never goes above the model the user asked for, and it rounds up when unsure.
- A blind judge compares a sample of the cheaper answers with the requested model's answers, and routing tightens where quality slips.

## 1. Customer and workload

**Who owns the budget.** The Head of Developer Productivity, or the VP of Engineering, at a software company with 50 to 300 engineers that uses Claude Code every day. Anthropic gives the enterprise average as about $150–250 per developer per month ([Claude Code costs](https://code.claude.com/docs/en/costs)), so 100 engineers spend roughly $15,000–25,000 a month.

**How they choose models today.** The org sets one default model in Claude Code's managed settings, and developers switch with `/model` ([model configuration](https://code.claude.com/docs/en/model-config)). We assume many keep Opus as the default, because a weaker model on a hard task costs engineer time. The conversations in section 2 check this.

**Does lower API usage lower their bill?** Yes, with one exception:
- Pointing Claude Code at a gateway ([gateway docs](https://code.claude.com/docs/en/llm-gateway)) means the gateway calls the Claude API with an API key. All routed traffic is therefore billed per token, through the Anthropic Console, Bedrock or Vertex, and fewer tokens means a smaller bill.
- **The exception:** a company with prepaid or committed spend it hasn't used up won't see a smaller invoice until its usage goes past the commitment. We screen these companies out.

**Workload.** Claude Code sessions on three coding tasks, each ending with an executable check:

| Task | Example | Counts as done when |
| --- | --- | --- |
| Fix a bug that has a failing test | "test_parse_dates fails on leap years, fix it" | The failing test passes and the rest of the suite still passes |
| Write tests for existing code | "add unit tests for `cost_usd`" | The new tests pass on the current code and fail on a planted bug |
| Small refactor | "extract the retry logic into a helper" | The full suite, linter and type checks pass |

**Out of scope:**
- new features from open-ended specs
- design and architecture work
- long unattended runs
- repositories without a test suite
- GPT and Gemini routing, which refuses tool use and so can't serve Claude Code

## 2. Why this customer would choose us

| Alternative | What it gives this buyer | What it leaves out |
| --- | --- | --- |
| Bedrock Intelligent Prompt Routing ([docs](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html)) | Chooses between two models in one family based on predicted quality | Quality is predicted from the prompt alone, and AWS advises testing on your own tasks. Bedrock only |
| OpenRouter Auto Code ([docs](https://openrouter.ai/docs/guides/routing/routers/auto-code)) | A router tuned for coding agents that keeps a session on one model | Routes using OpenRouter's own coding policy; its docs describe no measurement on your code |
| Not Diamond custom router ([docs](https://docs.notdiamond.ai/docs/router-training-quickstart)) | A router trained on your evaluation data | You have to build and score that evaluation data yourself |

**Our one advantage: evidence and control on the customer's own work.** The budget owner can already see what Claude Code costs. What they can't see is whether a cheaper model would have finished a given task just as well. Today the only way to find out is to change the default model and wait for complaints. We give them:

1. **Evidence before switching anything on.** We replay tasks from their repository history through both setups, and report cost per completed task with a confidence interval.
2. **Evidence after switching on.** Per-team spend, savings and quality, with the share of traffic the checks actually cover stated next to each number.
3. **Control.** The requested model is a ceiling that is never exceeded. Routing tightens automatically per team when quality slips.

**Hypothesis to validate:** a budget owner will let a router change Claude Code's models only with this evidence, and would pay for it.

**How we test it:** we talk to 3–5 people who own a Claude Code budget. We ask:
- how they're billed
- how they choose models
- what they would need to see before letting a router change models

**What would kill it:** if they wouldn't pay for evidence, the hypothesis fails.

## 3. Customer outcome and economics

**Task set.** About 50 tasks from [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/), plus about 20 from our own repositories. For our own tasks, we take past commits that fixed a bug and had a test, and revert the fix. Every task is graded by running its tests.

**Setups.** Each task runs 3 times on the requested model (Opus, with routing off) and 3 times routed.

**Metrics.**
- **Cost per successful task:** all spend in a setup, including failed runs, divided by the number of tasks that passed.
- **Completion rate**, with a 95% interval.
- **Elapsed time** per task.

**Net dollar savings.**
- Each run logs actual input, output, cache-read and cache-write tokens, plus router, judge and retry costs.
- **Net savings** = cost on the requested model − (routed cost + router + judge + retries + cache rewrites after escalation).

**What the same work would have cost on the requested model.**
- **In the eval:** we measure it directly, by running every task on Opus.
- **In production:** the gateway takes the tokens the cheaper model actually used and prices them at the requested model's rates. This assumes Opus would have used the same number of tokens and turns. If a cheaper model takes more turns to finish, the estimate overstates savings. We measure the token ratio between routed and Opus runs in the eval, and label production savings as an estimate corrected by that ratio.

**Price.** We charge 20% of verified net savings. Monthly fee = Claude Code spend × share of spend in scope × net savings rate × 20%.

| Input | Value | Source |
| --- | --- | --- |
| Engineers using Claude Code | 100 | Assumption |
| Spend per engineer per month | $200 | Middle of Anthropic's $150–250 range |
| Monthly Claude Code spend | $20,000 | Calculated |
| Share of spend on in-scope tasks | 50% | Assumption |
| Net savings rate on in-scope work | 25% | Assumption; the eval measures this |
| Customer's net savings | $2,500 a month | Calculated |
| **Our fee** | **$500 a month** | Calculated |

**Is the account worth onboarding and support?** These figures are assumptions:
- **Onboarding:** about 20 hours for security review, deployment, keys and a replay of the customer's repository history.
- **Support:** about 3 hours a month for keeping prices current, a monthly report and incidents.

At $100 an hour, with onboarding spread over a year, the account costs about $470 a month to serve, against a $500 fee. An account breaks even at about $19,000 a month of Claude Code spend, roughly 95 engineers. Smaller accounts only pay off if onboarding is mostly self-serve, or if we charge a minimum fee.

## 4. Failure modes we will test

1. **Conversations that start easy and get harder.** Today a conversation keeps its first tier. It only moves up if the user repeats the exact same prompt within 30 minutes, or if a cheaper model rejects the request.
   - **Test:** 20–30 scripted sessions whose first turn is trivial ("rename this function") and whose later turn is hard ("now fix the race condition in the cache").
   - **Measured:** how often escalation fires, and completion rate compared with Opus.
   - **If escalation misses these changes,** we test re-rating each new turn the user types, moving up only.
2. **What the quality checks leave unmeasured.** Checks skip tool-call turns, refusals and prompts over 50,000 tokens.
   - We log the reason for each skip and report what share of requests and of spend can't be checked.
   - Claude Code sessions are mostly tool turns, and they grow past 50,000 tokens quickly, so we expect that share to be large.
   - We state the quality claim for the checked share only. For the rest, we rely on test results.
3. **Judge disagreement.** The judge compares two independent Opus answers to about 200 prompts. The rate at which it calls one of them worse must sit well below 2%, the rate of worse answers at which the gateway tightens routing. If it doesn't, the loop would tighten on noise alone, so we raise the target before trusting automatic tightening.
4. **Judge against people.** Two teammates label about 100 verdicts blind, and we report how often the judge agrees with them.
5. **Quality-loss threshold and uncertainty.**
   - Routing passes if its completion rate is at most 5 points below Opus's. We judge this with the lower bound of a paired 95% interval.
   - With about 70 tasks and a completion rate near 70%, the interval on a single rate is about ±10 points. If the interval is wider than the margin, we report the result as inconclusive, not as a pass.
