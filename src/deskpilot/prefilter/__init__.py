"""Request pre-filter: labels a goal text; it never grants permission."""

from deskpilot.prefilter.lexicon import LexiconHit, scan, strongest
from deskpilot.prefilter.normalize import normalize, variants

__all__ = ["LexiconHit", "normalize", "scan", "strongest", "variants"]
