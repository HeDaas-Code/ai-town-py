"""规范化道路网络生成器。

负责：
1. 根据相邻 chunk portal 约束和 biome 决定本 chunk 边界 portal
2. 在 chunk 内部用 A* 连接 portal 形成道路
3. 根据道路方向选择对应的水平/垂直/转弯/交叉瓦片
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from config import CHUNK_SIZE, MAP_SEED
from engine.chunk import Chunk, Portal
from .biome import get_biome
from .noise import deterministic_shuffle, fbm_noise
from .tiles import (
    ALL_ROAD_TILES,
    RIVER_BANK_TILES,
    RIVER_WATER_TILES,
    MOUNTAIN_EDGE_TILES,
    MOUNTAIN_TILES,
    road_tile_for_direction,
)

# 生成器需要避让的 tile 集合
_PROTECTED_BG_TILES = set(
    ALL_ROAD_TILES
    + RIVER_WATER_TILES
    + RIVER_BANK_TILES
    + MOUNTAIN_TILES
    + MOUNTAIN_EDGE_TILES
)

# 边界方向常量
EDGES = ["N", "S", "E", "W"]
EDGE_OFFSETS = {
    "N": (0, -1),
    "S": (0, 1),
    "E": (1, 0),
    "W": (-1, 0),
}


def generate_roads_for_chunk(
    chunk: Chunk,
    neighbor_chunks: Dict[Tuple[int, int], Chunk],
    seed: int = MAP_SEED,
    forced_edges: Optional[List[str]] = None,
) -> None:
    """为 chunk 生成道路网络和边界 portal。"""
    size = chunk.size
    biome = get_biome(chunk.biome)
    forced_edges = forced_edges or []

    # 1. 继承相邻 chunk 的强制 portal
    forced_portals = _inherit_portals(chunk, neighbor_chunks)

    # 2. 根据 biome 生成额外 portal（强制边始终开 portal）
    extra_portals = _generate_extra_portals(chunk, forced_portals, biome, seed, forced_edges)

    chunk.portals = forced_portals + extra_portals

    # 3. 连接 portals 并铺设方向性道路瓦片
    if not chunk.portals:
        return

    road_segments = _plan_road_segments(chunk)
    _paint_directional_roads(chunk, road_segments, seed)


def _plan_road_segments(chunk: Chunk) -> List[Set[Tuple[int, int]]]:
    """规划道路路径，返回每段路径的 tile 集合列表。"""
    size = chunk.size
    segments: List[Set[Tuple[int, int]]] = []

    if len(chunk.portals) == 1:
        p = chunk.portals[0]
        hub = _snap_to_bounds(int(size / 2), int(size / 2), size)
        segments.append(_astar_road(chunk, (int(p.local_x), int(p.local_y)), hub))
    else:
        hub = _choose_hub(chunk)
        for p in chunk.portals:
            start = (int(p.local_x), int(p.local_y))
            segments.append(_astar_road(chunk, start, hub))
        segments.append({hub})

    return segments


def _paint_directional_roads(
    chunk: Chunk,
    segments: List[Set[Tuple[int, int]]],
    seed: int,
) -> None:
    """根据每段道路的方向铺设对应的道路瓦片。

    不会覆盖河流、山脉、建筑等已被占用的 tile，避免"看得见走不通"或道路
    切穿自然地貌的异常渲染。
    """
    size = chunk.size
    layer = chunk.bg_tiles[0]
    obj_layer = chunk.obj_tiles[0]
    local_seed = seed + chunk.cx * 7 + chunk.cy * 13

    # 先合并所有道路 tile，统计每个 tile 的邻居方向
    road_cells: Set[Tuple[int, int]] = set()
    for seg in segments:
        road_cells.update(seg)

    for lx, ly in road_cells:
        if not (0 <= lx < size and 0 <= ly < size):
            continue
        # 避让河流、山脉、建筑（object 层被占用）
        if obj_layer[lx][ly] != -1:
            continue
        if layer[lx][ly] in _PROTECTED_BG_TILES:
            continue
        direction = _road_direction_at(road_cells, lx, ly)
        layer[lx][ly] = road_tile_for_direction(lx, ly, local_seed, direction)


def _road_direction_at(cells: Set[Tuple[int, int]], x: int, y: int) -> str:
    """根据道路 cell 的上下左右邻居判断瓦片方向。"""
    north = (x, y - 1) in cells
    south = (x, y + 1) in cells
    east = (x + 1, y) in cells
    west = (x - 1, y) in cells

    count = sum([north, south, east, west])

    if count >= 3:
        return "C"  # 交叉
    if count == 2:
        if north and south:
            return "V"
        if east and west:
            return "H"
        if north and east:
            return "NE"
        if north and west:
            return "NW"
        if south and east:
            return "SE"
        if south and west:
            return "SW"
    if count == 1:
        if north or south:
            return "V"
        if east or west:
            return "H"

    return "C"  # 孤立点用交叉兜底


def _inherit_portals(chunk: Chunk, neighbors: Dict[Tuple[int, int], Chunk]) -> List[Portal]:
    """从相邻 chunk 继承指向本 chunk 的 portal。"""
    inherited: List[Portal] = []
    for edge in EDGES:
        dx, dy = EDGE_OFFSETS[edge]
        neighbor = neighbors.get((chunk.cx + dx, chunk.cy + dy))
        if neighbor is None:
            continue
        opposite = _opposite_edge(edge)
        for p in neighbor.portals:
            if p.edge != opposite:
                continue
            lx, ly = _neighbor_portal_to_local(p, edge, chunk.size)
            inherited.append(Portal(
                edge=edge,
                local_x=lx,
                local_y=ly,
                connected_chunk=(neighbor.cx, neighbor.cy),
                connected_portal_idx=neighbor.portals.index(p),
            ))
    return inherited


def _generate_extra_portals(
    chunk: Chunk,
    forced_portals: List[Portal],
    biome,
    seed: int,
    forced_edges: Optional[List[str]] = None,
) -> List[Portal]:
    """根据 biome 和噪声生成额外的边界 portal。"""
    size = chunk.size
    extra: List[Portal] = []
    forced_edges = forced_edges or []

    occupied: Dict[str, Set[int]] = {e: set() for e in EDGES}
    for p in forced_portals:
        coord = p.local_x if p.edge in ("N", "S") else p.local_y
        occupied[p.edge].add(int(coord))

    for edge in EDGES:
        if occupied[edge]:
            continue
        is_forced = edge in forced_edges
        if not is_forced:
            n = (fbm_noise(chunk.cx * 0.5 + _edge_seed(edge), chunk.cy * 0.5, seed + 3) + 1.0) * 0.5
            if n >= biome.road_density:
                continue
        pos = _find_free_portal_pos(edge, size, occupied, seed + chunk.cx * 31 + chunk.cy * 57)
        if pos is not None:
            dx, dy = EDGE_OFFSETS[edge]
            connected = (chunk.cx + dx, chunk.cy + dy)
            extra.append(Portal(
                edge=edge,
                local_x=pos[0],
                local_y=pos[1],
                connected_chunk=connected,
            ))
            coord = pos[0] if edge in ("N", "S") else pos[1]
            occupied[edge].add(int(coord))

    # 保底：如果一个 portal 都没有，强制在一条空闲边上开一个，
    # 避免出现完全孤立、没有道路的 chunk。
    if not forced_portals and not extra:
        for edge in deterministic_shuffle(list(EDGES), seed + chunk.cx * 73 + chunk.cy * 37):
            if occupied[edge]:
                continue
            pos = _find_free_portal_pos(edge, size, occupied, seed + chunk.cx * 31 + chunk.cy * 57)
            if pos is not None:
                dx, dy = EDGE_OFFSETS[edge]
                connected = (chunk.cx + dx, chunk.cy + dy)
                extra.append(Portal(
                    edge=edge,
                    local_x=pos[0],
                    local_y=pos[1],
                    connected_chunk=connected,
                ))
                break

    return extra


def _find_free_portal_pos(
    edge: str,
    size: int,
    occupied: Dict[str, Set[int]],
    seed: int,
) -> Optional[Tuple[float, float]]:
    """在边界上找一个远离已有 portal 的位置。"""
    candidates = list(range(2, size - 2))
    from .noise import deterministic_shuffle
    candidates = deterministic_shuffle(candidates, seed)
    for c in candidates:
        if any(abs(c - o) < 3 for o in occupied[edge]):
            continue
        if edge == "N":
            return float(c), 0.0
        if edge == "S":
            return float(c), float(size - 1)
        if edge == "W":
            return 0.0, float(c)
        if edge == "E":
            return float(size - 1), float(c)
    return None


def _choose_hub(chunk: Chunk) -> Tuple[int, int]:
    """选择道路 hub 位置，偏向 chunk 中心并避让障碍。"""
    size = chunk.size
    layer = chunk.bg_tiles[0]
    obj_layer = chunk.obj_tiles[0]

    def usable(lx: int, ly: int) -> bool:
        return (
            0 <= lx < size
            and 0 <= ly < size
            and obj_layer[lx][ly] == -1
            and layer[lx][ly] not in _PROTECTED_BG_TILES
        )

    if not chunk.portals:
        return _find_nearby_passable(size // 2, size // 2, size, usable)

    avg_x = sum(p.local_x for p in chunk.portals) / len(chunk.portals)
    avg_y = sum(p.local_y for p in chunk.portals) / len(chunk.portals)
    hx = int((avg_x + size / 2) / 2)
    hy = int((avg_y + size / 2) / 2)
    hx, hy = _snap_to_bounds(hx, hy, size)
    return _find_nearby_passable(hx, hy, size, usable)


def _find_nearby_passable(
    x: int, y: int, size: int, usable
) -> Tuple[int, int]:
    """从 (x,y) 开始螺旋搜索附近的可用位置。"""
    if usable(x, y):
        return x, y
    for radius in range(1, size):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if abs(dx) != radius and abs(dy) != radius:
                    continue
                nx, ny = x + dx, y + dy
                if usable(nx, ny):
                    return nx, ny
    return _snap_to_bounds(x, y, size)


def _snap_to_bounds(x: int, y: int, size: int) -> Tuple[int, int]:
    """确保 hub 不在边界上，留出建筑空间。"""
    margin = 3
    x = max(margin, min(size - 1 - margin, x))
    y = max(margin, min(size - 1 - margin, y))
    return x, y


def _astar_road(chunk: Chunk, start: Tuple[int, int], goal: Tuple[int, int]) -> Set[Tuple[int, int]]:
    """在 chunk 内部用 A* 找道路路径，自动避让河流、山脉和建筑。"""
    size = chunk.size
    layer = chunk.bg_tiles[0]
    obj_layer = chunk.obj_tiles[0]

    def passable(lx: int, ly: int) -> bool:
        if not (0 <= lx < size and 0 <= ly < size):
            return False
        # 起点/终点允许在边界 portal 上（可能落在已有地貌旁边）
        if (lx, ly) == start or (lx, ly) == goal:
            return True
        # 避开已被占用的碰撞格
        if obj_layer[lx][ly] != -1:
            return False
        # 避让河流、山脉等自然地貌
        if layer[lx][ly] in _PROTECTED_BG_TILES:
            return False
        return True

    if start == goal:
        return {start}

    open_set = [(start, 0)]
    came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
    g_score: Dict[Tuple[int, int], float] = {start: 0.0}
    f_score: Dict[Tuple[int, int], float] = {start: _heuristic(start, goal)}

    while open_set:
        open_set.sort(key=lambda item: f_score.get(item[0], float("inf")))
        current, _ = open_set.pop(0)

        if current == goal:
            return _reconstruct_path(came_from, current)

        for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            nxt = (current[0] + dx, current[1] + dy)
            if not passable(nxt[0], nxt[1]):
                continue
            tentative_g = g_score[current] + 1.0
            if tentative_g < g_score.get(nxt, float("inf")):
                came_from[nxt] = current
                g_score[nxt] = tentative_g
                f_score[nxt] = tentative_g + _heuristic(nxt, goal)
                if not any(nxt == item[0] for item in open_set):
                    open_set.append((nxt, 0))

    return _straight_line(start, goal)


def _heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _reconstruct_path(
    came_from: Dict[Tuple[int, int], Tuple[int, int]],
    current: Tuple[int, int],
) -> Set[Tuple[int, int]]:
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    return set(path)


def _straight_line(start: Tuple[int, int], goal: Tuple[int, int]) -> Set[Tuple[int, int]]:
    """Bresenham 直线作为 fallback。"""
    points: Set[Tuple[int, int]] = set()
    x0, y0 = start
    x1, y1 = goal
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        points.add((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy
    return points


def _opposite_edge(edge: str) -> str:
    return {"N": "S", "S": "N", "E": "W", "W": "E"}[edge]


def _neighbor_portal_to_local(p: Portal, edge: str, size: int) -> Tuple[float, float]:
    """把相邻 chunk 的 portal 坐标转换为本 chunk 边界坐标。"""
    if edge == "N":
        return p.local_x, 0.0
    if edge == "S":
        return p.local_x, float(size - 1)
    if edge == "W":
        return 0.0, p.local_y
    if edge == "E":
        return float(size - 1), p.local_y
    return 0.0, 0.0


def _edge_seed(edge: str) -> int:
    return {"N": 1, "S": 2, "E": 3, "W": 4}[edge]
