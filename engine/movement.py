"""A* 寻路与移动。

移植自原项目 convex/aiTown/movement.ts。地图坐标系下做网格寻路，
允许从非整点位置出发，先对齐到网格再走 A*，最后压缩路径。

无限世界支持：
- 同 chunk 内走局部网格 A*
- 跨 chunk 时先在 ChunkGraph 上找 chunk 序列，再分段走局部 A*
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import MOVEMENT_SPEED
from .chunk import chunk_to_world, world_to_chunk
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
    """A* 寻路。返回 ``{path, new_destination}`` 或 ``None``。

    同 chunk 内直接走网格 A*；跨 chunk 时先通过 ChunkGraph 规划 chunk
    序列，再分段走局部 A*。
    """
    chunk_size = game.world_map.chunk_manager.chunk_size
    start_cx, start_cy, _, _ = world_to_chunk(player.position.x, player.position.y, chunk_size)
    dest_cx, dest_cy, _, _ = world_to_chunk(destination.x, destination.y, chunk_size)

    if (start_cx, start_cy) == (dest_cx, dest_cy):
        return _find_grid_route(game, now, player, player.position, destination)

    return _find_long_route(game, now, player, destination)


def _find_long_route(game, now: float, player, destination: Point):
    """跨 chunk 长距离寻路：ChunkGraph + 分段网格 A*。"""
    cm = game.world_map.chunk_manager
    chunk_size = cm.chunk_size

    # 确保起点和终点 chunk 已加载（终点必须存在才能规划）
    start_cx, start_cy, _, _ = world_to_chunk(player.position.x, player.position.y, chunk_size)
    dest_cx, dest_cy, _, _ = world_to_chunk(destination.x, destination.y, chunk_size)
    cm.ensure_chunk(start_cx, start_cy)
    cm.ensure_chunk(dest_cx, dest_cy)

    graph = cm.chunk_graph()
    chunk_path = graph.find_path((start_cx, start_cy), (dest_cx, dest_cy))
    if chunk_path is None:
        # 没有 chunk 级路径时回退到直接网格 A*（通常走不通，但保留兼容）
        return _find_grid_route(game, now, player, player.position, destination)

    # 把 chunk 序列转换为 portal 世界坐标路径点
    waypoints = _chunk_path_to_waypoints(chunk_path, cm, chunk_size)
    if waypoints is None:
        return _find_grid_route(game, now, player, player.position, destination)

    # 最后一段：最后一个 portal -> 最终目的地
    waypoints.append(destination)

    # 分段网格 A* 并拼接路径
    full_dense: List[PathComponent] = []
    current_pos = player.position
    current_facing = player.facing
    current_t = now
    new_destination: Optional[Point] = None

    for i, waypoint in enumerate(waypoints):
        route = _find_grid_route(
            game, now, player, current_pos, waypoint,
            start_facing=current_facing, start_t=current_t,
        )
        if route is None:
            return None
        segment = route["path"]
        if not segment:
            current_pos = waypoint
            continue
        full_dense.extend(segment)
        # 下一段起点为当前段终点
        last = segment[-1]
        current_pos = last.position
        current_facing = last.facing
        current_t = last.t
        if route["new_destination"] is not None:
            new_destination = route["new_destination"]
            # 如果不能到达当前 waypoint，但还能走一段，就到此为止
            if i < len(waypoints) - 1:
                break

    if not full_dense:
        return None

    compressed = compress_path(full_dense)
    return {"path": compressed, "new_destination": new_destination}


def _chunk_path_to_waypoints(
    chunk_path: List[Tuple[int, int]], cm, chunk_size: int
) -> Optional[List[Point]]:
    """把 chunk 路径转换为途经 portal 的世界坐标列表（不含终点）。"""
    waypoints: List[Point] = []
    for i in range(len(chunk_path) - 1):
        a = chunk_path[i]
        b = chunk_path[i + 1]
        chunk = cm.get_chunk(a[0], a[1])
        if chunk is None:
            return None
        portal = None
        for p in chunk.portals:
            if p.connected_chunk == b:
                portal = p
                break
        if portal is None:
            return None
        wx, wy = chunk_to_world(a[0], a[1], int(portal.local_x), int(portal.local_y), chunk_size)
        waypoints.append(Point(wx, wy))
    return waypoints


def _find_grid_route(
    game,
    now: float,
    player,
    start_pos: Point,
    destination: Point,
    start_facing: Optional[Vector] = None,
    start_t: Optional[float] = None,
):
    """局部网格 A*。支持指定起点、朝向与时间，用于分段寻路拼接。"""
    # 使用字典以支持负坐标（无限世界）
    min_distances: Dict[Tuple[int, int], PathCandidate] = {}

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
            # 维护每个网格点的最优 cost（用字典支持负坐标）。
            ix, iy = int(pos.x), int(pos.y)
            existing = min_distances.get((ix, iy))
            if existing is not None and existing.cost <= candidate.cost:
                continue
            min_distances[(ix, iy)] = candidate
            nxt.append(candidate)
        return nxt

    current: Optional[PathCandidate] = PathCandidate(
        position=start_pos,
        facing=start_facing if start_facing is not None else player.facing,
        t=start_t if start_t is not None else now,
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
