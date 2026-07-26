"""道路网络生成器。

每个新生成的 chunk 会根据相邻 chunk 的 portal 约束和自身 biome 决定边界 portal，
然后在 chunk 内部用 A* 连接这些 portal 形成道路网络。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from config import CHUNK_SIZE, MAP_SEED
from engine.chunk import Chunk, Portal
from engine.chunk_manager import world_to_chunk
from .biome import get_biome
from .noise import deterministic_choice, fbm_noise
from .tiles import road_tile

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
    """为 chunk 生成道路和边界 portal。

    流程：
    1. 继承相邻 chunk 已存在的 portal（强制连接）
    2. 根据 biome 和噪声额外生成一些 portal
    3. 用 A* 在 chunk 内部连接所有 portal
    """
    size = chunk.size
    biome = get_biome(chunk.biome)

    forced_edges = forced_edges or []

    # 1. 继承相邻 chunk 的强制 portal
    forced_portals = _inherit_portals(chunk, neighbor_chunks)

    # 2. 根据 biome 生成额外 portal（强制边始终开 portal）
    extra_portals = _generate_extra_portals(chunk, forced_portals, biome, seed, forced_edges)

    chunk.portals = forced_portals + extra_portals

    # 3. 连接 portals
    if not chunk.portals:
        return

    # 收集道路点集
    road_tiles: Set[Tuple[int, int]] = set()

    # 单个 portal：向中心延伸一条短道路
    if len(chunk.portals) == 1:
        p = chunk.portals[0]
        hub = _snap_to_bounds(int(size / 2), int(size / 2), size)
        road_tiles.update(_astar_road(chunk, (int(p.local_x), int(p.local_y)), hub))
    else:
        # 多个 portal：选一个 hub，所有 portal 连到 hub
        hub = _choose_hub(chunk)
        for p in chunk.portals:
            start = (int(p.local_x), int(p.local_y))
            road_tiles.update(_astar_road(chunk, start, hub))
            # 也把 hub 附近连起来
            road_tiles.add(hub)

    # 4. 把道路 tile 写入 bg_tiles
    for lx, ly in road_tiles:
        if 0 <= lx < size and 0 <= ly < size:
            chunk.bg_tiles[0][lx][ly] = road_tile(lx, ly, seed + chunk.cx * 7 + chunk.cy * 13)


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
            # 把邻居 portal 的局部坐标转换为本 chunk 边界坐标
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

    # 已占用的边界位置（按 edge -> set of local positions）
    occupied: Dict[str, Set[int]] = {e: set() for e in EDGES}
    for p in forced_portals:
        coord = p.local_x if p.edge in ("N", "S") else p.local_y
        occupied[p.edge].add(int(coord))

    # 每个边根据 biome.road_density 决定是否开 portal。
    # 若某条边已经从邻居继承了强制 portal，则不再开额外 portal，
    # 保证区块边界一对一连接，避免道路网络歧义。
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

    return extra


def _find_free_portal_pos(
    edge: str,
    size: int,
    occupied: Dict[str, Set[int]],
    seed: int,
) -> Optional[Tuple[float, float]]:
    """在边界上找一个远离已有 portal 的位置。"""
    candidates = list(range(2, size - 2))
    # 打乱候选位置
    from .noise import deterministic_shuffle
    candidates = deterministic_shuffle(candidates, seed)
    for c in candidates:
        # 检查与已占用位置距离
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
    """选择道路 hub 位置，偏向 chunk 中心。"""
    size = chunk.size
    if not chunk.portals:
        return int(size / 2), int(size / 2)
    # 简单：portal 质心，然后向中心拉拢
    avg_x = sum(p.local_x for p in chunk.portals) / len(chunk.portals)
    avg_y = sum(p.local_y for p in chunk.portals) / len(chunk.portals)
    hx = int((avg_x + size / 2) / 2)
    hy = int((avg_y + size / 2) / 2)
    return _snap_to_bounds(hx, hy, size)


def _snap_to_bounds(x: int, y: int, size: int) -> Tuple[int, int]:
    """确保 hub 不在边界上，留出建筑空间。"""
    margin = 3
    x = max(margin, min(size - 1 - margin, x))
    y = max(margin, min(size - 1 - margin, y))
    return x, y


def _astar_road(chunk: Chunk, start: Tuple[int, int], goal: Tuple[int, int]) -> Set[Tuple[int, int]]:
    """在 chunk 内部用 A* 找道路路径。"""
    size = chunk.size
    # 优先走已有道路或空地；建筑/障碍不可走
    def passable(lx: int, ly: int) -> bool:
        if not (0 <= lx < size and 0 <= ly < size):
            return False
        # 建筑层有物体则不可走（道路生成时建筑还没生成，所以这里主要是边界检查）
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

    # 找不到路径：返回直线路径（可能穿过障碍，但道路生成阶段没有障碍）
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
