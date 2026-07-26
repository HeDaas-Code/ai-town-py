"""Game 引擎主类：tick 循环、input 调度、operation 执行。

对应原项目 convex/aiTown/game.ts + convex/engine/abstractGame.ts。

原项目把引擎跑在 Convex 的 cron 上：每秒一个 step，每个 step 内 600 个 16ms tick。
本项目把这套搬到一个本地线程里：``Game.run_forever`` 是主循环，
- 每 ``TICK_DURATION_MS`` 跑一次 tick（更新 player/conversation/agent 状态）
- 每 ``STEP_DURATION_MS`` 落盘一次（save_diff）
- 接收 ``enqueue_input`` 提交的 input（人类玩家 + brain 回写）并在 tick 开头处理
- 接收 ``schedule_operation`` 提交的 operation（brain 在后台跑）
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config import (
    AppConfig, MAX_INPUTS_PER_STEP, MAX_TICKS_PER_STEP, STEP_DURATION_MS,
    TICK_DURATION_MS,
)
from db import Database
from dialogue import AgentEndpoint, DialogueRouter
from engine.agent import Agent
from engine.conversation import Conversation
from engine.ids import GameId, alloc_game_id
from engine.player import Player
from engine.player_description import AgentDescription, PlayerDescription
from engine.world import World
from engine.world_map import WorldMap


@dataclass
class PendingInput:
    """等待被某个 step 开头处理的 input。"""

    name: str
    args: Dict[str, Any]


@dataclass
class PendingOperation:
    """等待 brain 执行的 operation。"""

    name: str
    args: Dict[str, Any]


# 所有 input handler 的签名：(game, now_ms, args) -> Any
InputHandler = Callable[["Game", int, Dict[str, Any]], Any]


class Game:
    """引擎核心。线程安全：内部一把锁，input / operation 都在锁内入队。"""

    tick_duration_ms = TICK_DURATION_MS
    step_duration_ms = STEP_DURATION_MS
    max_ticks_per_step = MAX_TICKS_PER_STEP
    max_inputs_per_step = MAX_INPUTS_PER_STEP

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        world_map: WorldMap,
        world: Optional[World] = None,
        dialogue_router: Optional[DialogueRouter] = None,
    ):
        self.config = config
        self.db = db
        self.world_map = world_map
        self.world = world or World()
        self.world_id = config.world_id

        # 描述表：player_id -> PlayerDescription / agent_id -> AgentDescription
        self.player_descriptions: Dict[GameId, PlayerDescription] = {}
        self.agent_descriptions: Dict[GameId, AgentDescription] = {}
        self.descriptions_modified = False

        # 历史位置（按 tick 累积，落盘时清空）
        self.historical_locations: Dict[GameId, Dict[int, Dict[str, float]]] = {}

        # 引擎状态
        self.next_engine_ts = 0
        self.generation = 0
        self.num_pathfinds = 0
        self.pending_operations: List[PendingOperation] = []
        self._pending_inputs: List[PendingInput] = []
        self._input_lock = threading.Lock()

        # 对话路由：所有 agent 注册到这里
        self.dialogue_router = dialogue_router or DialogueRouter(db, self.world_id)

        # brain：可选，由 setup_brain 注入
        self.brain: Optional[Any] = None

        # input handler 注册表
        self._input_handlers: Dict[str, InputHandler] = {}
        self._register_default_inputs()

        # 运行控制
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- ID 分配 ----
    def alloc_id(self, id_type: str) -> GameId:
        gid = alloc_game_id(id_type, self.world.next_id)
        self.world.next_id += 1
        return gid

    # ---- input / operation 调度 ----
    def enqueue_input(self, name: str, args: Dict[str, Any]) -> None:
        """人类玩家 / brain 回写 input 的入口。"""
        with self._input_lock:
            self._pending_inputs.append(PendingInput(name, args))

    def schedule_operation(self, name: str, args: Dict[str, Any]) -> None:
        """Agent.startOperation 调这个把 operation 排队等 brain 执行。"""
        self.pending_operations.append(PendingOperation(name, args))

    def handle_input(self, now: int, name: str, args: Dict[str, Any]) -> Any:
        handler = self._input_handlers.get(name)
        if handler is None:
            raise ValueError(f"Invalid input: {name}")
        return handler(self, now, args)

    def register_input(self, name: str, handler: InputHandler) -> None:
        self._input_handlers[name] = handler

    # ---- brain 注入 ----
    def attach_brain(self, brain: Any) -> None:
        self.brain = brain
        # brain 完成后会调这个 lambda 把 input 推回 engine
        brain._on_finish = self._make_brain_callback()

    def _make_brain_callback(self) -> Callable[[str, Dict[str, Any]], None]:
        def cb(name: str, args: Dict[str, Any]) -> None:
            self.enqueue_input(name, args)
        return cb

    def _sync_brain_snapshots(self) -> None:
        """brain 跑 LLM 时需要 descriptions 快照，按 player_id 索引方便查询。"""
        if self.brain is None:
            return
        # 按 player_id 索引
        self.brain.player_descriptions_snapshot = dict(self.player_descriptions)
        # agent_descriptions 也按 player_id 索引（因为对话函数只看 player_id）
        agent_by_player = {
            a.player_id: ad
            for aid, ad in self.agent_descriptions.items()
            for a in [self.world.agents.get(aid)]
            if a is not None
        }
        self.brain.agent_descriptions_snapshot = agent_by_player

    # ---- tick / step ----
    def begin_step(self, now: int) -> None:
        self.historical_locations.clear()
        self.num_pathfinds = 0
        # 处理 input：原项目按 step 内时序处理；这里简化为 step 开头一次性处理
        with self._input_lock:
            inputs = self._pending_inputs[: self.max_inputs_per_step]
            self._pending_inputs = self._pending_inputs[self.max_inputs_per_step:]
        for inp in inputs:
            try:
                self.handle_input(now, inp.name, inp.args)
            except Exception as e:  # noqa: BLE001
                print(f"[engine] input {inp.name} failed: {e}")

        # 派发 operations 给 brain
        if self.brain is not None and self.pending_operations:
            self._sync_brain_snapshots()
            ops = self.pending_operations
            self.pending_operations = []
            for op in ops:
                self.brain.submit(op.name, op.args)

    def tick(self, now: int) -> None:
        # 1. player.tick（清理 / 人类玩家超时）
        for player in list(self.world.players.values()):
            player.tick(self, now)
        # 2. player.tickPathfinding
        for player in list(self.world.players.values()):
            player.tick_pathfinding(self, now)
        # 3. player.tickPosition
        for player in list(self.world.players.values()):
            player.tick_position(self, now)
        # 4. conversation.tick
        for conv in list(self.world.conversations.values()):
            conv.tick(self, now)
        # 5. agent.tick
        for agent in list(self.world.agents.values()):
            agent.tick(self, now)
        # 6. 记录历史位置（用于渲染插值）
        for player in self.world.players.values():
            buf = self.historical_locations.setdefault(player.id, {})
            buf[now] = {"x": player.position.x, "y": player.position.y,
                        "dx": player.facing.dx, "dy": player.facing.dy,
                        "speed": player.speed}

    def take_diff(self) -> Dict[str, Any]:
        diff = {
            "world": self.world.to_dict(),
            "historicalLocations": [
                {"playerId": pid, "location": buf}
                for pid, buf in self.historical_locations.items()
            ],
        }
        if self.descriptions_modified:
            diff["playerDescriptions"] = [p.to_dict() for p in self.player_descriptions.values()]
            diff["agentDescriptions"] = [a.to_dict() for a in self.agent_descriptions.values()]
            diff["worldMap"] = self.world_map.to_dict()
            self.descriptions_modified = False
        return diff

    def save_step(self) -> None:
        """落盘到 SQLite。"""
        diff = self.take_diff()
        self.db.save_world(self.world_id, diff)
        if "playerDescriptions" in diff:
            self.db.save_player_descriptions(self.world_id, diff["playerDescriptions"])
        if "agentDescriptions" in diff:
            self.db.save_agent_descriptions(self.world_id, diff["agentDescriptions"])
        if "worldMap" in diff:
            self.db.save_map(self.world_id, diff["worldMap"])
        self.db.set_engine_state(self.world_id, self.next_engine_ts, self.generation)

    # ---- 主循环 ----
    def run_forever(self) -> None:
        """引擎主线程：按 TICK / STEP 节拍跑。"""
        self._stop.clear()
        last_step = time.time() * 1000
        last_tick = last_step
        while not self._stop.is_set():
            now = time.time() * 1000
            if now - last_tick >= self.tick_duration_ms:
                self.tick(int(now))
                last_tick = now
            if now - last_step >= self.step_duration_ms:
                self.begin_step(int(now))
                # begin_step 里已经派发 operations
                self.save_step()
                last_step = now
                self.next_engine_ts = int(now)
            # 不忙等：sleep 到下一个 tick
            time.sleep(max(0.0, (self.tick_duration_ms - (time.time() * 1000 - now)) / 1000.0))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="ai-town-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.brain is not None:
            self.brain.shutdown(wait=False)

    # ---- 默认 input handlers ----
    def _register_default_inputs(self) -> None:
        self.register_input("finishRememberConversation", _finish_remember_conversation)
        self.register_input("finishDoSomething", _finish_do_something)
        self.register_input("agentFinishSendingMessage", _agent_finish_sending_message)
        self.register_input("createAgent", _create_agent)
        self.register_input("leaveConversation", _leave_conversation)
        self.register_input("startConversation", _start_conversation)
        self.register_input("acceptInvite", _accept_invite)
        self.register_input("rejectInvite", _reject_invite)
        self.register_input("finishSendingMessage", _finish_sending_message)
        self.register_input("startTyping", _start_typing)
        self.register_input("sendHumanMessage", _send_human_message)


# ---- input handlers ----
def _finish_remember_conversation(game: Game, now: int, args: Dict[str, Any]) -> None:
    agent_id = args["agentId"]
    operation_id = args["operationId"]
    agent = game.world.agents.get(agent_id)
    if agent is None:
        return
    if (agent.in_progress_operation is None
            or agent.in_progress_operation.operation_id != operation_id):
        return
    agent.in_progress_operation = None
    agent.to_remember = None


def _finish_do_something(game: Game, now: int, args: Dict[str, Any]) -> None:
    agent_id = args["agentId"]
    operation_id = args["operationId"]
    agent = game.world.agents.get(agent_id)
    if agent is None:
        return
    if (agent.in_progress_operation is None
            or agent.in_progress_operation.operation_id != operation_id):
        return
    agent.in_progress_operation = None
    player = game.world.players.get(agent.player_id)
    if player is None:
        return

    if args.get("invitee"):
        invitee = game.world.players.get(args["invitee"])
        if invitee is not None:
            Conversation.start(game, now, player, invitee)
            agent.last_invite_attempt = now
    if args.get("destination"):
        from engine.types import Point
        dest = args["destination"]
        from engine.movement import move_player
        move_player(game, now, player, Point(dest["x"], dest["y"]))
    if args.get("activity"):
        from engine.player import Activity
        player.activity = Activity(
            description=args["activity"]["description"],
            emoji=args["activity"].get("emoji"),
            until=args["activity"]["until"],
        )


def _agent_finish_sending_message(game: Game, now: int, args: Dict[str, Any]) -> None:
    agent_id = args["agentId"]
    operation_id = args["operationId"]
    agent = game.world.agents.get(agent_id)
    if agent is None:
        return
    player = game.world.players.get(agent.player_id)
    if player is None:
        return
    conv = game.world.conversations.get(args["conversationId"])
    if conv is None:
        # 对话可能已经结束；只清 operation
        agent.in_progress_operation = None
        return
    if (agent.in_progress_operation is None
            or agent.in_progress_operation.operation_id != operation_id):
        return
    agent.in_progress_operation = None

    # 把这条消息写入对话状态（清 typing，更新 lastMessage，numMessages++）
    _finish_sending_message(game, now, {
        "playerId": agent.player_id,
        "conversationId": args["conversationId"],
        "timestamp": args["timestamp"],
    })

    if args.get("leaveConversation"):
        conv.leave(game, now, player)


def _finish_sending_message(game: Game, now: int, args: Dict[str, Any]) -> None:
    conv = game.world.conversations.get(args["conversationId"])
    if conv is None:
        return
    pid = args["playerId"]
    if conv.is_typing and conv.is_typing["player_id"] == pid:
        conv.is_typing = None
    conv.last_message = {"author": pid, "timestamp": args["timestamp"]}
    conv.num_messages += 1


def _start_typing(game: Game, now: int, args: Dict[str, Any]) -> None:
    conv = game.world.conversations.get(args["conversationId"])
    if conv is None:
        return
    player = game.world.players.get(args["playerId"])
    if player is None:
        return
    conv.set_is_typing(now, player, args["messageUuid"])


def _create_agent(game: Game, now: int, args: Dict[str, Any]) -> Dict[str, Any]:
    from data.characters import descriptions
    desc = descriptions[args["descriptionIndex"]]
    player_id = Player.join(game, now, desc.name, desc.character, desc.identity)
    agent_id = game.alloc_id("agents")
    game.world.agents[agent_id] = Agent(id=agent_id, player_id=player_id)
    game.agent_descriptions[agent_id] = AgentDescription(
        agent_id=agent_id, identity=desc.identity, plan=desc.plan
    )
    game.descriptions_modified = True
    return {"agentId": agent_id}


def _leave_conversation(game: Game, now: int, args: Dict[str, Any]) -> None:
    player = game.world.players.get(args["playerId"])
    if player is None:
        return
    conv = game.world.conversations.get(args["conversationId"])
    if conv is None:
        return
    conv.leave(game, now, player)


def _start_conversation(game: Game, now: int, args: Dict[str, Any]) -> Any:
    player = game.world.players.get(args["playerId"])
    invitee = game.world.players.get(args["invitee"])
    if player is None or invitee is None:
        return None
    return Conversation.start(game, now, player, invitee)


def _accept_invite(game: Game, now: int, args: Dict[str, Any]) -> None:
    player = game.world.players.get(args["playerId"])
    conv = game.world.conversations.get(args["conversationId"])
    if player is None or conv is None:
        return
    conv.accept_invite(game, player)


def _reject_invite(game: Game, now: int, args: Dict[str, Any]) -> None:
    player = game.world.players.get(args["playerId"])
    conv = game.world.conversations.get(args["conversationId"])
    if player is None or conv is None:
        return
    conv.reject_invite(game, now, player)


def _send_human_message(game: Game, now: int, args: Dict[str, Any]) -> None:
    """人类玩家发消息：直接落库 + 更新对话状态 + 通过路由转发。"""
    player_id = args["playerId"]
    conv_id = args["conversationId"]
    text = args["text"]
    conv = game.world.conversations.get(conv_id)
    if conv is None:
        return
    msg_uuid = args.get("messageUuid") or str(int(time.time() * 1000))
    # 找对方
    other_pid = next((pid for pid in conv.participants if pid != player_id), None)
    if other_pid is None:
        return
    if game.dialogue_router is not None:
        from dialogue import RoutedMessage
        game.dialogue_router.deliver(RoutedMessage(
            conversation_id=conv_id, author=player_id, recipient=other_pid,
            text=text, message_uuid=msg_uuid, timestamp=int(now),
        ))
    _finish_sending_message(game, int(now), {
        "playerId": player_id, "conversationId": conv_id, "timestamp": int(now),
    })
