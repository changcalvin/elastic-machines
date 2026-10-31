"""The feedback loop: judged answers go in, routing strictness comes out. No I/O, so the
simulator and tests can drive it directly.

Each segment (a task type or a team) sits on one rung of a ladder and keeps two CUSUM
statistics. CUSUM is a sequential test: unlike re-checking a confidence interval after every
sample, it stays reliable when it's looked at continuously.
"""
from math import log, sqrt
from typing import NamedTuple

from .policy import CONFIDENCE

TARGET = 0.02  # the proposal's bar: judged worse than the reference on under 2% of answers
ALARM_RATE = 0.06  # a rate worth moving up for
CLEAR_RATE = 0.01  # a rate low enough to earn moving down
TIGHTEN_AT = 4.0  # 4 misses in a row; at a true 25% this moves in about 15 checks
LOOSEN_AT = 4.0  # about 400 clean checks in a row, so loosening is rare right at the target

# Each rung is (confidence, minimum tier). Moving right sends more requests up a tier. The top
# rungs set a floor because a higher bar changes nothing when Jev is confidently wrong.
# The ladder stops at a Sonnet floor: past that nothing cheap would run, so nothing could be
# measured and a segment could never come back down.
LADDER = [(0.70, 0), (0.80, 0), (CONFIDENCE, 0), (0.95, 0), (0.98, 0), (0.99, 1), (0.99, 2)]
DEFAULT_RUNG = 2
TOP_RUNG = len(LADDER) - 1


class Segment(NamedTuple):
    rung: int = DEFAULT_RUNG
    tighten: float = 0.0
    loosen: float = 0.0
    checks: int = 0  # since the last move
    worse: int = 0


def evidence(rate: float, worse: bool) -> float:
    """Log-likelihood ratio: how much one verdict favours `rate` over the target."""
    return log(rate / TARGET) if worse else log((1 - rate) / (1 - TARGET))


def observe(segment: Segment, worse: bool) -> Segment:
    """One verdict in, the segment's next state out. A move starts its evidence from zero."""
    tighten = max(0.0, segment.tighten + evidence(ALARM_RATE, worse))
    loosen = max(0.0, segment.loosen + evidence(CLEAR_RATE, worse))
    if tighten >= TIGHTEN_AT:
        if segment.rung < TOP_RUNG:
            return Segment(segment.rung + 1)
        tighten = TIGHTEN_AT  # at the limit: stays flagged rather than growing without bound
    if loosen >= LOOSEN_AT and segment.rung > 0:
        return Segment(segment.rung - 1)
    return Segment(segment.rung, tighten, loosen, segment.checks + 1, segment.worse + worse)


def wilson(worse: int, checks: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval on the true rate, sound for small samples and rates near zero."""
    if not checks:
        return 0.0, 1.0
    rate = worse / checks
    denominator = 1 + z * z / checks
    centre = rate + z * z / (2 * checks)
    margin = z * sqrt(rate * (1 - rate) / checks + z * z / (4 * checks * checks))
    return max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator)
