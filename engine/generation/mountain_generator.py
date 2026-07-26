"""山脉生成器。

根据 chunk biome 和低频噪声，在 chunk 内生成山脉/高地区域。
山脉会标记为不可通行（object_tiles），并铺设对应的岩石背景瓦片。
"""
from __future__ import annotations

from typing import List, Set, Tuple

from engine.chunk import Chunk
from .biome import get_biome
from .noise import fbm_noise
from .tiles import mountain_tile


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

    # 低频噪声决定山脉大斑块
    local_seed = seed + chunk.cx * 523 + chunk.cy * 701
    mountain_cells: Set[Tuple[int, int]] = set()
    # 让山脉有一定概率出现，但不要太密集
    threshold = 0.45 - base_density * 1.5

    for lx in range(size):
        for ly in range(size):
            n = fbm_noise(
                (chunk.cx * size + lx) * 0.06,
                (chunk.cy * size + ly) * 0.06,
                local_seed,
                octaves=3,
            )
            if n > threshold:
                mountain_cells.add((lx, ly))

    if not mountain_cells:
        return set()

    # 铺设山脉瓦片：内部用高山瓦片，边缘用过渡瓦片
    _paint_mountains(chunk, mountain_cells, local_seed)
    return mountain_cells


def _paint_mountains(
    chunk: Chunk,
    mountain_cells: Set[Tuple[int, int]],
    seed: int,
) -> None:
    """在山脉区域绘制瓦片并标记为阻挡。"""
    size = chunk.size
    layer = chunk.bg_tiles[0]
    obj_layer = chunk.obj_tiles[0]

    for lx, ly in mountain_cells:
        # 边缘判断
        is_edge = any(
            (lx + dx, ly + dy) not in mountain_cells
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
        )
        layer[lx][ly] = mountain_tile(lx, ly, seed, is_edge=is_edge)
        # 山脉主体阻挡通行
        if not is_edge:
            obj_layer[lx][ly] = 1  # 使用通用阻挡标记


def is_mountain_blocked(chunk: Chunk, lx: int, ly: int) -> bool:
    """检查某个局部坐标是否被山脉阻挡。"""
    if not (0 <= lx < chunk.size and 0 <= ly < chunk.size):
        return True
    obj = chunk.obj_tiles[0][lx][ly]
    return obj != -1
