"""Request pre-filter: labels a goal text; it never grants permission."""

from deskpilot.prefilter.decide import PrefilterDecision, evaluate
from deskpilot.prefilter.lexicon import LexiconHit, scan, strongest
from deskpilot.prefilter.normalize import normalize, variants

__all__ = [
    "LexiconHit",
    "PrefilterDecision",
    "evaluate",
    "normalize",
    "scan",
    "strongest",
    "variants",
]
