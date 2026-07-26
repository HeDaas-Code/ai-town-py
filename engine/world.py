"""世界状态容器：玩家 / 对话 / agent 的运行期集合。

对应原项目 convex/aiTown/world.ts。所有游戏状态都在内存里维护，
每个 step 结束时由 Game 序列化并写入 SQLite。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .conversation import Conversation
from .ids import GameId
from .player import Player


@dataclass
class World:
    next_id: int = 0
    players: Dict[GameId, Player] = field(default_factory=dict)
    conversations: Dict[GameId, Conversation] = field(default_factory=dict)
    agents: Dict = field(default_factory=dict)  # Dict[GameId, Agent]，延迟注解

    @classmethod
    def from_dict(cls, d: dict) -> "World":
        from .agent import Agent
        return cls(
            next_id=d["nextId"],
            players={p["id"]: Player.from_dict(p) for p in d["players"]},
            conversations={c["id"]: Conversation.from_dict(c) for c in d["conversations"]},
            agents={a["id"]: Agent.from_dict(a) for a in d["agents"]},
        )

    def to_dict(self) -> dict:
        return {
            "nextId": self.next_id,
            "players": [p.to_dict() for p in self.players.values()],
            "conversations": [c.to_dict() for c in self.conversations.values()],
            "agents": [a.to_dict() for a in self.agents.values()],
        }

    def player_conversation(self, player: Player) -> Optional[Conversation]:
        return next(
            (c for c in self.conversations.values()
             if any(m.player_id == player.id for m in c.participants.values())),
            None,
        )
