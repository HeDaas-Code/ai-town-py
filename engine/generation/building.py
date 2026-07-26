"""建筑与装饰生成器。

根据 chunk 的 biome 和已有道路，在可用地块中放置建筑和树木。
"""
from __future__ import annotations

from typing import List, Optional, Set, Tuple

from config import MAP_SEED
from .biome import get_biome
from .noise import deterministic_choice, fbm_noise
from .tiles import grass_tile, roof_tile, tree_tile, wall_tile


# 建筑尺寸限制（tile）
MIN_BUILDING_SIZE = 2
MAX_BUILDING_SIZE = 4


def generate_buildings_and_decorations(chunk, seed: int = MAP_SEED) -> None:
    """为 chunk 生成建筑和装饰。

    1. 标记道路 tile 和建筑占位
    2. 在剩余空地中放置建筑
    3. 根据 biome 放置树木等装饰
    """
    size = chunk.size
    biome = get_biome(chunk.biome)

    # 可建造网格：True 表示可以建筑/放树
    buildable = _compute_buildable_grid(chunk)

    # 放置建筑
    buildings = _place_buildings(chunk, buildable, biome, seed)

    # 放置树木
    _place_trees(chunk, buildable, biome, seed + 999)

    # 确保建筑周围和道路是草地（覆盖可能不正确的默认草地）
    _fill_grass(chunk, seed)


def _compute_buildable_grid(chunk) -> List[List[bool]]:
    """计算哪些 tile 可用于建筑和装饰。

    不可用的位置：道路、边界、已有 object。
    """
    size = chunk.size
    grid = [[True] * size for _ in range(size)]

    # 边界不可用
    for i in range(size):
        grid[0][i] = False
        grid[size - 1][i] = False
        grid[i][0] = False
        grid[i][size - 1] = False

    # 道路不可用（简单判断：bg_tiles[0][x][y] 是道路 tile）
    road_ids = _road_tile_ids()
    for x in range(size):
        for y in range(size):
            if chunk.bg_tiles[0][x][y] in road_ids:
                grid[x][y] = False

    # 已有 object 不可用
    for layer in chunk.obj_tiles:
        for x in range(size):
            for y in range(size):
                if layer[x][y] != -1:
                    grid[x][y] = False

    return grid


def _place_buildings(
    chunk,
    buildable: List[List[bool]],
    biome,
    seed: int,
) -> List[Tuple[int, int, int, int]]:
    """在 buildable 网格中放置建筑，返回建筑列表（x, y, w, h）。"""
    size = chunk.size
    buildings: List[Tuple[int, int, int, int]] = []

    # 根据 biome.building_density 决定尝试次数
    attempts = int(size * size * biome.building_density * 0.025)
    attempts = max(1, min(attempts, 12))

    for i in range(attempts):
        # 确定性但随位置变化的尺寸
        n = fbm_noise(chunk.cx * 2.0 + i * 0.1, chunk.cy * 2.0 + i * 0.1, seed + i)
        w = MIN_BUILDING_SIZE + int(abs(n) * 1000) % (MAX_BUILDING_SIZE - MIN_BUILDING_SIZE + 1)
        h = MIN_BUILDING_SIZE + int(abs(n) * 10000) % (MAX_BUILDING_SIZE - MIN_BUILDING_SIZE + 1)

        # 找一个可放置位置
        pos = _find_building_spot(buildable, w, h, seed + i * 17)
        if pos is None:
            continue
        bx, by = pos

        # 绘制建筑
        _draw_building(chunk, bx, by, w, h, seed + i * 31)

        # 标记为不可用
        for dx in range(-1, w + 1):
            for dy in range(-1, h + 1):
                px, py = bx + dx, by + dy
                if 0 <= px < size and 0 <= py < size:
                    buildable[px][py] = False

        buildings.append((bx, by, w, h))

    return buildings


def _find_building_spot(
    buildable: List[List[bool]],
    w: int,
    h: int,
    seed: int,
) -> Optional[Tuple[int, int]]:
    """找一个能放下 w x h 建筑的位置。"""
    size = len(buildable)
    candidates = []
    for x in range(1, size - w - 1):
        for y in range(1, size - h - 1):
            if _can_place_building(buildable, x, y, w, h):
                candidates.append((x, y))

    if not candidates:
        return None

    # 确定性选择
    idx = int(abs(fbm_noise(seed * 0.1, seed * 0.1, seed))) % len(candidates)
    return candidates[idx]


def _can_place_building(
    buildable: List[List[bool]],
    x: int,
    y: int,
    w: int,
    h: int,
) -> bool:
    """检查 x,y 位置是否能放下 w x h 建筑（含 1 tile 间距）。"""
    size = len(buildable)
    for dx in range(-1, w + 1):
        for dy in range(-1, h + 1):
            px, py = x + dx, y + dy
            if px < 0 or py < 0 or px >= size or py >= size:
                return False
            if not buildable[px][py]:
                return False
    return True


def _draw_building(chunk, x: int, y: int, w: int, h: int, seed: int) -> None:
    """在 chunk 中绘制一个矩形建筑。"""
    roof = roof_tile(seed)
    wall = wall_tile(seed + 1)

    # 屋顶（背景层）
    for dx in range(w):
        for dy in range(h):
            chunk.bg_tiles[0][x + dx][y + dy] = roof

    # 外墙（碰撞层）
    for dx in range(w):
        chunk.obj_tiles[0][x + dx][y] = wall
        chunk.obj_tiles[0][x + dx][y + h - 1] = wall
    for dy in range(h):
        chunk.obj_tiles[0][x][y + dy] = wall
        chunk.obj_tiles[0][x + w - 1][y + dy] = wall

    # 门：随机一面墙开缺口
    door_edge = deterministic_choice(["N", "S", "E", "W"], seed + 2)
    if door_edge == "N":
        dx = w // 2
        chunk.obj_tiles[0][x + dx][y] = -1
    elif door_edge == "S":
        dx = w // 2
        chunk.obj_tiles[0][x + dx][y + h - 1] = -1
    elif door_edge == "E":
        dy = h // 2
        chunk.obj_tiles[0][x + w - 1][y + dy] = -1
    else:
        dy = h // 2
        chunk.obj_tiles[0][x][y + dy] = -1


def _place_trees(chunk, buildable: List[List[bool]], biome, seed: int) -> None:
    """根据 biome.tree_density 在空地放置树木。"""
    size = chunk.size
    tree_density = biome.tree_density
    if tree_density <= 0:
        return

    attempts = int(size * size * tree_density * 0.05)
    attempts = max(0, min(attempts, 24))

    for i in range(attempts):
        n = fbm_noise(chunk.cx + i * 0.05, chunk.cy + i * 0.05, seed + i)
        x = 1 + int(abs(n) * 10000) % (size - 2)
        y = 1 + int(abs(n) * 100000) % (size - 2)
        if buildable[x][y]:
            chunk.obj_tiles[0][x][y] = tree_tile(seed + i)
            buildable[x][y] = False


def _fill_grass(chunk, seed: int) -> None:
    """把非道路、非建筑的背景 tile 填充为草地变体。"""
    size = chunk.size
    road_ids = _road_tile_ids()
    for x in range(size):
        for y in range(size):
            bg = chunk.bg_tiles[0][x][y]
            if bg not in road_ids and bg == 0:
                chunk.bg_tiles[0][x][y] = grass_tile(x, y, seed)


def _road_tile_ids() -> Set[int]:
    """道路 tile ID 集合。"""
    from .tiles import ALL_ROAD_TILES
    return set(ALL_ROAD_TILES)
