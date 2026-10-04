"""Herdr event handlers."""

from __future__ import annotations

from .cascades import handle_tab_closed, handle_workspace_closed
from .pane_close import handle_pane_closed
from .status import handle_agent_status_changed

__all__ = [
    "handle_agent_status_changed",
    "handle_pane_closed",
    "handle_tab_closed",
    "handle_workspace_closed",
]



