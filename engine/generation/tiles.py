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

# 道路瓦片：使用纯路面瓦片（100% 路面覆盖，颜色一致）。
# 1007 是绿色草地瓦片，不是道路！1024/1069/943/1070 是棕色纯路面。
# 原地图的道路在 bg1 层用 751-850 的 2-tile 宽组件，无法用于单 tile 生成。
# 这里用纯路面瓦片作为单 tile 道路，所有方向共用同一瓦片池。
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

# 河流瓦片（水体 + 水边）
RIVER_WATER_TILES = [405, 406, 407, 408, 409]
RIVER_BANK_TILES = [450, 451, 452, 453, 495, 496, 497, 498, 499]

# 河流自适应贴图：通过分析 tileset 中每个瓦片的 4 角水体覆盖分类。
# 水体在 tileset 中占据蓝色区域，岸是沙地/泥土。
# 分类依据：水在哪些角/边 -> 选择对应过渡瓦片。

# 全水体瓦片（四角都是水）
RIVER_FULL_WATER_TILES = [406, 407, 409, 451, 452, 499]

# 水边过渡瓦片：水在某个方向，岸在对面方向
# N_edge: 水在上半，岸在下半（从上方流入）
RIVER_N_EDGE_TILES = [496, 497]
# E_edge: 水在右半，岸在左半（从右方流入）
RIVER_E_EDGE_TILES = [405, 450]
# W_edge: 水在左半，岸在右半（从左方流入）
RIVER_W_EDGE_TILES = [408, 453]

# 角落瓦片：水只在一个角
RIVER_SW_CORNER_TILES = [495]  # 水在左上角
RIVER_SE_CORNER_TILES = [498]  # 水在右上角

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

# 建筑外墙瓦片（object 层）：360-365 是不透明木质墙瓦片
# 380-387 在 tileset 中是透明空瓦片，不能用作墙
WALL_TILES = [360, 361, 362, 363, 364, 365]

# 建筑屋顶/平台瓦片（背景层）：180-183 + 1072/1073/1117/1118
# 1072/1073/1117/1118 是原地图建筑结构中的完整屋顶瓦片
ROOF_TILES = [180, 181, 182, 183, 1072, 1073, 1117, 1118]

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


def river_tile_autotile(
    lx: int,
    ly: int,
    seed: int,
    neighbors_n: bool,
    neighbors_s: bool,
    neighbors_e: bool,
    neighbors_w: bool,
) -> int:
    """根据 4 邻域水体分布自适应选择河流瓦片。

    neighbors_* 为 True 表示该方向有相邻水格。
    选择规则：
    - 四邻都有 -> 全水体
    - 某方向缺 -> 该方向的水边过渡瓦片（水在该方向，岸在对侧）
    - 两相邻方向缺 -> 角落瓦片
    """
    n_open = sum(1 for v in (neighbors_n, neighbors_s, neighbors_e, neighbors_w) if not v)
    s = seed + lx * 11 + ly * 17

    if n_open == 0:
        return deterministic_choice(RIVER_FULL_WATER_TILES, s)

    if n_open == 1:
        # 某方向没有水 -> 岸在该方向，水在其余三面
        if not neighbors_n:
            return deterministic_choice(RIVER_N_EDGE_TILES, s)
        if not neighbors_s:
            # 缺少 S_edge 瓦片，用 FULL_WATER 兜底
            return deterministic_choice(RIVER_FULL_WATER_TILES, s)
        if not neighbors_e:
            return deterministic_choice(RIVER_E_EDGE_TILES, s)
        if not neighbors_w:
            return deterministic_choice(RIVER_W_EDGE_TILES, s)

    if n_open == 2:
        # 两个相邻方向没水 -> 角落
        if not neighbors_n and not neighbors_w:
            return deterministic_choice(RIVER_SW_CORNER_TILES, s)
        if not neighbors_n and not neighbors_e:
            return deterministic_choice(RIVER_SE_CORNER_TILES, s)
        # 缺少 NW/NE corner 瓦片，用 FULL_WATER 兜底
        return deterministic_choice(RIVER_FULL_WATER_TILES, s)

    # n_open >= 3：用 FULL_WATER 兜底
    return deterministic_choice(RIVER_FULL_WATER_TILES, s)


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


def wall_tile_for_position(
    bx: int, by: int, w: int, h: int, dx: int, dy: int, seed: int
) -> int:
    """根据墙在建筑中的位置自适应选择墙瓦片。

    (bx, by) 建筑左上角，w/h 尺寸，(dx, dy) 当前 tile 相对位置。
    角落用 360/363，水平墙用 361/362，垂直墙用 364/365。
    """
    is_corner = (
        (dx == 0 and dy == 0)  # 左上
        or (dx == w - 1 and dy == 0)  # 右上
        or (dx == 0 and dy == h - 1)  # 左下
        or (dx == w - 1 and dy == h - 1)  # 右下
    )
    if is_corner:
        return deterministic_choice([360, 363], seed + dx * 7 + dy * 11)
    # 水平墙（上下边）
    if dy == 0 or dy == h - 1:
        return deterministic_choice([361, 362], seed + dx * 7 + dy * 11)
    # 垂直墙（左右边）
    return deterministic_choice([364, 365], seed + dx * 7 + dy * 11)


def roof_tile(seed: int) -> int:
    return deterministic_choice(ROOF_TILES, seed)
