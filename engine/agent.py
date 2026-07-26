"""Agent 游戏状态：在每个 tick 里决策「做什么」。

移植自原项目 convex/aiTown/agent.ts。Agent 不直接调 LLM，而是通过
``start_operation`` 把长耗时任务（生成对话 / 记忆 / 决策）交给
``agent_brain.agent_ops`` 在后台线程执行，完成后回写 input 给引擎。
"""
from __future__ import annotations

import random
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Optional

from config import (
    ACTION_TIMEOUT_MS, AWKWARD_CONVERSATION_TIMEOUT_MS, CONVERSATION_COOLDOWN_MS,
    CONVERSATION_DISTANCE, INVITE_ACCEPT_PROBABILITY, INVITE_TIMEOUT_MS,
    MAX_CONVERSATION_DURATION_MS, MAX_CONVERSATION_MESSAGES, MESSAGE_COOLDOWN_MS,
    MIDPOINT_THRESHOLD,
)
from .chunk import world_to_chunk
from .geometry import distance
from .ids import GameId
from .movement import move_player
from .types import Point


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
            # 在无限世界中为 agent 预选一个跨 chunk 的远方目标
            candidate_dest = pick_far_destination(game, player)
            args = {
                "player": player.to_dict(),
                "otherFreePlayers": other_free,
                "agent": self.to_dict(),
                "map": game.world_map.to_dict(),
            }
            if candidate_dest is not None:
                args["candidateDestination"] = {
                    "x": candidate_dest.x, "y": candidate_dest.y,
                }
            self.start_operation(game, now, "agentDoSomething", args)
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


def _spawn_far_direction(cm, cx: int, cy: int, distance: int, player_id: str) -> Tuple[Optional[int], Optional[int]]:
    """沿确定性正交方向生成一串 chunk，返回最远端的 chunk 坐标。

    只用 N/S/E/W 四个正交方向，并强制在链上每个 chunk 的进出边开 portal，
    确保生成的 chunk 链与 seed 区域连通。
    """
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    idx = abs(hash(player_id)) % len(directions)
    dx, dy = directions[idx]

    edge_for = {
        (1, 0): "W",
        (-1, 0): "E",
        (0, 1): "N",
        (0, -1): "S",
    }
    opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}
    incoming = edge_for[(dx, dy)]       # 指向 seed/前一个 chunk 的边
    outgoing = opposite[incoming]       # 指向下一个 chunk 的边

    prev = (cx, cy)
    tx, ty = cx, cy
    for i in range(distance):
        tx += dx
        ty += dy
        # 中间 chunk 强制同时开进出 portal；最后一个 chunk 只开进入 portal
        forced = [incoming]
        if i < distance - 1:
            forced.append(outgoing)
        cm.ensure_chunk(tx, ty, forced_edges=forced)
        prev = (tx, ty)
    return tx, ty


def pick_far_destination(game, player, min_chunks: int = 3, max_chunks: int = 6) -> Optional[Point]:
    """基于 ChunkGraph 为 agent 选择一个跨 chunk 的远处可达目标。

    返回世界 tile 坐标（整数点），如果当前没有足够远的可达 chunk 则返回 None。
    """
    cm = game.world_map.chunk_manager
    chunk_size = cm.chunk_size
    cx, cy, _, _ = world_to_chunk(player.position.x, player.position.y, chunk_size)

    graph = cm.chunk_graph()
    if not graph.has_chunk((cx, cy)):
        return None

    # BFS 收集距离在 [min_chunks, max_chunks] 范围内的可达 chunk
    queue = deque([(cx, cy, 0)])
    visited = {(cx, cy)}
    candidates = []
    while queue:
        x, y, d = queue.popleft()
        if d > max_chunks:
            continue
        if min_chunks <= d <= max_chunks:
            candidates.append((x, y))
        for (nx, ny), _ in graph.neighbors((x, y)):
            if (nx, ny) not in visited:
                visited.add((nx, ny))
                queue.append((nx, ny, d + 1))

    if not candidates:
        # 当前世界还太小：沿一个随机方向主动生成一串 chunk，让 agent 能走向远方。
        tx, ty = _spawn_far_direction(cm, cx, cy, max_chunks, player.id)
        if tx is None:
            return None
    else:
        # 确定性选择：基于 agent id 和当前 generation 取一个稳定目标，
        # 避免所有 agent 都涌向同一个方向。
        idx = (hash(player.id) + game.generation) % len(candidates)
        tx, ty = candidates[idx]

    # 确保目标 chunk 已加载（会触发生成）
    cm.ensure_chunk(tx, ty)

    # 选择目标 chunk 内部一个可通行点：先尝试中心，再尝试 portal 附近
    center_x = tx * chunk_size + chunk_size // 2
    center_y = ty * chunk_size + chunk_size // 2
    target_chunk = cm.get_chunk(tx, ty)
    if target_chunk is not None and target_chunk.portals:
        # 使用第一个 portal 往内部偏 2 tile 的位置，通常可通行
        p = target_chunk.portals[0]
        if p.edge in ("N", "S"):
            center_x = tx * chunk_size + int(p.local_x)
            center_y = ty * chunk_size + int(p.local_y) + (2 if p.edge == "N" else -2)
        else:
            center_x = tx * chunk_size + int(p.local_x) + (2 if p.edge == "W" else -2)
            center_y = ty * chunk_size + int(p.local_y)

    return Point(float(center_x), float(center_y))
