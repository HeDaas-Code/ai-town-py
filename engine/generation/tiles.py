"""地图瓦片 ID 选择器。

基于 gentle-obj.png tileset（45 列 × 32 行，32×32 像素/瓦片）和
原始 gentle.json 的使用频率整理：
- 草地：以 271 为主，少量自然变体
- 道路：900+ 范围的土路/石路瓦片
- 水/河流：400+ 范围的水体与水边瓦片
- 山脉/悬崖：700+ 范围的岩石与高地瓦片
- 树木/外墙：367、458 等 object 层瓦片
"""
from __future__ import annotations

from typing import List

from .noise import deterministic_choice, fbm_noise


# 基础草地瓦片：以原始地图最常用的 271 为主，避免混入非草地瓦片造成噪点
GRASS_TILES = [271]

# 道路瓦片：水平、垂直、交叉、转弯等方向性瓦片
# 原始地图中 900+ 区域为路径/道路
ROAD_HORIZONTAL_TILES = [912, 913, 957, 958]
ROAD_VERTICAL_TILES = [916, 917, 960, 961]
ROAD_CROSS_TILES = [962, 963]
ROAD_TURN_TILES = {
    ("N", "E"): [946, 950],
    ("N", "W"): [947, 951],
    ("S", "E"): [948, 952],
    ("S", "W"): [949, 953],
}

# 河流瓦片（水体 + 水边）
RIVER_WATER_TILES = [405, 406, 407, 408, 409]
RIVER_BANK_TILES = [450, 451, 452, 453, 495, 496, 497, 498, 499]

# 山脉/悬崖瓦片
MOUNTAIN_TILES = [736, 737, 738, 739, 781, 782, 783, 784]
MOUNTAIN_EDGE_TILES = [721, 722, 723, 724, 725, 726, 728, 729, 730, 731]

# 建筑外墙瓦片（与树木共用 object 层）
WALL_TILES = [367, 458]

# 建筑屋顶瓦片（背景层）
ROOF_TILES = [1270, 1271, 1268, 1269, 1223, 1224]

# 所有道路瓦片集合，用于快速判断
ALL_ROAD_TILES = (
    ROAD_HORIZONTAL_TILES
    + ROAD_VERTICAL_TILES
    + ROAD_CROSS_TILES
    + [t for group in ROAD_TURN_TILES.values() for t in group]
)


def grass_tile(lx: int, ly: int, seed: int) -> int:
    """根据位置确定性选择草地变体。

    使用极低频噪声，让草地区域呈现大块自然变化，而不是逐 tile 随机噪点。
    """
    n = int((fbm_noise(lx * 0.08, ly * 0.08, seed, octaves=2) + 1.0) * 0.5 * 1000)
    return GRASS_TILES[n % len(GRASS_TILES)]


def road_tile(lx: int, ly: int, seed: int) -> int:
    """默认道路瓦片（无方向信息时的兜底）。"""
    return deterministic_choice(ALL_ROAD_TILES, seed + lx * 7 + ly * 13)


def road_tile_for_direction(lx: int, ly: int, seed: int, direction: str) -> int:
    """根据道路方向选择合适的瓦片。

    direction: "H" 水平, "V" 垂直, "C" 交叉, "T" T型路口, "NE"/"NW"/"SE"/"SW" 转弯
    """
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
    """山脉瓦片：高地或边缘过渡。"""
    pool = MOUNTAIN_EDGE_TILES if is_edge else MOUNTAIN_TILES
    return deterministic_choice(pool, seed + lx * 19 + ly * 23)


def tree_tile(seed: int) -> int:
    return deterministic_choice([367, 458], seed)


def wall_tile(seed: int) -> int:
    return deterministic_choice(WALL_TILES, seed)


def roof_tile(seed: int) -> int:
    return deterministic_choice(ROOF_TILES, seed)
