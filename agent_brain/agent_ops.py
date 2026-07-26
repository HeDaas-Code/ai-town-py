"""Agent 操作：在后台线程执行 LLM 调用，把结果作为 input 回写给 engine。

对应原项目 convex/aiTown/agentOperations.ts。三类操作：
- agentDoSomething          : 决定下一步（漫游 / 活动 / 邀请某人）
- agentGenerateMessage      : 生成对话消息（start / continue / leave）
- agentRememberConversation : 把刚结束的对话总结成记忆

原项目用 Convex 的 scheduler 在云端跑这些 action；本项目用
``concurrent.futures.ThreadPoolExecutor`` 在本地后台跑，完成后通过
``Game.enqueue_input`` 把 finish input 推回 engine 主循环。
"""
from __future__ import annotations

import random
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from config import (
    ACTIVITIES, ACTIVITY_COOLDOWN_MS, CONVERSATION_COOLDOWN_MS,
)
from db import Database
from dialogue import DialogueRouter, RoutedMessage
from llm import LLMClient
from .memory import MemoryStore, remember_conversation
from .conversation_ops import (
    start_conversation_message, continue_conversation_message,
    leave_conversation_message, find_conversation_candidate,
)


class AgentBrain:
    """负责跑所有 LLM 操作的后台大脑。

    与 Game 解耦：Game 调 ``submit(name, args)``，brain 在线程池里跑完后
    通过 ``on_finish`` 回调把 input 推回 Game。
    """

    def __init__(
        self,
        llm: LLMClient,
        db: Database,
        world_id: str,
        dialogue_router: DialogueRouter,
        memory_store: MemoryStore,
        on_finish: Callable[[str, Dict[str, Any]], None],
        max_workers: int = 4,
    ):
        self._llm = llm
        self._db = db
        self._world_id = world_id
        self._router = dialogue_router
        self._memory = memory_store
        self._on_finish = on_finish
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="brain")
        # 让 operation handler 能拿到当前的 descriptions 快照
        self.player_descriptions_snapshot: Dict[str, Any] = {}
        self.agent_descriptions_snapshot: Dict[str, Any] = {}

    # ---- 提交操作 ----
    def submit(self, name: str, args: Dict[str, Any]) -> Future:
        return self._executor.submit(self._run, name, args)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    # ---- 内部派发 ----
    def _run(self, name: str, args: Dict[str, Any]) -> None:
        try:
            handler = self._HANDLERS.get(name)
            if handler is None:
                raise ValueError(f"Unknown agent operation: {name}")
            handler(self, args)
        except Exception as e:  # noqa: BLE001
            # 失败也要让 engine 把 in_progress_operation 清掉
            operation_id = args.get("operationId", "")
            agent_id = args.get("agentId", "")
            # agentGenerateMessage 的话，typing 状态也要清
            if name == "agentGenerateMessage":
                self._on_finish("agentFinishSendingMessage", {
                    "agentId": agent_id,
                    "conversationId": args.get("conversationId"),
                    "timestamp": int(time.time() * 1000),
                    "operationId": operation_id,
                    "leaveConversation": False,
                    "error": str(e),
                })
            else:
                # 让 engine 超时清理 in_progress_operation
                print(f"[brain] operation {name} failed: {e}")

    # ---- 具体操作 ----
    def _agent_do_something(self, args: Dict[str, Any]) -> None:
        player = args["player"]
        agent = args["agent"]
        operation_id = args["operationId"]
        other_free = args["otherFreePlayers"]

        now_ms = int(time.time() * 1000)
        just_left = (
            agent.get("lastConversation") is not None
            and now_ms < agent["lastConversation"] + CONVERSATION_COOLDOWN_MS
        )
        recently_attempted = (
            agent.get("lastInviteAttempt") is not None
            and now_ms < agent["lastInviteAttempt"] + CONVERSATION_COOLDOWN_MS
        )
        recent_activity = (
            player.get("activity") is not None
            and now_ms < player["activity"]["until"] + ACTIVITY_COOLDOWN_MS
        )

        invitee: Optional[str] = None
        activity: Optional[Dict[str, Any]] = None
        destination: Optional[Dict[str, int]] = None

        if not player.get("pathfinding"):
            if recent_activity or just_left:
                # 漫游到地图上一个随机点
                destination = self._wander_destination(args["map"])
            else:
                # 做个活动
                act = random.choice(ACTIVITIES)
                activity = {
                    "description": act["description"],
                    "emoji": act["emoji"],
                    "until": now_ms + act["duration_ms"],
                }
        else:
            if not (just_left or recently_attempted):
                invitee = find_conversation_candidate(
                    self._db, self._world_id, now_ms,
                    player["id"], player["position"], other_free,
                )

        # 模拟原项目的 sleep + jitter，避免 OCC 抖动
        time.sleep(random.random())
        self._on_finish("finishDoSomething", {
            "operationId": operation_id,
            "agentId": agent["id"],
            "destination": destination,
            "invitee": invitee,
            "activity": activity,
        })

    def _agent_generate_message(self, args: Dict[str, Any]) -> None:
        msg_type = args["type"]
        player_id = args["playerId"]
        agent_id = args.get("agentId", "")
        conversation_id = args["conversationId"]
        other_player_id = args["otherPlayerId"]
        message_uuid = args["messageUuid"]
        operation_id = args["operationId"]

        player_descs = self.player_descriptions_snapshot
        agent_descs = self.agent_descriptions_snapshot

        if msg_type == "start":
            last_conv = self._db.last_archived_conversation(
                self._world_id, player_id, other_player_id
            )
            text = start_conversation_message(
                self._llm, self._db, self._memory, self._world_id,
                conversation_id, player_id, other_player_id,
                player_descs, agent_descs, last_conv,
            )
        elif msg_type == "continue":
            text = continue_conversation_message(
                self._llm, self._db, self._memory, self._world_id,
                conversation_id, player_id, other_player_id,
                player_descs, agent_descs,
            )
        elif msg_type == "leave":
            text = leave_conversation_message(
                self._llm, self._db, self._world_id,
                conversation_id, player_id, other_player_id,
                player_descs, agent_descs,
            )
        else:
            raise ValueError(f"Unknown message type: {msg_type}")

        # 通过对话路由把消息从 author 转发到 recipient
        self._router.deliver(RoutedMessage(
            conversation_id=conversation_id,
            author=player_id,
            recipient=other_player_id,
            text=text,
            message_uuid=message_uuid,
            timestamp=int(time.time() * 1000),
            leave_conversation=(msg_type == "leave"),
        ))

        self._on_finish("agentFinishSendingMessage", {
            "agentId": agent_id,
            "conversationId": conversation_id,
            "timestamp": int(time.time() * 1000),
            "operationId": operation_id,
            "leaveConversation": msg_type == "leave",
        })

    def _agent_remember_conversation(self, args: Dict[str, Any]) -> None:
        agent_id = args["agentId"]
        player_id = args["playerId"]
        conversation_id = args["conversationId"]
        operation_id = args["operationId"]

        # 找对方
        msgs = self._db.list_messages(self._world_id, conversation_id)
        other_player_id = next(
            (m["author"] for m in msgs if m["author"] != player_id), None
        )
        player_desc = self.player_descriptions_snapshot.get(player_id)
        other_desc = (self.player_descriptions_snapshot.get(other_player_id)
                      if other_player_id else None)
        if player_desc is None or other_desc is None:
            # 缺描述就跳过记忆
            self._on_finish("finishRememberConversation", {
                "operationId": operation_id, "agentId": agent_id,
            })
            return

        remember_conversation(
            self._llm, self._db, self._memory, self._world_id,
            agent_id, player_id, conversation_id,
            player_desc.name, other_desc.name,
        )

        time.sleep(random.random())
        self._on_finish("finishRememberConversation", {
            "operationId": operation_id, "agentId": agent_id,
        })

    def _wander_destination(self, world_map_dict: Dict[str, Any]) -> Dict[str, int]:
        w = world_map_dict["width"]
        h = world_map_dict["height"]
        return {
            "x": 1 + random.randint(0, max(0, w - 2)),
            "y": 1 + random.randint(0, max(0, h - 2)),
        }

    # 操作名 -> handler 的映射
    _HANDLERS: Dict[str, Callable[["AgentBrain", Dict[str, Any]], None]] = {
        "agentDoSomething": _agent_do_something,
        "agentGenerateMessage": _agent_generate_message,
        "agentRememberConversation": _agent_remember_conversation,
    }


def run_agent_operation(brain: AgentBrain, name: str, args: Dict[str, Any]) -> Future:
    """Game 调这个入口提交操作。"""
    return brain.submit(name, args)
