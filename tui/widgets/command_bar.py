"""Bottom bar: command input.

A thin subclass of Textual's ``Input``. ``Input`` already emits
``Input.Submitted`` (with a ``.value`` field) when the user presses Enter,
so no custom message is needed. The subclass exists for naming clarity
and future customization.
"""
from __future__ import annotations

from textual.widgets import Input


class CommandBar(Input):
    """Single-line command input. Emits ``Input.Submitted`` on Enter."""
    pass
