"""List prices in $ per MTok. Confirm against each provider's pricing page before quoting savings."""

# (input, output, cache read). Claude cache writes are 1.25x input for 5 minutes, 2x for 1 hour.
PRICES = {
    # Anthropic, October 2026
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    # OpenAI, November 2026, prompts up to 272K tokens
    "gpt-6-astra": (10.00, 50.00, 1.00),
    "gpt-6.1-sol": (2.00, 10.00, 0.10),
    "gpt-6.1-terra": (1.00, 5.00, 0.05),
    "gpt-6-luna": (0.10, 0.50, 0.01),
    # Gemini, November 2026. Gemini 4 doubles on Dec 1 2026, and 3.8 Flash on Jan 1 2027.
    "gemini-4-carbon": (2.00, 10.00, 0.10),
    "gemini-3.8-flash": (0.75, 3.75, 0.075),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
}
HAIKU_LONG_PROMPT = 100_000
HAIKU_LONG_RATES = (0.50, 2.50, 0.05)
JEV_INPUT = 0.042  # output is free


def cost_usd(model: str, usage: dict) -> float | None:
    """Cost of one response from its usage block, or None for a model we have no price for."""
    if model not in PRICES:
        return None
    fresh = usage.get("input_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    writes = usage.get("cache_creation") or {}
    write_5m = writes.get("ephemeral_5m_input_tokens", usage.get("cache_creation_input_tokens") or 0)
    write_1h = writes.get("ephemeral_1h_input_tokens", 0)

    prompt = fresh + read + write_5m + write_1h
    long_haiku = model == "claude-haiku-5-5" and prompt > HAIKU_LONG_PROMPT
    in_rate, out_rate, read_rate = HAIKU_LONG_RATES if long_haiku else PRICES[model]
    return (
        fresh * in_rate
        + read * read_rate
        + write_5m * in_rate * 1.25
        + write_1h * in_rate * 2
        + (usage.get("output_tokens") or 0) * out_rate
    ) / 1_000_000
