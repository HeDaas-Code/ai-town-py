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
from .types import Path, PathComponent, Point, Vector, unpack_component
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
        # segment 已经被 compress_path 打包成 tuple，先解包再拼接
        segment_components = [unpack_component(p) for p in segment]
        full_dense.extend(segment_components)
        # 下一段起点为当前段终点
        last = segment_components[-1]
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
    """局部网格 A*。支持指定起点、朝向与时间，用于分段寻路拼接。

    修正：
    - 非整点起点不再强制先水平/垂直对齐到网格线，而是直接走到最近的
      整数网格点，避免在空旷区域出现不必要的折线。
    - A* 只在整数网格点上扩展，启发函数使用曼哈顿距离（与四方向移动匹配）。
    """

    def _nearest_passable_grid(pos: Point) -> Optional[Point]:
        """找到离 pos 最近且可通行的整数网格点。"""
        x, y = pos.x, pos.y
        if math.floor(x) == x and math.floor(y) == y:
            p = Point(int(x), int(y))
            return p if blocked(game, now, p, player.id) is None else None

        candidates = []
        for ix in (math.floor(x), math.ceil(x)):
            for iy in (math.floor(y), math.ceil(y)):
                candidates.append((ix, iy, math.hypot(ix - x, iy - y)))
        candidates.sort(key=lambda item: item[2])
        for ix, iy, _ in candidates:
            p = Point(int(ix), int(iy))
            if blocked(game, now, p, player.id) is None:
                return p
        return None

    def _grid_facing(from_pos: Point, to_pos: Point) -> Vector:
        dx = to_pos.x - from_pos.x
        dy = to_pos.y - from_pos.y
        if abs(dx) > abs(dy):
            return Vector(1 if dx > 0 else -1, 0)
        if abs(dy) > 0:
            return Vector(0, 1 if dy > 0 else -1)
        return Vector(1, 0)

    initial_facing = start_facing if start_facing is not None else player.facing
    initial_t = start_t if start_t is not None else now

    # 对齐到整数网格点作为 A* 的实际起点
    grid_start = _nearest_passable_grid(start_pos)
    if grid_start is None:
        return None

    # 起点就在终点 -> 直接返回
    if points_equal(grid_start, destination):
        dense: List[PathComponent] = [
            PathComponent(position=start_pos, facing=initial_facing, t=initial_t),
            PathComponent(position=destination, facing=initial_facing, t=initial_t),
        ]
        return {"path": compress_path(dense), "new_destination": None}

    # A* 状态：g_score / f_score / came_from
    start_key = (int(grid_start.x), int(grid_start.y))
    g_score: Dict[Tuple[int, int], float] = {start_key: 0.0}
    f_score: Dict[Tuple[int, int], float] = {
        start_key: manhattan_distance(grid_start, destination),
    }
    came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}

    # 把起点封装成 PathCandidate，保留从原始 start_pos 到 grid_start 的前一段
    initial_seg_len = distance(start_pos, grid_start)
    initial_t_end = initial_t + (initial_seg_len / MOVEMENT_SPEED) * 1000.0
    start_candidate = PathCandidate(
        position=grid_start,
        facing=_grid_facing(start_pos, grid_start),
        t=initial_t_end,
        length=initial_seg_len,
        cost=initial_seg_len + manhattan_distance(grid_start, destination),
        prev=PathCandidate(
            position=start_pos,
            facing=initial_facing,
            t=initial_t,
            length=0.0,
            cost=manhattan_distance(start_pos, destination),
            prev=None,
        ),
    )

    heap = MinHeap(lambda a, b: a.cost > b.cost)
    heap.push(start_candidate)
    best = start_candidate

    while heap:
        current: Optional[PathCandidate] = heap.pop()
        if current is None:
            break

        if points_equal(current.position, destination):
            break

        if manhattan_distance(current.position, destination) < manhattan_distance(
            best.position, destination
        ):
            best = current

        cx, cy = int(current.position.x), int(current.position.y)
        current_key = (cx, cy)
        current_g = g_score.get(current_key, float("inf"))

        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            nxt_pos = Point(nx, ny)
            if blocked(game, now, nxt_pos, player.id):
                continue

            seg_len = 1.0
            tentative_g = current_g + seg_len
            nxt_key = (nx, ny)
            if tentative_g >= g_score.get(nxt_key, float("inf")):
                continue

            came_from[nxt_key] = current_key
            g_score[nxt_key] = tentative_g
            f_score[nxt_key] = tentative_g + manhattan_distance(nxt_pos, destination)
            facing = Vector(dx, dy)
            candidate = PathCandidate(
                position=nxt_pos,
                facing=facing,
                t=current.t + (seg_len / MOVEMENT_SPEED) * 1000.0,
                length=current.length + seg_len,
                cost=f_score[nxt_key],
                prev=current,
            )
            heap.push(candidate)
    else:
        current = None

    new_destination: Optional[Point] = None
    if current is None:
        if best.length == 0 or points_equal(best.position, start_pos):
            return None
        current = best
        new_destination = current.position

    # 回溯路径
    dense = []
    node: Optional[PathCandidate] = current
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
