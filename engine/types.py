"""核心几何类型：Point / Vector / Path。

路径用打包后的五元组列表 ``[x, y, dx, dy, t]`` 表示，与原项目一致，
便于序列化与压缩。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

# 路径分量打包格式：[x, y, dx, dy, t]
PackedPathComponent = Tuple[float, float, float, float, float]
Path = List[PackedPathComponent]


@dataclass
class Point:
    x: float
    y: float


@dataclass
class Vector:
    dx: float
    dy: float


@dataclass
class PathComponent:
    position: Point
    facing: Vector
    t: float


def pack_component(p: PathComponent) -> PackedPathComponent:
    return (p.position.x, p.position.y, p.facing.dx, p.facing.dy, p.t)


def unpack_component(p: PackedPathComponent) -> PathComponent:
    return PathComponent(position=Point(p[0], p[1]), facing=Vector(p[2], p[3]), t=p[4])


def query_path(path: Path, at: int) -> PathComponent:
    return unpack_component(path[at])
