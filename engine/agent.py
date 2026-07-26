"""Agent 游戏状态：在每个 tick 里决策「做什么」。

移植自原项目 convex/aiTown/agent.ts。Agent 不直接调 LLM，而是通过
``start_operation`` 把长耗时任务（生成对话 / 记忆 / 决策）交给
``agent_brain.agent_ops`` 在后台线程执行，完成后回写 input 给引擎。
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Optional

from config import (
    ACTION_TIMEOUT_MS, AWKWARD_CONVERSATION_TIMEOUT_MS, CONVERSATION_COOLDOWN_MS,
    CONVERSATION_DISTANCE, INVITE_ACCEPT_PROBABILITY, INVITE_TIMEOUT_MS,
    MAX_CONVERSATION_DURATION_MS, MAX_CONVERSATION_MESSAGES, MESSAGE_COOLDOWN_MS,
    MIDPOINT_THRESHOLD,
)
from .geometry import distance
from .ids import GameId
from .movement import move_player


@dataclass
class InProgressOperation:
    name: str
    operation_id: str
    started: float


@dataclass
class Agent:
    id: GameId
    player_id: GameId
    to_remember: Optional[GameId] = None
    last_conversation: Optional[float] = None
    last_invite_attempt: Optional[float] = None
    in_progress_operation: Optional[InProgressOperation] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Agent":
        op = d.get("inProgressOperation")
        op = InProgressOperation(**op) if op is not None else None
        return cls(
            id=d["id"], player_id=d["playerId"],
            to_remember=d.get("toRemember"),
            last_conversation=d.get("lastConversation"),
            last_invite_attempt=d.get("lastInviteAttempt"),
            in_progress_operation=op,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "playerId": self.player_id, "toRemember": self.to_remember,
            "lastConversation": self.last_conversation,
            "lastInviteAttempt": self.last_invite_attempt,
            "inProgressOperation": (self.in_progress_operation.__dict__
                                    if self.in_progress_operation else None),
        }

    # ---- 主 tick ----
    def tick(self, game, now: float) -> None:
        player = game.world.players.get(self.player_id)
        if player is None:
            raise RuntimeError(f"Invalid player ID {self.player_id}")

        # 等待进行中的操作完成；超时则放弃。
        if self.in_progress_operation is not None:
            if now < self.in_progress_operation.started + ACTION_TIMEOUT_MS:
                return
            self.in_progress_operation = None

        conversation = game.world.player_conversation(player)
        member = None
        if conversation is not None:
            member = conversation.participants.get(player.id)

        recently_attempted_invite = (
            self.last_invite_attempt is not None
            and now < self.last_invite_attempt + CONVERSATION_COOLDOWN_MS
        )
        doing_activity = player.activity is not None and player.activity.until > now
        if doing_activity and (conversation is not None or player.pathfinding is not None):
            player.activity.until = now

        # 没在对话、没在做活动、没在走或没刚尝试邀请 -> 决策「做什么」。
        if (conversation is None and not doing_activity
                and (player.pathfinding is None or not recently_attempted_invite)):
            other_free = [
                p.to_dict() for p in game.world.players.values()
                if p.id != player.id
                and not any(p.id in [m.player_id for m in c.participants.values()]
                            for c in game.world.conversations.values())
            ]
            self.start_operation(game, now, "agentDoSomething", {
                "player": player.to_dict(),
                "otherFreePlayers": other_free,
                "agent": self.to_dict(),
                "map": game.world_map.to_dict(),
            })
            return

        # 记忆上一段对话。
        if self.to_remember is not None:
            self.start_operation(game, now, "agentRememberConversation", {
                "playerId": self.player_id, "agentId": self.id,
                "conversationId": self.to_remember,
            })
            self.to_remember = None
            return

        if conversation is not None and member is not None:
            other_id, other_member = next(
                (pid, m) for pid, m in conversation.participants.items() if pid != player.id
            )
            other_player = game.world.players[other_id]

            # invited -> 按概率接受 / 拒绝。
            if member.status.kind == "invited":
                if other_player.human or random.random() < INVITE_ACCEPT_PROBABILITY:
                    conversation.accept_invite(game, player)
                    if player.pathfinding is not None:
                        player.pathfinding = None
                else:
                    conversation.reject_invite(game, now, player)
                return

            # walkingOver -> 朝对方走，太久了就放弃。
            if member.status.kind == "walkingOver":
                if member.invited + INVITE_TIMEOUT_MS < now:
                    conversation.leave(game, now, player)
                    return
                pd = distance(player.position, other_player.position)
                if pd < CONVERSATION_DISTANCE:
                    return
                if player.pathfinding is None:
                    if pd < MIDPOINT_THRESHOLD:
                        dest = {"x": math_floor(other_player.position.x),
                                "y": math_floor(other_player.position.y)}
                    else:
                        dest = {"x": math_floor((player.position.x + other_player.position.x) / 2),
                                "y": math_floor((player.position.y + other_player.position.y) / 2)}
                    from .types import Point
                    move_player(game, now, player, Point(dest["x"], dest["y"]))
                return

            # participating -> 生成消息（start / continue / leave）。
            if member.status.kind == "participating":
                started = member.status.started
                if conversation.is_typing and conversation.is_typing["player_id"] != player.id:
                    return
                if conversation.last_message is None:
                    is_initiator = conversation.creator == player.id
                    awkward_deadline = started + AWKWARD_CONVERSATION_TIMEOUT_MS
                    if is_initiator or awkward_deadline < now:
                        msg_uuid = str(uuid.uuid4())
                        conversation.set_is_typing(now, player, msg_uuid)
                        self.start_operation(game, now, "agentGenerateMessage", {
                            "playerId": player.id, "conversationId": conversation.id,
                            "otherPlayerId": other_player.id, "messageUuid": msg_uuid,
                            "type": "start",
                        })
                    return
                too_long = started + MAX_CONVERSATION_DURATION_MS
                if too_long < now or conversation.num_messages > MAX_CONVERSATION_MESSAGES:
                    msg_uuid = str(uuid.uuid4())
                    conversation.set_is_typing(now, player, msg_uuid)
                    self.start_operation(game, now, "agentGenerateMessage", {
                        "playerId": player.id, "conversationId": conversation.id,
                        "otherPlayerId": other_player.id, "messageUuid": msg_uuid,
                        "type": "leave",
                    })
                    return
                if conversation.last_message["author"] == player.id:
                    awkward_deadline = conversation.last_message["timestamp"] + AWKWARD_CONVERSATION_TIMEOUT_MS
                    if now < awkward_deadline:
                        return
                cooldown = conversation.last_message["timestamp"] + MESSAGE_COOLDOWN_MS
                if now < cooldown:
                    return
                msg_uuid = str(uuid.uuid4())
                conversation.set_is_typing(now, player, msg_uuid)
                self.start_operation(game, now, "agentGenerateMessage", {
                    "playerId": player.id, "conversationId": conversation.id,
                    "otherPlayerId": other_player.id, "messageUuid": msg_uuid,
                    "type": "continue",
                })
                return

    # ---- 调度操作 ----
    def start_operation(self, game, now: float, name: str, args: dict) -> None:
        if self.in_progress_operation is not None:
            raise RuntimeError(
                f"Agent {self.id} already has an operation: {self.in_progress_operation}"
            )
        operation_id = game.alloc_id("operations")
        game.schedule_operation(name, {"operationId": operation_id, **args})
        self.in_progress_operation = InProgressOperation(name=name, operation_id=operation_id, started=now)


# 避免在文件顶部 import math 与 math.floor 产生循环引用的微优化
def math_floor(x: float) -> int:
    import math
    return math.floor(x)
