"""河流生成器。

根据 chunk 坐标和世界 seed，在每个 chunk 内生成连贯的河流或小溪。
河流从 chunk 的一边流向另一边，使用水体瓦片和水边过渡瓦片。
"""
from __future__ import annotations

from typing import List, Optional, Set, Tuple

from engine.chunk import Chunk
from .noise import fbm_noise
from .tiles import river_tile


# 河流方向：从进入边到离开边
RIVER_DIRECTIONS = [
    ("N", "S"),
    ("S", "N"),
    ("E", "W"),
    ("W", "E"),
    ("N", "E"),
    ("N", "W"),
    ("S", "E"),
    ("S", "W"),
]


def generate_river(chunk: Chunk, seed: int) -> Set[Tuple[int, int]]:
    """在 chunk 内生成一条河流，返回河流占据的 tile 坐标集合。"""
    size = chunk.size
    local_seed = seed + chunk.cx * 877 + chunk.cy * 653

    # 用噪声决定是否生成河流，公园/低洼区域概率更高
    n = fbm_noise(chunk.cx * 0.2, chunk.cy * 0.2, local_seed, octaves=2)
    river_threshold = 0.35  # 约 30% 的 chunk 有河流
    if n < river_threshold:
        return set()

    # 确定性选择进入/离开边
    dir_idx = int(abs(fbm_noise(chunk.cx * 0.5, chunk.cy * 0.5, local_seed + 1)) * 1000) % len(RIVER_DIRECTIONS)
    entry_edge, exit_edge = RIVER_DIRECTIONS[dir_idx]

    entry_pos = _edge_position(entry_edge, size, local_seed)
    exit_pos = _edge_position(exit_edge, size, local_seed + 2)

    river_cells = _river_path(entry_pos, exit_pos, size, local_seed)
    _paint_river(chunk, river_cells, local_seed)
    return river_cells


def _edge_position(edge: str, size: int, seed: int) -> Tuple[int, int]:
    """在指定边界上选择一个进入/离开点。"""
    offset = 3 + int(abs(fbm_noise(seed * 0.1, seed * 0.1, seed)) * 1000) % (size - 6)
    if edge == "N":
        return offset, 0
    if edge == "S":
        return offset, size - 1
    if edge == "W":
        return 0, offset
    if edge == "E":
        return size - 1, offset
    return size // 2, size // 2


def _river_path(
    start: Tuple[int, int],
    end: Tuple[int, int],
    size: int,
    seed: int,
) -> Set[Tuple[int, int]]:
    """用带噪声的直线/折线生成河流路径。"""
    cells: Set[Tuple[int, int]] = set()
    x, y = start
    cells.add((x, y))

    # 简单向目标靠近，同时加入横向摆动
    step = 0
    while (x, y) != end and step < size * 4:
        step += 1
        dx = 1 if end[0] > x else (-1 if end[0] < x else 0)
        dy = 1 if end[1] > y else (-1 if end[1] < y else 0)

        # 偶尔横向摆动
        wobble = fbm_noise(step * 0.3 + seed, seed * 0.1, seed)
        if abs(wobble) > 0.4:
            if dx != 0 and 0 <= y + dx < size:
                dy = dx
                dx = 0
            elif dy != 0 and 0 <= x + dy < size:
                dx = dy
                dy = 0

        x += dx
        y += dy
        if 0 <= x < size and 0 <= y < size:
            cells.add((x, y))

    return cells


def _paint_river(chunk: Chunk, river_cells: Set[Tuple[int, int]], seed: int) -> None:
    """绘制河流瓦片并标记为水体阻挡。"""
    size = chunk.size
    layer = chunk.bg_tiles[0]
    obj_layer = chunk.obj_tiles[0]

    for lx, ly in river_cells:
        layer[lx][ly] = river_tile(lx, ly, seed, is_bank=False)
        obj_layer[lx][ly] = 1  # 水体不可通行

    # 水边过渡：河流周围一圈使用 bank tile（不阻挡）
    for lx, ly in list(river_cells):
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = lx + dx, ly + dy
            if 0 <= nx < size and 0 <= ny < size and (nx, ny) not in river_cells:
                if layer[nx][ny] not in [obj for obj in []]:
                    layer[nx][ny] = river_tile(nx, ny, seed + 1, is_bank=True)
