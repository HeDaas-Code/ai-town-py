"""地图瓦片 ID 选择器。

这些 ID 来自 gentle-obj.png tileset（data/maps/gentle.json 中使用）。
后续可以通过调试层观察效果后调整。
"""
from __future__ import annotations

from typing import List

from .noise import deterministic_choice, fbm_noise


# 基础草地瓦片（多种变体增加自然感）
GRASS_TILES = [1, 271, 272, 273, 274, 275, 276]

# 道路瓦片（土路/石路变体）
ROAD_TILES = [5, 6, 7, 8, 52, 53, 54, 55]

# 树木/植被瓦片（放在 object_tiles 中作为装饰性碰撞）
TREE_TILES = [367, 458]

# 建筑外墙瓦片
WALL_TILES = [367, 458]

# 建筑屋顶瓦片（背景层）
ROOF_TILES = [3, 4, 9, 10]

# 水体（公园可能用）
WATER_TILES = [11, 12, 13, 14]


def grass_tile(lx: int, ly: int, seed: int) -> int:
    """根据位置确定性选择草地变体。"""
    n = int((fbm_noise(lx * 0.3, ly * 0.3, seed) + 1.0) * 0.5 * 1000)
    return GRASS_TILES[n % len(GRASS_TILES)]


def road_tile(lx: int, ly: int, seed: int) -> int:
    n = int((fbm_noise(lx * 0.5, ly * 0.5, seed + 7) + 1.0) * 0.5 * 1000)
    return ROAD_TILES[n % len(ROAD_TILES)]


def tree_tile(seed: int) -> int:
    return deterministic_choice(TREE_TILES, seed)


def wall_tile(seed: int) -> int:
    return deterministic_choice(WALL_TILES, seed)


def roof_tile(seed: int) -> int:
    return deterministic_choice(ROOF_TILES, seed)
