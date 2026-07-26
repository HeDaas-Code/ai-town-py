"""玩家：位置、朝向、寻路状态机、活动。

移植自原项目 convex/aiTown/player.ts。玩家可以是人类或 agent 控制的角色，
两者共享同一套移动 / 寻路 / 对话机制。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from config import HUMAN_IDLE_TOO_LONG_MS, MAX_HUMAN_PLAYERS, MAX_PATHFINDS_PER_STEP
from config import PATHFINDING_BACKOFF_MS, PATHFINDING_TIMEOUT_MS
from .geometry import path_position, points_equal
from .ids import GameId
from .movement import blocked, find_route, move_player, stop_player
from .state_machine import PlayerPathfindingState
from .types import Point, Vector


@dataclass
class Activity:
    description: str
    emoji: Optional[str]
    until: float


@dataclass
class Player:
    id: GameId
    position: Point
    facing: Vector
    last_input: float
    speed: float = 0.0
    human: Optional[str] = None
    pathfinding: Optional[Dict[str, Any]] = None  # {destination, started, state}
    activity: Optional[Activity] = None

    # ---- 序列化 ----
    @classmethod
    def from_dict(cls, data: dict) -> "Player":
        pf = data.get("pathfinding")
        if pf is not None:
            pf = dict(pf)
            pf["state"] = _deserialize_pf_state(pf["state"])
            pf["destination"] = Point(**pf["destination"])
        activity = data.get("activity")
        if activity is not None:
            activity = Activity(**activity)
        return cls(
            id=data["id"],
            human=data.get("human"),
            pathfinding=pf,
            activity=activity,
            last_input=data["last_input"],
            position=Point(**data["position"]),
            facing=Vector(**data["facing"]),
            speed=data["speed"],
        )

    def to_dict(self) -> dict:
        pf = None
        if self.pathfinding is not None:
            pf = {
                "destination": {"x": self.pathfinding["destination"].x,
                                "y": self.pathfinding["destination"].y},
                "started": self.pathfinding["started"],
                "state": _serialize_pf_state(self.pathfinding["state"]),
            }
        activity = None
        if self.activity is not None:
            activity = {"description": self.activity.description,
                        "emoji": self.activity.emoji, "until": self.activity.until}
        return {
            "id": self.id,
            "human": self.human,
            "pathfinding": pf,
            "activity": activity,
            "last_input": self.last_input,
            "position": {"x": self.position.x, "y": self.position.y},
            "facing": {"dx": self.facing.dx, "dy": self.facing.dy},
            "speed": self.speed,
        }

    # ---- tick 三段式 ----
    def tick(self, game, now: float) -> None:
        if self.human and self.last_input < now - HUMAN_IDLE_TOO_LONG_MS:
            self.leave(game, now)

    def tick_pathfinding(self, game, now: float) -> None:
        pf = self.pathfinding
        if pf is None:
            return

        # 到达目的地 -> 停下。
        if pf["state"].kind == "moving" and points_equal(pf["destination"], self.position):
            stop_player(self)

        # 超时 -> 停下。
        if pf["started"] + PATHFINDING_TIMEOUT_MS < now:
            stop_player(self)

        # waiting -> needsPath。
        if pf["state"].kind == "waiting" and pf["state"].until < now:
            pf["state"] = PlayerPathfindingState.needs_path()

        # needsPath -> 执行寻路。
        if pf["state"].kind == "needsPath" and game.num_pathfinds < MAX_PATHFINDS_PER_STEP:
            game.num_pathfinds += 1
            route = find_route(game, now, self, pf["destination"])
            if route is None:
                stop_player(self)
            else:
                if route["new_destination"] is not None:
                    pf["destination"] = route["new_destination"]
                pf["state"] = PlayerPathfindingState.moving(route["path"])

    def tick_position(self, game, now: float) -> None:
        pf = self.pathfinding
        if pf is None or pf["state"].kind != "moving":
            self.speed = 0.0
            return

        candidate = path_position(pf["state"].path, now)
        if candidate is None:
            return
        pos, facing, velocity = candidate["position"], candidate["facing"], candidate["velocity"]
        reason = blocked(game, now, pos, self.id)
        if reason is not None:
            backoff = random.random() * PATHFINDING_BACKOFF_MS
            pf["state"] = PlayerPathfindingState.waiting(now + backoff)
            return
        self.position = pos
        self.facing = facing
        self.speed = velocity

    # ---- 生命周期 ----
    @classmethod
    def join(cls, game, now: float, name: str, character: str,
             description: str, token_identifier: Optional[str] = None) -> GameId:
        from data.characters import characters  # 延迟导入

        if token_identifier:
            num_humans = sum(1 for p in game.world.players.values() if p.human)
            for p in game.world.players.values():
                if p.human == token_identifier:
                    raise RuntimeError("You are already in this game!")
            if num_humans >= MAX_HUMAN_PLAYERS:
                raise RuntimeError(f"Only {MAX_HUMAN_PLAYERS} human players allowed at once.")

        position = None
        for _ in range(100):
            cx = math.floor(random.random() * game.world_map.width)
            cy = math.floor(random.random() * game.world_map.height)
            if blocked(game, now, Point(cx, cy)) is None:
                position = Point(cx, cy)
                break
        if position is None:
            raise RuntimeError("Failed to find a free position!")

        facings = [Vector(1, 0), Vector(-1, 0), Vector(0, 1), Vector(0, -1)]
        facing = random.choice(facings)

        if not any(c.name == character for c in characters):
            raise ValueError(f"Invalid character: {character}")

        player_id = game.alloc_id("players")
        game.world.players[player_id] = cls(
            id=player_id, human=token_identifier, last_input=now,
            position=position, facing=facing, speed=0.0,
        )
        from .player_description import PlayerDescription
        game.player_descriptions[player_id] = PlayerDescription(
            player_id=player_id, character=character, description=description, name=name,
        )
        game.descriptions_modified = True
        return player_id

    def leave(self, game, now: float) -> None:
        conv = next((c for c in game.world.conversations.values()
                     if any(m.player_id == self.id for m in c.participants.values())), None)
        if conv is not None:
            conv.stop(game, now)
        game.world.players.pop(self.id, None)


def _serialize_pf_state(state: PlayerPathfindingState) -> dict:
    d = {"kind": state.kind}
    if state.until is not None:
        d["until"] = state.until
    if state.path is not None:
        d["path"] = [list(p) for p in state.path]
    return d


def _deserialize_pf_state(d: dict) -> PlayerPathfindingState:
    return PlayerPathfindingState(
        kind=d["kind"],
        until=d.get("until"),
        path=[tuple(p) for p in d["path"]] if d.get("path") is not None else None,
    )
