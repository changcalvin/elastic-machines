# Project Proposal: Elastic Machines
> team elastic machines · revised after instructor feedback

**Summary.** For engineering teams that pay for Claude Code by the token, Elastic Machines sends each coding session to the cheapest Claude model that can finish it. It proves the saving with cost per completed task, measured against the team's own tests.

**What's built.** The gateway from our first proposal is unchanged:
- It's a drop-in Claude Messages API endpoint.
- A router (Jev) picks one of four Claude tiers for each conversation: Haiku 5.5 at medium effort, Haiku 5.5 at high effort, Sonnet 5.5 or Opus 5.5. It never goes above the model the user requested.
- A blind judge checks a sample of answers, and routing tightens when quality slips.

## 1. Customer and workload

**Who owns the budget.** The Head of Developer Productivity, or the VP of Engineering, at a company with 50–300 engineers that uses Claude Code daily. Anthropic's enterprise average is $150–250 per developer per month ([costs](https://code.claude.com/docs/en/costs)), so 100 engineers spend $15,000–25,000 a month.

**How they choose models today.** The org sets one default in Claude Code's managed settings, and developers switch with `/model` ([docs](https://code.claude.com/docs/en/model-config)). We assume many default to Opus to avoid failed tasks. Our buyer conversations will check this.

**Does lower usage lower the bill?** Yes. Claude Code connects to our gateway ([docs](https://code.claude.com/docs/en/llm-gateway)), and the gateway calls Claude with an API key. Routed traffic is therefore billed per token, through the Console, Bedrock or Vertex. The exception is a company with unused committed spend, whose invoice won't change until usage passes the commitment. We screen these out.

**Workload.** Claude Code sessions on three tasks, each graded by running tests:

| Task | Done when |
| --- | --- |
| Fix a bug that has a failing test | That test and the rest of the suite pass |
| Write tests for existing code | The new tests pass, and fail on a planted bug |
| Small refactor | The suite, linter and type checks pass |

**Out of scope:**
- open-ended features
- design work
- long unattended runs
- repositories without tests
- GPT and Gemini routing, which refuses tool use and so can't serve Claude Code

## 2. Why they would choose us

| Alternative | What it leaves out for this buyer |
| --- | --- |
| [Bedrock Intelligent Prompt Routing](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html) | Predicts quality from the prompt alone, and AWS advises testing on your own tasks. Bedrock only |
| [OpenRouter Auto Code](https://openrouter.ai/docs/guides/routing/routers/auto-code) | Routes using OpenRouter's own coding policy, with no measurement on your code |
| [Not Diamond custom router](https://docs.notdiamond.ai/docs/router-training-quickstart) | You have to build and score its training data yourself |

**Our advantage: evidence and control on the customer's own code.** Budget owners can see what Claude Code costs. They can't see whether a cheaper model would have finished the same task. We provide:
- **Before rollout:** cost per completed task on tasks replayed from their repository, with confidence intervals.
- **After rollout:** per-team spend, savings and quality, with the share of traffic the checks cover.
- **Control:** the requested model is a hard ceiling, and routing tightens automatically per team when quality slips.

**Hypothesis:** budget owners will let a router change Claude Code's models only with this evidence, and will pay for it.

**How we test it:** we ask 3–5 budget owners how they're billed, how they choose models, and what they would need to see first. If they wouldn't pay for evidence, the hypothesis fails.

## 3. Outcome and economics

**Eval.**
- **Tasks:** about 50 from SWE-bench Verified and about 20 from our own repositories. For our own tasks we take past bug fixes and revert them, keeping their tests.
- **Runs:** each task runs 3 times on Opus with routing off and 3 times routed.
- **We report:**
  - cost per successful task: all spend, including failed runs, divided by tasks passed
  - completion rate, with a 95% interval
  - elapsed time per task

**Net savings** = Opus cost − (routed cost + router + judge + retries + cache rewrites after escalation). It's calculated from actual input, output, cache-read and cache-write tokens.

**What the work would have cost on the requested model.**
- **In the eval:** we measure it, by running each task on Opus.
- **In production:** the gateway prices the cheaper model's actual tokens at Opus rates. That assumes Opus would have used the same tokens and turns, so it overstates savings if cheaper models take more turns. We correct it with the token ratio measured in the eval and label it an estimate.

**Price:** 20% of verified net savings.

| Input | Value | Basis |
| --- | --- | --- |
| Claude Code spend | $20,000 a month | 100 engineers × $200, within Anthropic's range |
| Share of spend in scope | 50% | Assumption |
| Net savings rate | 25% | Assumption; the eval measures it |
| Customer saves | $2,500 a month | Calculated |
| Our fee | $500 a month | Calculated |

**Is the account worth serving?** We assume 20 hours of onboarding, spread over a year, and 3 hours of support a month, at $100 an hour. That costs about $470 a month per account. An account breaks even at about $19,000 a month of Claude Code spend, roughly 95 engineers. Smaller accounts need self-serve onboarding or a minimum fee.

## 4. Failure modes we will test

1. **Easy start, hard later.** A conversation keeps its first tier unless the user repeats a prompt exactly within 30 minutes, or a cheaper model rejects the request.
   - **Test:** 20–30 sessions that start trivial and turn hard.
   - **Measured:** how often escalation fires, and completion rate compared with Opus.
   - **If escalation misses the change,** we test re-rating each new user turn, moving up only.
2. **Unmeasured traffic.** Quality checks skip tool-call turns, refusals and prompts over 50,000 tokens, which we expect to be most Claude Code traffic. We log each skip reason and report the unchecked share of requests and of spend. The quality claim covers the checked share only, and tests cover the rest.
3. **Judge disagreement.** The judge compares two independent Opus answers to about 200 prompts. The rate at which it calls one of them worse must sit well below 2%, the rate at which the gateway tightens routing. If it doesn't, we raise that rate before trusting automatic tightening.
4. **Judge against people.** Two teammates label about 100 verdicts blind, and we report how often the judge agrees with them.
5. **Threshold and uncertainty.** Routing passes if the lower bound of the paired 95% interval puts its completion rate no more than 5 points below Opus's. With about 70 tasks, a single completion rate is uncertain by about ±10 points; comparing the same tasks on both setups narrows this. If the interval is wider than the margin, we report the result as inconclusive, not as a pass.
