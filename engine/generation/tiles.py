"""地图瓦片 ID 选择器。

基于 gentle-obj.png tileset（45 列 × 32 行，32×32 像素/瓦片）和
原始 gentle.json 的实际使用方式整理：
- 草地：以 271 为主
- 道路：1007 等完整土路瓦片（避免使用 912/916 等切片）
- 水/河流：405-409 水体、450-453/495-499 水边瓦片
- 山脉/悬崖：540-599 范围的岩石与悬崖瓦片
- 建筑：180-183 帐篷/木质平台、361-363 木质外墙
- 树木：object 层 367、458
"""
from __future__ import annotations

from typing import List

from .noise import deterministic_choice, fbm_noise


# 基础草地瓦片：以原始地图最常用的 271 为主，避免混入非草地瓦片造成噪点
GRASS_TILES = [271]

# 道路瓦片：使用完整的土路瓦片，避免 912/916/962 等仅用于多 tile 拼接的切片
# 1007 是原始地图中常见的完整 dirt path，可直接单独使用
ROAD_HORIZONTAL_TILES = [1007]
ROAD_VERTICAL_TILES = [1007]
ROAD_CROSS_TILES = [1007]
ROAD_TURN_TILES = {
    ("N", "E"): [1007],
    ("N", "W"): [1007],
    ("S", "E"): [1007],
    ("S", "W"): [1007],
}

# 河流瓦片（水体 + 水边）
RIVER_WATER_TILES = [405, 406, 407, 408, 409]
RIVER_BANK_TILES = [450, 451, 452, 453, 495, 496, 497, 498, 499]

# 山脉/悬崖瓦片：通过分析 tileset 中每个瓦片的 4 角岩石覆盖，
# 分类为 autotile 角色（内部、各方向边、各角）。各集合互斥。
# 山体生成时根据 4 邻域山体分布选择对应角色，实现自适应过渡。

# 山体内部填充瓦片（四角都有岩石）
MOUNTAIN_INTERIOR_TILES = [553, 594, 596]

# 各方向边瓦片（该方向两角有岩石，对侧没有）
MOUNTAIN_N_EDGE_TILES = [593]                 # 上边有岩石
MOUNTAIN_S_EDGE_TILES = [543]                 # 下边有岩石
MOUNTAIN_W_EDGE_TILES = [548]                 # 左边有岩石
MOUNTAIN_E_EDGE_TILES = [555, 595]            # 右边有岩石

# 角落瓦片（只有一个角的岩石占主导）
MOUNTAIN_NW_CORNER_TILES = [540, 541, 545, 552, 585, 586, 590, 597]  # 左上角
MOUNTAIN_NE_CORNER_TILES = [542, 546, 547, 551, 554, 587, 591, 592]  # 右上角
MOUNTAIN_SW_CORNER_TILES = [598]              # 左下角
MOUNTAIN_SE_CORNER_TILES = [588]              # 右下角

# 兼容旧代码：保留 MOUNTAIN_BASE/EDGE/TILES 作为合集
MOUNTAIN_BASE_TILES = MOUNTAIN_INTERIOR_TILES
MOUNTAIN_EDGE_TILES = (
    MOUNTAIN_N_EDGE_TILES + MOUNTAIN_S_EDGE_TILES
    + MOUNTAIN_W_EDGE_TILES + MOUNTAIN_E_EDGE_TILES
    + MOUNTAIN_NW_CORNER_TILES + MOUNTAIN_NE_CORNER_TILES
    + MOUNTAIN_SW_CORNER_TILES + MOUNTAIN_SE_CORNER_TILES
)
MOUNTAIN_TILES = list(set(MOUNTAIN_BASE_TILES + MOUNTAIN_EDGE_TILES))

# 建筑外墙瓦片（object 层）：使用 objmap 中实际构成建筑物的瓦片
WALL_TILES = [380, 381, 382, 383, 384, 385, 386, 387]

# 建筑屋顶/平台瓦片（背景层）：帐篷/木质平台
ROOF_TILES = [180, 181, 182, 183]

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
    pool = MOUNTAIN_EDGE_TILES if is_edge else MOUNTAIN_INTERIOR_TILES
    return deterministic_choice(pool, seed + lx * 19 + ly * 23)


def mountain_tile_autotile(
    lx: int,
    ly: int,
    seed: int,
    neighbors_n: bool,
    neighbors_s: bool,
    neighbors_e: bool,
    neighbors_w: bool,
) -> int:
    """根据 4 邻域山体分布自适应选择山脉瓦片。

    neighbors_* 为 True 表示该方向有相邻山体。
    选择规则：
    - 四邻都有 -> 内部填充
    - 某方向缺 -> 该方向的边瓦片
    - 两相邻方向缺 -> 角落瓦片
    """
    n_open = sum(1 for v in (neighbors_n, neighbors_s, neighbors_e, neighbors_w) if not v)

    if n_open == 0:
        # 完全被包围 -> 内部
        return deterministic_choice(MOUNTAIN_INTERIOR_TILES, seed + lx * 19 + ly * 23)

    if n_open == 1:
        # 只有一个方向空 -> 边瓦片
        if not neighbors_n:
            return deterministic_choice(MOUNTAIN_N_EDGE_TILES, seed + lx * 19 + ly * 23)
        if not neighbors_s:
            return deterministic_choice(MOUNTAIN_S_EDGE_TILES, seed + lx * 19 + ly * 23)
        if not neighbors_e:
            return deterministic_choice(MOUNTAIN_E_EDGE_TILES, seed + lx * 19 + ly * 23)
        if not neighbors_w:
            return deterministic_choice(MOUNTAIN_W_EDGE_TILES, seed + lx * 19 + ly * 23)

    if n_open == 2:
        # 两个相邻方向空 -> 角落瓦片
        if not neighbors_n and not neighbors_w:
            return deterministic_choice(MOUNTAIN_NW_CORNER_TILES, seed + lx * 19 + ly * 23)
        if not neighbors_n and not neighbors_e:
            return deterministic_choice(MOUNTAIN_NE_CORNER_TILES, seed + lx * 19 + ly * 23)
        if not neighbors_s and not neighbors_w:
            return deterministic_choice(MOUNTAIN_SW_CORNER_TILES, seed + lx * 19 + ly * 23)
        if not neighbors_s and not neighbors_e:
            return deterministic_choice(MOUNTAIN_SE_CORNER_TILES, seed + lx * 19 + ly * 23)

    # n_open >= 3：孤立或半岛状，用角落兜底
    if not neighbors_n and not neighbors_w:
        return deterministic_choice(MOUNTAIN_NW_CORNER_TILES, seed + lx * 19 + ly * 23)
    if not neighbors_n and not neighbors_e:
        return deterministic_choice(MOUNTAIN_NE_CORNER_TILES, seed + lx * 19 + ly * 23)
    if not neighbors_s and not neighbors_w:
        return deterministic_choice(MOUNTAIN_SW_CORNER_TILES, seed + lx * 19 + ly * 23)
    return deterministic_choice(MOUNTAIN_SE_CORNER_TILES, seed + lx * 19 + ly * 23)


def tree_tile(seed: int) -> int:
    return deterministic_choice([367, 458], seed)


def wall_tile(seed: int) -> int:
    return deterministic_choice(WALL_TILES, seed)


def roof_tile(seed: int) -> int:
    return deterministic_choice(ROOF_TILES, seed)
