"""A* 寻路与移动。

移植自原项目 convex/aiTown/movement.ts。地图坐标系下做网格寻路，
允许从非整点位置出发，先对齐到网格再走 A*，最后压缩路径。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from config import MOVEMENT_SPEED
from .geometry import compress_path, distance, manhattan_distance, points_equal
from .minheap import MinHeap
from .types import Path, PathComponent, Point, Vector
from .world_map import WorldMap


@dataclass
class PathCandidate:
    position: Point
    facing: Optional[Vector]
    t: float
    length: float
    cost: float
    prev: Optional["PathCandidate"] = None


def stop_player(player) -> None:
    """清除玩家的寻路状态并停下。``player`` 是 engine.player.Player 实例。"""
    player.pathfinding = None
    player.speed = 0.0


def move_player(game, now: float, player, destination: Point,
                allow_in_conversation: bool = False) -> None:
    """让 ``player`` 走向 ``destination``（整数网格点）。"""
    if math.floor(destination.x) != destination.x or math.floor(destination.y) != destination.y:
        raise ValueError(f"Non-integral destination: {destination}")

    if points_equal(player.position, destination):
        return

    in_conversation = any(
        m.status.kind == "participating"
        for c in game.world.conversations.values()
        for m in c.participants.values()
        if m.player_id == player.id
    )
    if in_conversation and not allow_in_conversation:
        raise RuntimeError("Can't move when in a conversation. Leave the conversation first!")

    from .state_machine import PlayerPathfindingState  # 延迟导入避免循环
    player.pathfinding = {
        "destination": destination,
        "started": now,
        "state": PlayerPathfindingState.needs_path(),
    }


def find_route(game, now: float, player, destination: Point):
    """A* 寻路。返回 ``{path, new_destination}`` 或 ``None``。"""
    world_map = game.world_map
    min_distances: List[List[Optional[PathCandidate]]] = []

    def explore(current: PathCandidate) -> List[PathCandidate]:
        x, y = current.position.x, current.position.y
        neighbors: List[Tuple[Point, Vector]] = []

        # 非整点：先尝试水平/垂直对齐到网格点。
        if x != math.floor(x):
            neighbors.append((Point(math.floor(x), y), Vector(-1, 0)))
            neighbors.append((Point(math.floor(x) + 1, y), Vector(1, 0)))
        if y != math.floor(y):
            neighbors.append((Point(x, math.floor(y)), Vector(0, -1)))
            neighbors.append((Point(x, math.floor(y) + 1), Vector(0, 1)))
        # 整点：扩展四邻域。
        if x == math.floor(x) and y == math.floor(y):
            neighbors.append((Point(x + 1, y), Vector(1, 0)))
            neighbors.append((Point(x - 1, y), Vector(-1, 0)))
            neighbors.append((Point(x, y + 1), Vector(0, 1)))
            neighbors.append((Point(x, y - 1), Vector(0, -1)))

        nxt: List[PathCandidate] = []
        for pos, facing in neighbors:
            seg_len = distance(current.position, pos)
            length = current.length + seg_len
            if blocked(game, now, pos, player.id):
                continue
            remaining = manhattan_distance(pos, destination)
            candidate = PathCandidate(
                position=pos,
                facing=facing,
                t=current.t + (seg_len / MOVEMENT_SPEED) * 1000.0,
                length=length,
                cost=length + remaining,
                prev=current,
            )
            # 维护每个网格点的最优 cost。
            ix, iy = int(pos.x), int(pos.y)
            while len(min_distances) <= iy:
                min_distances.append([])
            row = min_distances[iy]
            while len(row) <= ix:
                row.append(None)
            existing = row[ix]
            if existing is not None and existing.cost <= candidate.cost:
                continue
            row[ix] = candidate
            nxt.append(candidate)
        return nxt

    start_pos = Point(player.position.x, player.position.y)
    current: Optional[PathCandidate] = PathCandidate(
        position=start_pos,
        facing=player.facing,
        t=now,
        length=0.0,
        cost=manhattan_distance(start_pos, destination),
        prev=None,
    )
    best = current
    heap = MinHeap(lambda a, b: a.cost > b.cost)

    while current is not None:
        if points_equal(current.position, destination):
            break
        if manhattan_distance(current.position, destination) < manhattan_distance(
            best.position, destination
        ):
            best = current
        for cand in explore(current):
            heap.push(cand)
        current = heap.pop() if heap else None

    new_destination: Optional[Point] = None
    if current is None:
        if best.length == 0:
            return None
        current = best
        new_destination = current.position

    dense: List[PathComponent] = []
    node = current
    while node is not None:
        dense.append(PathComponent(position=node.position, facing=node.facing, t=node.t))
        node = node.prev
    dense.reverse()
    return {"path": compress_path(dense), "new_destination": new_destination}


def blocked(game, now: float, pos: Point, player_id: Optional[str] = None) -> Optional[str]:
    other_positions = [p.position for p in game.world.players.values() if p.id != player_id]
    return blocked_with_positions(pos, other_positions, game.world_map)


def blocked_with_positions(position: Point, other_positions, world_map: WorldMap) -> Optional[str]:
    if math.isnan(position.x) or math.isnan(position.y):
        raise ValueError(f"NaN position {position}")
    # 无限世界：不再检查固定世界边界，而是让 chunk_manager 判断。
    # 目标 chunk 未加载时 is_blocked 返回 True，避免 AI / 玩家走进未生成区域。
    if world_map.chunk_manager.is_blocked(position.x, position.y):
        return "world blocked"
    for other in other_positions:
        if distance(other, position) < 0.75:  # COLLISION_THRESHOLD
            return "player"
    return None
