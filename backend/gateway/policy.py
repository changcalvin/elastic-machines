"""Turns Jev's answers into a tier. No I/O, so the eval replays exactly what production ran."""
from typing import NamedTuple


class Tier(NamedTuple):
    name: str
    model: str
    effort: str | None = None  # None keeps whatever the client sent


TIERS = [
    Tier("haiku", "claude-haiku-5-5", "medium"),
    Tier("haiku-high", "claude-haiku-5-5", "high"),
    Tier("sonnet", "claude-sonnet-5-5"),
    Tier("opus", "claude-opus-5-5"),
]

# The model a client asks for is a ceiling. Any other model passes through untouched.
CEILINGS = {"claude-sonnet-5-5": 2, "claude-opus-5-5": 3}

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
    tier, covered = len(TIERS) - 1, 0.0
    for level in range(len(TIERS)):
        covered += levels[str(level)]
        if covered >= confidence:
            tier = level
            break

    tasks = answers["task"]["probabilities"]
    plausible = [TASK_FLOOR[task] for task, p in tasks.items() if p >= 1 - confidence]
    tier = max(tier, *plausible, floor)

    if answers["stakes"]["noul"] >= STAKES_BUMP:
        tier += 1
    return min(tier, len(TIERS) - 1)
