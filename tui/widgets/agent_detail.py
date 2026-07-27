"""Right column: status / goal / today's log / recent memories."""
from __future__ import annotations

from textual.widgets import RichLog

from tui.snapshot import AgentSnapshot
from tui.activity_log import LogEntry


class AgentDetail(RichLog):
    """Shows four blocks for the selected agent.

    The app calls ``update_detail`` each refresh with the snapshot, today's
    logs, and recent memories.
    """

    def update_detail(self, snapshot: AgentSnapshot, today_logs, memories) -> None:
        self.clear()
        self.write(f"[{snapshot.name}]  ({snapshot.player_id})")
        self.write("")
        self.write("-- Status --")
        self.write(f"  state:    {snapshot.status}")
        self.write(f"  position: ({snapshot.position[0]:.1f}, {snapshot.position[1]:.1f})")
        if snapshot.conversation_partner:
            self.write(f"  talking:  {snapshot.conversation_partner}")
        if snapshot.activity_description:
            self.write(f"  activity: {snapshot.activity_description}")
        self.write("")
        self.write("-- Goal --")
        self.write(f"  {snapshot.goal}")
        if snapshot.plan:
            self.write("")
            self.write("-- Plan --")
            for line in snapshot.plan.splitlines()[:3]:
                self.write(f"  {line.strip()}")
        self.write("")
        self.write("-- Today --")
        if not today_logs:
            self.write("  (no activity yet)")
        for entry in today_logs[:20]:
            ts = _format_ts(entry.timestamp_ms)
            self.write(f"  {ts}  {entry.text}")
        self.write("")
        self.write("-- Recent memories --")
        if not memories:
            self.write("  (none)")
        for mem in memories[:3]:
            imp = getattr(mem, "importance", 0)
            desc = getattr(mem, "description", "")
            self.write(f"  [imp {imp:.0f}] {desc}")


def _format_ts(ts_ms: int) -> str:
    """Format a millisecond timestamp as HH:MM (best-effort, UTC-agnostic)."""
    if ts_ms <= 0:
        return "--:--"
    s = ts_ms // 1000
    h = (s // 3600) % 24
    m = (s // 60) % 60
    return f"{h:02d}:{m:02d}"
