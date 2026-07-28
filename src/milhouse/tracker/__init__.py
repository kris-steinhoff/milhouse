"""Issue tracking, as milhouse needs it.

:class:`~milhouse.tracker.base.Tracker` is the interface; :class:`BeadsTracker`
is the one implementation, over the ``bd`` CLI.
"""

from __future__ import annotations

from .base import Tracker
from .beads import BeadsTracker

__all__ = ["BeadsTracker", "Tracker"]
