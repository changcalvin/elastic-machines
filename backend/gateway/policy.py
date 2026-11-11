"""Turns Jev's answers into a tier. No I/O, so the eval replays exactly what production ran."""
from typing import NamedTuple


class Tier(NamedTuple):
    name: str  # unique across providers, because the request log stores it
    model: str
    label: str
    effort: str | None = None  # None keeps whatever the client sent


LEVELS = 4  # one tier per Jev complexity level, in every provider

# Each provider routes within its own models, cheapest first. A request never crosses providers.
TIERS = {
    "anthropic": [
        Tier("haiku", "claude-haiku-5-5", "Haiku 5.5", "medium"),
        Tier("haiku-high", "claude-haiku-5-5", "Haiku 5.5, high effort", "high"),
        Tier("sonnet", "claude-sonnet-5-5", "Sonnet 5.5"),
        Tier("opus", "claude-opus-5-5", "Opus 5.5"),
    ],
    "openai": [
        Tier("gpt-luna", "gpt-6-luna", "GPT-6 Luna"),
        Tier("gpt-terra", "gpt-6.1-terra", "GPT-6.1 Terra"),
        Tier("gpt-sol", "gpt-6.1-sol", "GPT-6.1 Sol"),
        Tier("gpt-astra", "gpt-6-astra", "GPT-6 Astra"),
    ],
    "gemini": [
        Tier("gemini-flash-lite", "gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
        Tier("gemini-flash-lite-3.5", "gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
        Tier("gemini-flash", "gemini-3.8-flash", "Gemini 3.8 Flash"),
        Tier("gemini-carbon", "gemini-4-carbon", "Gemini 4 Carbon"),
    ],
}

# The model a client asks for is a ceiling, and is always the model on that rung. Others pass through.
CEILINGS = {
    "claude-opus-5-5": 3,
    "claude-sonnet-5-5": 2,
    "gpt-6-astra": 3,
    "gpt-6.1-sol": 2,
    "gemini-4-carbon": 3,
    "gemini-3.8-flash": 2,
}

LEVEL_OF = {tier.name: level for tiers in TIERS.values() for level, tier in enumerate(tiers)}
LABEL_OF = {tier.name: tier.label for tiers in TIERS.values() for tier in tiers}


def provider_of(model: str) -> str:
    return "openai" if model.startswith("gpt-") else "gemini" if model.startswith("gemini-") else "anthropic"


def model_label(model: str) -> str:
    """A ceiling model's display name, or the raw id for anything else."""
    return TIERS[provider_of(model)][CEILINGS[model]].label if model in CEILINGS else model


QUESTIONS = {
    "complexity": {
        "type": "score",
        "instructions": "How capable a model does answering this request well require?",
        # One level per tier, lowest first.
        "criteria": [
            "Trivial: rewording, formatting, a one-step fact or formula",
            "Routine: a short summary, a simple script, a clear how-to",
            "Involved: several steps, tradeoffs, or non-trivial code",
            "Expert: hard reasoning, architecture, debugging across many parts",
        ],
    },
    "task": {
        "type": "choice",
        "instructions": "What kind of work is this request?",
        "criteria": {
            "transform": "Rewrite, translate, format, extract or summarize text the user supplied",
            "answer": "Answer a question or give advice from general knowledge",
            "code": "Write, change or debug code",
            "analysis": "Reason through data, plans or decisions with tradeoffs",
        },
    },
    "stakes": {
        "type": "noul",
        "instructions": "A subtly wrong answer to this request would cost the user significant time, money or trust.",
    },
}

TASK_FLOOR = {"transform": 0, "answer": 0, "code": 1, "analysis": 2}
CONFIDENCE = 0.9  # sweep this for the cost-quality curves
STAKES_BUMP = 0.3


def choose_tier(answers: dict, confidence: float = CONFIDENCE, floor: int = 0) -> int:
    """Lowest tier we're `confidence` sure is enough, so any doubt rounds up, and never below `floor`."""
    levels = answers["complexity"]["probabilities"]
    tier, covered = LEVELS - 1, 0.0
    for level in range(LEVELS):
        covered += levels[str(level)]
        if covered >= confidence:
            tier = level
            break

    tasks = answers["task"]["probabilities"]
    plausible = [TASK_FLOOR[task] for task, p in tasks.items() if p >= 1 - confidence]
    tier = max(tier, *plausible, floor)

    if answers["stakes"]["noul"] >= STAKES_BUMP:
        tier += 1
    return min(tier, LEVELS - 1)
