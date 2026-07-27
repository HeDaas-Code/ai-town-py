"""Textual App that wires together all TUI widgets.

Engine runs on its own daemon thread (started by main_tui.py before
constructing the app). The app reads game state on a 500ms timer and
pushes updates to the widgets. Routed messages arrive on the engine
thread via the observer; we use ``call_from_thread`` to schedule the
ActivityLog write safely (the log itself is also lock-guarded).
"""
from __future__ import annotations

from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal

from tui.activity_log import ActivityLog
from tui.commands import parse_command, JumpById, JumpByName, Lock, Unlock, ListCmd, Help, Quit, Unknown
from tui.snapshot import build_snapshot
from tui.widgets.agent_list import AgentList
from tui.widgets.agent_detail import AgentDetail
from tui.widgets.command_bar import CommandBar
from tui.widgets.map_view import MapView

REFRESH_INTERVAL_MS = 500


class TownApp(App):
    CSS = """
    Screen { layout: vertical; }
    #main-row { height: 1fr; }
    #agent-list { width: 24; border: solid $primary; padding: 0 1; }
    #map-view { width: 1fr; border: solid $primary; padding: 0 1; }
    #agent-detail { width: 40; border: solid $primary; padding: 0 1; }
    #command-bar { height: 3; border: solid $accent; }
    """

    def __init__(self, game):
        super().__init__()
        self.game = game
        self.activity_log = ActivityLog()
        self.selected_player_id: Optional[str] = None
        self.locked = True
        self._should_exit = False
        # ordered list of player_ids (roster)
        self._roster: list = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-row"):
            yield AgentList(id="agent-list")
            yield MapView(id="map-view")
            yield AgentDetail(id="agent-detail")
        yield CommandBar(id="command-bar", placeholder="> 输入命令: <数字> 或 :jump <名字> 或 :help")

    @property
    def command_bar(self) -> CommandBar:
        return self.query_one("#command-bar", CommandBar)

    def on_mount(self) -> None:
        # Register as a router observer so chat messages flow into the activity log
        self.game.dialogue_router.add_observer(self._on_routed_message)
        self.set_interval(REFRESH_INTERVAL_MS / 1000, self._refresh)

    def _on_routed_message(self, msg) -> None:
        """Engine-thread callback. Schedule the write on the TUI thread."""
        self.call_from_thread(self.activity_log.on_routed_message, msg)

    def _refresh(self) -> None:
        now_ms = int(__import__("time").time() * 1000)
        # Diff world for activity transitions
        self.activity_log.diff_world(self.game, now_ms)
        # Rebuild roster
        self._roster = list(self.game.world.players.keys())
        # Select first player if none selected
        if self.selected_player_id is None and self._roster:
            self.selected_player_id = self._roster[0]
        self._render()

    def _render(self) -> None:
        self._render_agent_list()
        self._render_map()
        self._render_detail()

    def _render_agent_list(self) -> None:
        agents = []
        for index, pid in enumerate(self._roster, start=1):
            snap = build_snapshot(self.game, pid)
            name = snap.name if snap else pid
            status = snap.status if snap else "idle"
            agents.append((index, name, status, pid == self.selected_player_id))
        self.query_one("#agent-list", AgentList).update_agents(agents)

    def _render_map(self) -> None:
        if self.selected_player_id is None:
            return
        player = self.game.world.players.get(self.selected_player_id)
        if player is None:
            return
        snap = build_snapshot(self.game, self.selected_player_id)
        mv = self.query_one("#map-view", MapView)
        grid = mv.render_map(
            self.game.world_map,
            player.position.x,
            player.position.y,
            view_w=39,
            view_h=17,
            players=self.game.world.players,
            selected_player_id=self.selected_player_id,
        )
        mv.update_view(grid, snap.name if snap else self.selected_player_id)

    def _render_detail(self) -> None:
        if self.selected_player_id is None:
            return
        snap = build_snapshot(self.game, self.selected_player_id)
        if snap is None:
            return
        logs = self.activity_log.get_today_logs(self.selected_player_id)
        memories = self._fetch_memories(self.selected_player_id)
        self.query_one("#agent-detail", AgentDetail).update_detail(snap, logs, memories)

    def _fetch_memories(self, player_id: str):
        brain = getattr(self.game, "brain", None)
        if brain is None:
            return []
        memory_store = getattr(brain, "_memory", None)
        if memory_store is None:
            return []
        try:
            return memory_store.list_recent(player_id, limit=3)
        except Exception:
            return []

    # ---- Command handling ----
    # Textual's Input emits Input.Submitted on Enter; the handler name is
    # on_input_submitted. event.value holds the submitted text.
    def on_input_submitted(self, event) -> None:
        if event.input.id != "command-bar":
            return
        event.input.value = ""  # clear the input after submit
        cmd = parse_command(event.value)
        if isinstance(cmd, JumpById):
            if 1 <= cmd.index <= len(self._roster):
                self.selected_player_id = self._roster[cmd.index - 1]
                self._render()
        elif isinstance(cmd, JumpByName):
            target = self._find_player_by_name(cmd.name)
            if target is not None:
                self.selected_player_id = target
                self._render()
        elif isinstance(cmd, Lock):
            self.locked = True
        elif isinstance(cmd, Unlock):
            self.locked = False
        elif isinstance(cmd, ListCmd):
            self._render_agent_list()
        elif isinstance(cmd, Help):
            self._show_help()
        elif isinstance(cmd, Quit):
            self._should_exit = True
            self.exit()
        elif isinstance(cmd, Unknown):
            pass  # ignore

    def _find_player_by_name(self, name: str) -> Optional[str]:
        name_lower = name.lower()
        for pid in self._roster:
            pdesc = self.game.player_descriptions.get(pid)
            if pdesc is not None and pdesc.name.lower() == name_lower:
                return pid
        return None

    def _show_help(self) -> None:
        help_text = (
            "Commands:\n"
            "  <number>        jump to agent by index\n"
            "  :jump <name>    jump to agent by name\n"
            "  :lock           lock view to selected agent\n"
            "  :unlock         unlock view (free camera)\n"
            "  :list           refresh agent list\n"
            "  :help           show this help\n"
            "  :quit           exit"
        )
        self.query_one("#agent-detail", AgentDetail).write(help_text)
