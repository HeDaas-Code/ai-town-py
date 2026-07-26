"""对话：成员状态机 + 走近/参与/离开的移动协调。

移植自原项目 convex/aiTown/conversation.ts。一次对话恰好两名成员，
状态流转：invited -> walkingOver -> participating。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import CONVERSATION_DISTANCE, TYPING_TIMEOUT_MS
from .geometry import distance, normalize, vector
from .ids import GameId
from .movement import blocked, move_player, stop_player
from .state_machine import MembershipStatus
from .types import Point


@dataclass
class ConversationMembership:
    player_id: GameId
    invited: float
    status: MembershipStatus

    def to_dict(self) -> dict:
        d = {"playerId": self.player_id, "invited": self.invited,
             "status": {"kind": self.status.kind}}
        if self.status.started is not None:
            d["status"]["started"] = self.status.started
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationMembership":
        s = d["status"]
        return cls(
            player_id=d["playerId"], invited=d["invited"],
            status=MembershipStatus(kind=s["kind"], started=s.get("started")),
        )


@dataclass
class Conversation:
    id: GameId
    creator: GameId
    created: float
    num_messages: int = 0
    participants: Dict[GameId, ConversationMembership] = field(default_factory=dict)
    is_typing: Optional[Dict] = None     # {player_id, message_uuid, since}
    last_message: Optional[Dict] = None  # {author, timestamp}

    # ---- 序列化 ----
    @classmethod
    def from_dict(cls, d: dict) -> "Conversation":
        return cls(
            id=d["id"], creator=d["creator"], created=d["created"],
            num_messages=d["numMessages"],
            participants={m["playerId"]: ConversationMembership.from_dict(m)
                          for m in d["participants"]},
            is_typing=d.get("isTyping"),
            last_message=d.get("lastMessage"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "creator": self.creator, "created": self.created,
            "isTyping": self.is_typing, "lastMessage": self.last_message,
            "numMessages": self.num_messages,
            "participants": [m.to_dict() for m in self.participants.values()],
        }

    # ---- tick ----
    def tick(self, game, now: float) -> None:
        if self.is_typing and self.is_typing["since"] + TYPING_TIMEOUT_MS < now:
            self.is_typing = None
        if len(self.participants) != 2:
            return

        ids = list(self.participants.keys())
        m1, m2 = self.participants[ids[0]], self.participants[ids[1]]
        p1 = game.world.players[ids[0]]
        p2 = game.world.players[ids[1]]
        d = distance(p1.position, p2.position)

        # 双方都 walkingOver 且足够近 -> 转 participating 并各自挪到相邻网格点。
        if m1.status.kind == "walkingOver" and m2.status.kind == "walkingOver":
            if d < CONVERSATION_DISTANCE:
                stop_player(p1)
                stop_player(p2)
                m1.status = MembershipStatus.participating(now)
                m2.status = MembershipStatus.participating(now)

                neighbors = lambda p: [Point(p.x + 1, p.y), Point(p.x - 1, p.y),
                                       Point(p.x, p.y + 1), Point(p.x, p.y - 1)]
                floor1 = Point(math.floor(p1.position.x), math.floor(p1.position.y))
                cands1 = [pt for pt in neighbors(floor1)
                          if blocked(game, now, pt, p1.id) is None]
                cands1.sort(key=lambda a: distance(a, p2.position))
                if cands1:
                    c1 = cands1[0]
                    cands2 = [pt for pt in neighbors(c1)
                              if blocked(game, now, pt, p2.id) is None]
                    cands2.sort(key=lambda a: distance(a, p2.position))
                    if cands2:
                        c2 = cands2[0]
                        move_player(game, now, p1, c1, allow_in_conversation=True)
                        move_player(game, now, p2, c2, allow_in_conversation=True)

        # 双方 participating 且未移动 -> 互相面向对方。
        if m1.status.kind == "participating" and m2.status.kind == "participating":
            v = normalize(vector(p1.position, p2.position))
            if v is not None:
                if p1.pathfinding is None:
                    p1.facing = v
                if p2.pathfinding is None:
                    p2.facing = Vector(-v.dx, -v.dy)

    # ---- 生命周期 ----
    @classmethod
    def start(cls, game, now: float, player, invitee):
        if player.id == invitee.id:
            raise RuntimeError("Can't invite yourself to a conversation")
        for c in game.world.conversations.values():
            if any(m.player_id == player.id for m in c.participants.values()):
                return {"error": f"Player {player.id} is already in a conversation"}
        for c in game.world.conversations.values():
            if any(m.player_id == invitee.id for m in c.participants.values()):
                return {"error": f"Player {invitee.id} is already in a conversation"}

        conv_id = game.alloc_id("conversations")
        game.world.conversations[conv_id] = cls(
            id=conv_id, created=now, creator=player.id, num_messages=0,
            participants={
                player.id: ConversationMembership(player.id, now, MembershipStatus.walking_over()),
                invitee.id: ConversationMembership(invitee.id, now, MembershipStatus.invited()),
            },
        )
        # 通知对话路由：新对话已建立（agent 注册与消息转发由 router 负责）。
        if game.dialogue_router is not None:
            game.dialogue_router.on_conversation_started(conv_id, player.id, invitee.id)
        return {"conversation_id": conv_id}

    def set_is_typing(self, now: float, player, message_uuid: str) -> None:
        if self.is_typing and self.is_typing["player_id"] != player.id:
            raise RuntimeError(f"Player {self.is_typing['player_id']} is already typing in {self.id}")
        self.is_typing = {"player_id": player.id, "message_uuid": message_uuid, "since": now}

    def accept_invite(self, game, player) -> None:
        m = self.participants.get(player.id)
        if m is None:
            raise RuntimeError(f"Player {player.id} not in conversation {self.id}")
        if m.status.kind != "invited":
            raise RuntimeError(f"Invalid membership status for {player.id}:{self.id}")
        m.status = MembershipStatus.walking_over()

    def reject_invite(self, game, now: float, player) -> None:
        m = self.participants.get(player.id)
        if m is None or m.status.kind != "invited":
            raise RuntimeError(f"Rejecting invite in wrong state: {self.id}:{player.id}")
        self.stop(game, now)

    def stop(self, game, now: float) -> None:
        self.is_typing = None
        for pid in list(self.participants.keys()):
            agent = next((a for a in game.world.agents.values() if a.player_id == pid), None)
            if agent is not None:
                agent.last_conversation = now
                agent.to_remember = self.id
        # 通知路由：对话结束。
        if game.dialogue_router is not None:
            game.dialogue_router.on_conversation_ended(self.id)
        game.world.conversations.pop(self.id, None)

    def leave(self, game, now: float, player) -> None:
        if player.id not in self.participants:
            raise RuntimeError(f"Couldn't find membership for {self.id}:{player.id}")
        self.stop(game, now)


# 用于在 conversation.tick 里反向 facing 的便捷引用
from .types import Vector  # noqa: E402
