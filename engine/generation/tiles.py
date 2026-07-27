"""地图瓦片 ID 选择器。

基于 gentle-obj.png tileset（45 列 × 32 行，32×32 像素/瓦片）。

瓦片选择原则：
- 原项目地图是 Tiled 编辑器手工编辑的静态地图，没有 autotiling。
- tileset 中 540-599 范围不是一组山体 autotile，而是各种地貌的混合，
  包含透明瓦片、草地、水体、粉红色等不相关瓦片。
- 因此不能用 bitmask autotiling，只能为每种地形选择经过像素颜色
  验证的、视觉一致的瓦片。
- 每种地形使用 3-9 个颜色相近的瓦片，通过 deterministic_choice
  按种子选择，避免视觉重复。
"""
from __future__ import annotations

from typing import List

from .noise import deterministic_choice, fbm_noise


# 基础草地瓦片：以原始地图最常用的 271 为主
GRASS_TILES = [271]

# 道路瓦片：经过像素验证的纯路面瓦片（100% 覆盖，棕色 avg~153,111,29）。
# 1007 是绿色草地瓦片，不是道路！
ROAD_FULL_TILES = [1024, 1069, 943, 1070]
ROAD_HORIZONTAL_TILES = ROAD_FULL_TILES
ROAD_VERTICAL_TILES = ROAD_FULL_TILES
ROAD_CROSS_TILES = ROAD_FULL_TILES
ROAD_TURN_TILES = {
    ("N", "E"): ROAD_FULL_TILES,
    ("N", "W"): ROAD_FULL_TILES,
    ("S", "E"): ROAD_FULL_TILES,
    ("S", "W"): ROAD_FULL_TILES,
}

# 河流水体瓦片：经过像素验证的纯水瓦片（蓝色 avg~24,62,76，完全不透明）。
# 405/408/450/453 是水岸过渡瓦片，颜色混杂，不用于水体主体。
RIVER_WATER_TILES = [406, 407, 409, 451, 452]
RIVER_BANK_TILES = [450, 451, 452, 495, 496, 497, 498, 499]

# 山脉瓦片：经过像素验证的纯岩石瓦片（棕色 avg~110-136,70-98,34-42，
# 完全不透明）。540-599 中只有这些是纯岩石，其余是透明/草地/水体/粉色等。
MOUNTAIN_TILES = [541, 542, 553, 555, 593, 594, 595, 596, 598]
MOUNTAIN_BASE_TILES = MOUNTAIN_TILES
MOUNTAIN_EDGE_TILES = MOUNTAIN_TILES
MOUNTAIN_INTERIOR_TILES = MOUNTAIN_TILES

# 建筑外墙瓦片（object 层）：360-365 是不透明木质墙瓦片。
# 380-387 在 tileset 中是透明空瓦片，不能用作墙。
WALL_TILES = [360, 361, 362, 363, 364, 365]

# 建筑屋顶瓦片（背景层）：180-183 + 1072/1073/1117/1118
# 都是绿色屋顶，颜色一致。
ROOF_TILES = [180, 181, 182, 183, 1072, 1073, 1117, 1118]

# 所有道路瓦片集合，用于快速判断
ALL_ROAD_TILES = list(set(ROAD_FULL_TILES))


def grass_tile(lx: int, ly: int, seed: int) -> int:
    """根据位置确定性选择草地变体。"""
    n = int((fbm_noise(lx * 0.08, ly * 0.08, seed, octaves=2) + 1.0) * 0.5 * 1000)
    return GRASS_TILES[n % len(GRASS_TILES)]


def road_tile(lx: int, ly: int, seed: int) -> int:
    """默认道路瓦片。"""
    return deterministic_choice(ALL_ROAD_TILES, seed + lx * 7 + ly * 13)


def road_tile_for_direction(lx: int, ly: int, seed: int, direction: str) -> int:
    """根据道路方向选择瓦片。所有方向共用同一瓦片池。"""
    s = seed + lx * 3 + ly * 5
    if direction == "H":
        return deterministic_choice(ROAD_HORIZONTAL_TILES, s)
    if direction == "V":
        return deterministic_choice(ROAD_VERTICAL_TILES, s)
    if direction == "C":
        return deterministic_choice(ROAD_CROSS_TILES, s)
    if direction in ROAD_TURN_TILES:
        return deterministic_choice(ROAD_TURN_TILES[direction], s)
    return road_tile(lx, ly, seed)


def river_tile(lx: int, ly: int, seed: int, is_bank: bool = False) -> int:
    """河流瓦片：水体或水边。"""
    pool = RIVER_BANK_TILES if is_bank else RIVER_WATER_TILES
    return deterministic_choice(pool, seed + lx * 11 + ly * 17)


def mountain_tile(lx: int, ly: int, seed: int, is_edge: bool = False) -> int:
    """山脉瓦片：所有山体格统一使用验证过的岩石瓦片池。"""
    return deterministic_choice(MOUNTAIN_TILES, seed + lx * 19 + ly * 23)


def tree_tile(seed: int) -> int:
    return deterministic_choice([367, 458], seed)


def wall_tile(seed: int) -> int:
    return deterministic_choice(WALL_TILES, seed)


def roof_tile(seed: int) -> int:
    return deterministic_choice(ROOF_TILES, seed)
