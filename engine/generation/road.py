"""道路网络生成器兼容层。

原有实现已迁移到 ``engine.generation.road_generator``，这里保留旧导入路径。
"""
from __future__ import annotations

from .road_generator import generate_roads_for_chunk

__all__ = ["generate_roads_for_chunk"]
