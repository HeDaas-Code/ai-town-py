"""功能区（Biome）生成器。

每个 chunk 根据世界坐标获得一个 biome 类型，用于决定道路密度、建筑风格、装饰等。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from config import MAP_SEED
from .noise import fbm_noise


@dataclass(frozen=True)
class Biome:
    name: str
    color: str  # 调试/开发用十六进制颜色，不影响 tile
    road_density: float  # 0.0 ~ 1.0
    building_density: float  # 0.0 ~ 1.0
    tree_density: float  # 0.0 ~ 1.0
    description: str


BIOMES: Dict[str, Biome] = {
    "residential": Biome(
        name="residential",
        color="#A8D5BA",
        road_density=0.4,
        building_density=0.35,
        tree_density=0.25,
        description="住宅区，有房屋、花园和小路",
    ),
    "commercial": Biome(
        name="commercial",
        color="#F7DC6F",
        road_density=0.55,
        building_density=0.45,
        tree_density=0.1,
        description="商业区，有商店、广场和密集街道",
    ),
    "industrial": Biome(
        name="industrial",
        color="#BDC3C7",
        road_density=0.35,
        building_density=0.4,
        tree_density=0.05,
        description="工业区，有仓库和工厂",
    ),
    "park": Biome(
        name="park",
        color="#82E0AA",
        road_density=0.2,
        building_density=0.05,
        tree_density=0.7,
        description="公园区，树木和草地为主",
    ),
}


def biome_at(cx: int, cy: int, seed: int = MAP_SEED) -> str:
    """根据 chunk 坐标确定 biome 类型。

    使用两个不同尺度的 FBM：一个大尺度决定宏观布局，一个小尺度增加局部变化。
    """
    # 世界坐标（把 chunk 坐标映射到噪声空间）
    nx = cx * 0.15
    ny = cy * 0.15

    # 大尺度：主导功能区分布
    large = fbm_noise(nx, ny, seed + 1, octaves=4)
    # 小尺度：混合边界
    small = fbm_noise(nx * 2.5, ny * 2.5, seed + 2, octaves=3)

    value = large * 0.7 + small * 0.3

    if value > 0.35:
        return "commercial"
    if value > 0.05:
        return "residential"
    if value > -0.25:
        return "park"
    return "industrial"


def get_biome(name: str) -> Biome:
    return BIOMES.get(name, BIOMES["residential"])


def all_biomes() -> List[str]:
    return list(BIOMES.keys())
