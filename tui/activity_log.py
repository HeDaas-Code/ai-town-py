"""Collect per-agent activity events for TUI display.

Two sources of events:
1. ``on_routed_message``: registered as a DialogueRouter observer, fires on
   every chat message (engine thread).
2. ``diff_world``: called from the TUI refresh tick, compares current
   ``game.world`` against the previous snapshot to detect conversation
   start/end, pathfinding start/end, and activity start/end.

Thread safety: ``on_routed_message`` runs on the engine thread;
``diff_world`` / ``get_today_logs`` run on the TUI thread. A lock guards
the internal list. Textual's ``call_from_thread`` is used by the app to
schedule UI updates, but the log itself is safe to read/write from any
thread due to the lock.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LogEntry:
    timestamp_ms: int
    player_id: str
    kind: str  # "message" | "conversation_start" | "conversation_end" | "path_start" | "path_end" | "activity_start" | "activity_end"
    text: str


class ActivityLog:
    def __init__(self, max_entries: int = 50):
        self._max = max_entries
        self._logs: Dict[str, List[LogEntry]] = defaultdict(list)
        self._lock = threading.Lock()
        # Previous-state cache for diff_world: player_id -> (pathfinding_dest, activity_desc, conv_id)
        self._prev_state: Dict[str, tuple] = {}

    def add_entry(self, entry: LogEntry) -> None:
        with self._lock:
            self._logs[entry.player_id].append(entry)
            if len(self._logs[entry.player_id]) > self._max:
                self._logs[entry.player_id] = self._logs[entry.player_id][-self._max:]

    def on_routed_message(self, msg: Any) -> None:
        """DialogueRouter observer callback. ``msg`` is a RoutedMessage."""
        ts = int(getattr(msg, "timestamp", 0))
        author = getattr(msg, "author", "")
        recipient = getattr(msg, "recipient", "")
        text = getattr(msg, "text", "")
        self.add_entry(LogEntry(ts, author, "message", f"sent: {text}"))
        self.add_entry(LogEntry(ts, recipient, "message", f"received: {text}"))

    def diff_world(self, game: Any, now_ms: int) -> None:
        """Detect state transitions by comparing against the previous snapshot.

        Call once per refresh tick. Records:
        - conversation start/end (per player)
        - pathfinding start/end (player started/stopped walking to a destination)
        - activity start/end (player started/stopped an activity like reading)
        """
        world = game.world
        # Track which conversations each player is in this tick.
        # Snapshot with list() because the engine thread mutates these dicts
        # during tick(); iterating a live dict raises RuntimeError if it
        # changes size mid-iteration.
        current_player_conv: Dict[str, str] = {}
        for conv_id, conv in list(world.conversations.items()):
            for pid in list(conv.participants.keys()):
                current_player_conv[pid] = conv_id

        for player_id, player in list(world.players.items()):
            dest: Optional[str] = None
            if player.pathfinding is not None:
                d = player.pathfinding.get("destination")
                if d is not None:
                    dest = f"({d.x:.0f},{d.y:.0f})"
            activity_desc: Optional[str] = None
            if player.activity is not None:
                activity_desc = player.activity.description
            conv_id = current_player_conv.get(player_id)
            current = (dest, activity_desc, conv_id)
            prev = self._prev_state.get(player_id)

            if prev is not None:
                prev_dest, prev_act, prev_conv = prev
                # Conversation transitions
                if prev_conv is None and conv_id is not None:
                    self.add_entry(LogEntry(now_ms, player_id, "conversation_start", "started a conversation"))
                elif prev_conv is not None and conv_id is None:
                    self.add_entry(LogEntry(now_ms, player_id, "conversation_end", "ended a conversation"))
                # Pathfinding transitions
                if prev_dest is None and dest is not None:
                    self.add_entry(LogEntry(now_ms, player_id, "path_start", f"heading to {dest}"))
                elif prev_dest is not None and dest is None:
                    self.add_entry(LogEntry(now_ms, player_id, "path_end", "arrived or stopped"))
                # Activity transitions
                if prev_act is None and activity_desc is not None:
                    self.add_entry(LogEntry(now_ms, player_id, "activity_start", f"started {activity_desc}"))
                elif prev_act is not None and activity_desc is None:
                    self.add_entry(LogEntry(now_ms, player_id, "activity_end", "finished activity"))
            self._prev_state[player_id] = current

        # Forget players that left the world
        gone = set(self._prev_state.keys()) - set(world.players.keys())
        for pid in gone:
            self._prev_state.pop(pid, None)

    def get_today_logs(self, player_id: str) -> List[LogEntry]:
        with self._lock:
            entries = list(self._logs.get(player_id, []))
        entries.sort(key=lambda e: e.timestamp_ms, reverse=True)
        return entries
