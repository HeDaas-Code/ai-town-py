"""山脉生成器。

根据 chunk biome 和低频噪声，在 chunk 内生成山脉/高地区域。
山脉会标记为不可通行（object_tiles），并铺设对应的岩石背景瓦片。

修复点：
- 降低噪声频率、提高 octave，生成更大更平滑的山体斑块。
- 过滤掉零散小斑块，避免「乱码」般的碎点。
- 瓦片选择：tileset 540-599 不是 autotile 组，只使用经过像素
  颜色验证的纯岩石瓦片，避免透明/草地/水体等错误瓦片混入。
"""
from __future__ import annotations

from typing import List, Set, Tuple

from engine.chunk import Chunk
from .biome import get_biome
from .noise import fbm_noise
from .tiles import (
    ALL_ROAD_TILES,
    RIVER_BANK_TILES,
    RIVER_WATER_TILES,
    mountain_tile,
)

# 山脉不应覆盖的道路/水体瓦片（生成顺序：道路 -> 山脉 -> 河流）
_PROTECTED_BG_TILES = set(ALL_ROAD_TILES + RIVER_WATER_TILES + RIVER_BANK_TILES)

# 最小保留的山体连通块大小，过滤碎点
_MIN_MOUNTAIN_COMPONENT_SIZE = 8

# 噪声频率：需要在 chunk 内部产生足够变化，避免整块变山。
_MOUNTAIN_FREQ = 0.06


def generate_mountains(chunk: Chunk, seed: int) -> Set[Tuple[int, int]]:
    """为 chunk 生成山脉区域，返回山脉占据的 tile 坐标集合。"""
    size = chunk.size
    biome = get_biome(chunk.biome)

    # 山脉只在工业区和部分住宅区出现，公园/商业区较少
    if biome.name == "park":
        base_density = 0.02
    elif biome.name == "industrial":
        base_density = 0.08
    elif biome.name == "residential":
        base_density = 0.04
    else:  # commercial
        base_density = 0.01

    if base_density <= 0:
        return set()

    local_seed = seed + chunk.cx * 523 + chunk.cy * 701
    threshold = 0.50 - base_density * 3.0

    raw_cells: Set[Tuple[int, int]] = set()
    for lx in range(size):
        for ly in range(size):
            wx = chunk.cx * size + lx
            wy = chunk.cy * size + ly
            n = fbm_noise(
                wx * _MOUNTAIN_FREQ,
                wy * _MOUNTAIN_FREQ,
                local_seed,
                octaves=4,
            )
            if n > threshold:
                raw_cells.add((lx, ly))

    if not raw_cells:
        return set()

    # 过滤碎点，只保留有一定规模的连通山体
    mountain_cells = _filter_large_components(raw_cells, _MIN_MOUNTAIN_COMPONENT_SIZE)
    if not mountain_cells:
        return set()

    _paint_mountains(chunk, mountain_cells, local_seed)
    return mountain_cells


def _filter_large_components(
    cells: Set[Tuple[int, int]], min_size: int
) -> Set[Tuple[int, int]]:
    """只保留大小 >= min_size 的 4-连通块，去掉零散噪点。"""
    unseen = set(cells)
    kept: Set[Tuple[int, int]] = set()

    while unseen:
        start = unseen.pop()
        component: Set[Tuple[int, int]] = {start}
        stack: List[Tuple[int, int]] = [start]

        while stack:
            x, y = stack.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + dx, y + dy)
                if nxt in unseen:
                    unseen.remove(nxt)
                    component.add(nxt)
                    stack.append(nxt)

        if len(component) >= min_size:
            kept.update(component)

    return kept


def _paint_mountains(
    chunk: Chunk,
    mountain_cells: Set[Tuple[int, int]],
    seed: int,
) -> None:
    """在山脉区域绘制瓦片并标记为阻挡。

    所有山体格统一使用验证过的岩石瓦片池，按位置确定性选择变体，
    避免视觉重复。若某格已有道路、水体或建筑，则跳过。
    """
    layer = chunk.bg_tiles[0]
    obj_layer = chunk.obj_tiles[0]

    for lx, ly in mountain_cells:
        # 不覆盖道路、水体和已有建筑
        if obj_layer[lx][ly] != -1:
            continue
        if layer[lx][ly] in _PROTECTED_BG_TILES:
            continue

        layer[lx][ly] = mountain_tile(lx, ly, seed)
        # 整个山脉区域都不可通行
        obj_layer[lx][ly] = 1


def is_mountain_blocked(chunk: Chunk, lx: int, ly: int) -> bool:
    """检查某个局部坐标是否被山脉阻挡。"""
    if not (0 <= lx < chunk.size and 0 <= ly < chunk.size):
        return True
    obj = chunk.obj_tiles[0][lx][ly]
    return obj != -1
