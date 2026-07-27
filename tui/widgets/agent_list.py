"""Left column: list of agents with status icons."""
from __future__ import annotations

from textual.widgets import Static

_STATUS_ICON = {
    "idle": "[idle]",
    "walking": "[walk]",
    "busy": "[busy]",
    "talking": "[talk]",
}


class AgentList(Static):
    """Displays the agent roster. The app calls ``update_agents`` each refresh.

    ``agents`` is a list of ``(index, name, status, is_selected)`` tuples.
    """

    def update_agents(self, agents) -> None:
        lines = []
        for index, name, status, is_selected in agents:
            marker = ">" if is_selected else " "
            icon = _STATUS_ICON.get(status, "[?]")
            lines.append(f"{marker}{index:>2} {name:<10} {icon}")
        self.update("\n".join(lines))
