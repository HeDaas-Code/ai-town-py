"""通用有限状态机（FSM）。

游戏内每个对象（玩家寻路、对话成员、agent 操作）都有一组状态。
原项目用 TS 联合类型 ``{ kind: 'needsPath' } | { kind: 'moving', path }`` 表达，
这里用 dataclass + ``StateMachine`` 容器统一管理，便于在每个 tick 里
按当前状态分发逻辑，并在状态迁移时记录轨迹。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class State:
    """一个具名状态。``data`` 携带该状态需要的附加字段。"""

    kind: str
    data: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default=None):
        return self.data.get(key, default)


@dataclass
class StateMachine:
    """极简 FSM：持有当前状态，支持迁移与按状态分发的 handler。

    用法::

        sm = StateMachine(State("idle"))
        sm.on("idle", lambda ctx, now: sm.transition(State("walking")))
        sm.tick(ctx, now)  # 调用当前状态对应 handler
    """

    state: State
    _handlers: Dict[str, Callable[[Any, float], None]] = field(default_factory=dict)

    def on(self, kind: str, handler: Callable[[Any, float], None]) -> None:
        self._handlers[kind] = handler

    def transition(self, new_state: State) -> None:
        self.state = new_state

    def is_state(self, kind: str) -> bool:
        return self.state.kind == kind

    def tick(self, ctx: Any, now: float) -> None:
        handler = self._handlers.get(self.state.kind)
        if handler is not None:
            handler(ctx, now)


@dataclass
class PlayerPathfindingState:
    """玩家寻路状态机解构后的可变字段，对应原项目的 ``pathfinding.state`` 联合类型。"""

    kind: str  # 'needsPath' | 'waiting' | 'moving'
    until: Optional[float] = None  # waiting 用
    path: Optional[list] = None  # moving 用（Path 类型）

    @classmethod
    def needs_path(cls) -> "PlayerPathfindingState":
        return cls(kind="needsPath")

    @classmethod
    def waiting(cls, until: float) -> "PlayerPathfindingState":
        return cls(kind="waiting", until=until)

    @classmethod
    def moving(cls, path: list) -> "PlayerPathfindingState":
        return cls(kind="moving", path=path)


@dataclass
class MembershipStatus:
    """对话成员状态：invited / walkingOver / participating。"""

    kind: str
    started: Optional[float] = None  # participating 用

    @classmethod
    def invited(cls) -> "MembershipStatus":
        return cls(kind="invited")

    @classmethod
    def walking_over(cls) -> "MembershipStatus":
        return cls(kind="walkingOver")

    @classmethod
    def participating(cls, started: float) -> "MembershipStatus":
        return cls(kind="participating", started=started)
