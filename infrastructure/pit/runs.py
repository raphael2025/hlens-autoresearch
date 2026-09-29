"""Compatibility exports for the neutral bounded sorted-run implementation.

New infrastructure consumers should import :mod:`infrastructure.streaming.runs` directly.
PIT keeps this module so existing callers and persisted v1 run format behavior remain stable.
"""

from infrastructure.streaming import runs as _runs

__all__ = _runs.__all__
globals().update({name: getattr(_runs, name) for name in __all__})
