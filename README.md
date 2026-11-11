# Elastic Machines

One chat and one API for Claude, GPT and Gemini. Each message gets answered by the cheapest model likely to do it as well as the model you picked, and you can see what that saved.

Most requests don't need a provider's top model, but people pick it anyway because choosing is friction. Elastic Machines sits between your tools and the providers. For each new conversation, a router rates how hard the request is. The gateway then answers with the cheapest model from the same provider that clears the bar, never above the one you asked for. When it's unsure, it rounds up.

- **Drop-in.** The gateway speaks the Claude Messages API, so Claude Code and any Claude SDK work by changing one base URL.
- **Multi-provider.** GPT and Gemini models are available through the same API and chat, each routed within its own provider.
- **Checks its own quality.** A small sample of cheaper answers is quietly compared against the model you asked for by a blind judge, and routing tightens automatically where quality slips.
- **Private by default.** Only metadata is stored, never prompts or answers.

## How routing works

The model you pick is a ceiling. A router, [Jev](https://docs.digitalocean.com/products/inference/how-to/use-system-one-api/index.html.md), rates each new conversation from trivial to expert, and the gateway answers from that rung of the same provider:

| Request | Claude | OpenAI | Gemini |
| --- | --- | --- | --- |
| Trivial | Haiku 5.5, medium effort | GPT-6 Luna | Gemini 3.1 Flash-Lite |
| Routine | Haiku 5.5, high effort | GPT-6.1 Terra | Gemini 3.5 Flash-Lite |
| Involved | Sonnet 5.5 | GPT-6.1 Sol | Gemini 3.8 Flash |
| Expert | Opus 5.5 | GPT-6 Astra | Gemini 4 Carbon |

You can pick the models in the bottom two rows. A conversation stays on one rung. If you ask the same thing again, it quietly moves up one. If the router fails, the request stays on the model you picked.

## Quickstart

### 1. Check prerequisites

- Python 3.12 or later, and Node 20.19 or later
- An [Anthropic API key](https://console.anthropic.com/) (required)
- A [DigitalOcean inference key](https://docs.digitalocean.com/products/inference/how-to/use-system-one-api/index.html.md) for the Jev router (required for savings; without it nothing is routed)
- An [OpenAI](https://platform.openai.com/api-keys) or [Gemini](https://aistudio.google.com/apikey) API key (optional; each one adds that provider's models)

### 2. Configure the backend

```sh
git clone https://github.com/changcalvin/elastic-machines.git
cd elastic-machines/backend
cp .env.example .env
```

Open `backend/.env` and fill in your keys:

| Variable | Required | What it's for |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes | Claude models, and the judge used for quality checks |
| `MODEL_ACCESS_KEY` | For routing | Jev, the router. Without it every request stays on the model you picked |
| `OPENAI_API_KEY` | No | Adds GPT models |
| `GEMINI_API_KEY` | No | Adds Gemini models |
| `GATEWAY_KEYS` | Yes | Keys your users and tools present, as `key:team` pairs. The chat uses the first |
| `ADMIN_TOKEN` | Yes | Unlocks the `/admin` endpoints |
| `SHADOW_RATE` | No | Share of cheaper answers to quality-check. Default `0.03`, and `0` turns checks off |
| `SHADOW_DAILY_BUDGET_USD` | No | Daily spend cap for quality checks. Default `25` |

### 3. Start the backend

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn gateway.main:app --env-file .env
```

The gateway listens on http://localhost:8000.

### 4. Start the chat

In a second terminal:

```sh
cd elastic-machines/frontend
npm install
npm run dev
```

Open http://localhost:5173. Pick a model, chat, then open **Analytics** to see what each message cost against sending it to the model you picked. There's no sign-in: the dev server attaches the first key from `GATEWAY_KEYS` to each request.

### 5. Use it from Claude Code (optional)

Any Claude client works the same way:

```sh
export ANTHROPIC_BASE_URL=http://localhost:8000
export ANTHROPIC_AUTH_TOKEN=<a key from GATEWAY_KEYS>
claude
```

## API

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/messages` | The Claude Messages API, routed. Also takes GPT and Gemini model ids, text only |
| `GET /v1/models` | The models you can pick, from every provider with a key set |
| `POST /v1/messages/count_tokens` | Claude token counting, passed through |
| `GET /usage/{session}` | One chat's requests and costs, for the calling key's team |
| `GET /admin/summary`, `/admin/quality`, `/admin/requests` | Spend, quality and request log across all teams. Needs `ADMIN_TOKEN` |

## Development

```sh
cd backend
.venv/bin/pytest                                              # tests, against fake providers
DB_PATH=:memory: .venv/bin/python simulate_drift.py           # quality drift demo
DB_PATH=:memory: .venv/bin/python simulate_drift.py --sweep   # sample rate trade-offs
```

```
backend/
  gateway/     the gateway: routing, providers, pricing, quality checks
  tests/
frontend/      the chat and analytics pages
docs/          architecture, proposal and course notes
```

## Status

Everything works end to end against fake providers, and all tests pass. It hasn't been run against the live APIs yet, and the eval that measures real savings and quality hasn't started, so there are no measured results yet. Prices in `backend/gateway/pricing.py` are list prices from November 2026; Gemini 4's price doubles on December 1, 2026.

[docs/architecture.md](docs/architecture.md) covers the design, the quality checks and the open items. [docs/proposal.md](docs/proposal.md) has the original proposal and evaluation plan.

## License

[MIT](LICENSE)
