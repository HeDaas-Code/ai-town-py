"""玩家描述（姓名 / 角色 / 自我介绍）与 agent 描述（identity / plan）。

对应原项目 playerDescription.ts / agentDescription.ts。
拆分到独立模块以便 player.py / agent.py 复用。
"""
from __future__ import annotations

from dataclasses import dataclass

from .ids import GameId


@dataclass
class PlayerDescription:
    player_id: GameId
    character: str
    description: str
    name: str

    def to_dict(self) -> dict:
        return {"playerId": self.player_id, "character": self.character,
                "description": self.description, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerDescription":
        return cls(player_id=d["playerId"], character=d["character"],
                   description=d["description"], name=d["name"])


@dataclass
class AgentDescription:
    agent_id: GameId
    identity: str
    plan: str

    def to_dict(self) -> dict:
        return {"agentId": self.agent_id, "identity": self.identity, "plan": self.plan}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentDescription":
        return cls(agent_id=d["agentId"], identity=d["identity"], plan=d["plan"])
