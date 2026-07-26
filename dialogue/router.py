"""对话路由：所有 agent 注册到这里，消息由路由转发给目标 agent。

设计动机
--------
原 AI Town 的 agent 之间不直接互发消息：A 想说话给 B 时，A 先把消息丢进
``messages`` 表 + 触发一次 ``agentFinishSendingMessage`` input；B 在下一 tick
里通过 ``previousMessages`` 拉取历史，再生成回复。这个流程被 Convex 的
mutation / query 系统隐式承担了。

本项目把这一层显式抽出来，作为 ``DialogueRouter``：
- 启动时所有 agent 必须调 ``register`` 注册自己的 playerId / agentId / handler
- 当 A 通过 ``deliver`` 发消息时，路由：
  1. 调用 ``MessageBus.persist`` 落盘到 SQLite（messages 表）
  2. 调用 ``MessageBus.dispatch`` 把消息推给目标 agent 的 handler
  3. 同时通知所有观察者（人类玩家 UI、调试日志）
- 对话开始 / 结束的事件也会被路由广播（``on_conversation_started`` / ``on_conversation_ended``）

这样 ``agent_brain`` 只需要：
  router.deliver(conversation_id, from=alice, to=bob, text=...)
就能完成「A 的话转发给 B」，无需关心 B 在哪个线程、是否在线。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from db import Database


@dataclass
class RoutedMessage:
    """一条被路由转发的消息。"""

    conversation_id: str
    author: str         # 发送者 playerId
    recipient: str      # 接收者 playerId
    text: str
    message_uuid: str
    timestamp: int
    leave_conversation: bool = False


@dataclass
class AgentEndpoint:
    """一个 agent 的注册信息。"""

    agent_id: str
    player_id: str
    name: str
    # handler 收到发给自己的消息时被调用（在路由线程内同步执行）
    handler: Callable[[RoutedMessage], None]
    # 可选：对话开始/结束回调
    on_conversation_started: Optional[Callable[[str, str, str], None]] = None
    on_conversation_ended: Optional[Callable[[str], None]] = None


class MessageBus:
    """消息总线：把消息持久化并派发给目标 agent。"""

    def __init__(self, db: Database, world_id: str):
        self._db = db
        self._world_id = world_id

    def persist(self, msg: RoutedMessage) -> int:
        """落盘到 messages 表，返回 message id。"""
        return self._db.insert_message(
            self._world_id, msg.conversation_id, msg.author, msg.text, msg.message_uuid
        )

    def list_history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self._db.list_messages(self._world_id, conversation_id)


class DialogueRouter:
    """所有 agent 的注册中心 + 消息转发器。

    线程安全：被 engine 主线程与 agent operation 后台线程共同访问。
    """

    def __init__(self, db: Database, world_id: str):
        self._db = db
        self._world_id = world_id
        self._bus = MessageBus(db, world_id)
        # playerId -> endpoint
        self._endpoints: Dict[str, AgentEndpoint] = {}
        # conversation_id -> (playerId1, playerId2)
        self._conversations: Dict[str, tuple] = {}
        # 全局观察者：人类玩家 UI、调试日志等
        self._observers: List[Callable[[RoutedMessage], None]] = []
        self._lock = threading.RLock()

    # ---- 注册 / 注销 ----
    def register(self, endpoint: AgentEndpoint) -> None:
        with self._lock:
            self._endpoints[endpoint.player_id] = endpoint

    def unregister(self, player_id: str) -> None:
        with self._lock:
            self._endpoints.pop(player_id, None)

    def get_endpoint(self, player_id: str) -> Optional[AgentEndpoint]:
        with self._lock:
            return self._endpoints.get(player_id)

    def list_registered(self) -> List[str]:
        with self._lock:
            return list(self._endpoints.keys())

    # ---- 观察者 ----
    def add_observer(self, observer: Callable[[RoutedMessage], None]) -> None:
        """注册一个全局观察者，每条路由消息都会回调它。"""
        with self._lock:
            self._observers.append(observer)

    # ---- 对话生命周期事件 ----
    def on_conversation_started(
        self, conversation_id: str, player_a: str, player_b: str
    ) -> None:
        with self._lock:
            self._conversations[conversation_id] = (player_a, player_b)
            for pid in (player_a, player_b):
                ep = self._endpoints.get(pid)
                if ep and ep.on_conversation_started:
                    try:
                        ep.on_conversation_started(conversation_id, player_a, player_b)
                    except Exception:  # noqa: BLE001
                        # 观察者异常不能影响路由主流程
                        pass

    def on_conversation_ended(self, conversation_id: str) -> None:
        with self._lock:
            pair = self._conversations.pop(conversation_id, None)
            if pair:
                for pid in pair:
                    ep = self._endpoints.get(pid)
                    if ep and ep.on_conversation_ended:
                        try:
                            ep.on_conversation_ended(conversation_id)
                        except Exception:  # noqa: BLE001
                            pass

    # ---- 消息转发 ----
    def deliver(self, msg: RoutedMessage) -> int:
        """把 ``msg`` 从 author 转发到 recipient。

        流程：
        1. 持久化到 SQLite
        2. 同步调用 recipient 的 handler（如果有）
        3. 通知所有全局观察者
        返回 message id。
        """
        msg_id = self._bus.persist(msg)

        # 通知全局观察者（UI 用它来即时显示气泡）
        with self._lock:
            observers = list(self._observers)
            recipient_ep = self._endpoints.get(msg.recipient)

        for obs in observers:
            try:
                obs(msg)
            except Exception:  # noqa: BLE001
                pass

        # 把消息派发给接收 agent 的 handler
        if recipient_ep is not None:
            try:
                recipient_ep.handler(msg)
            except Exception:  # noqa: BLE001
                # handler 异常不阻塞路由
                pass

        return msg_id

    # ---- 查询 ----
    def conversation_history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self._bus.list_history(conversation_id)
