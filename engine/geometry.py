"""几何工具：距离、路径插值、路径压缩、向量归一化。

移植自原项目 convex/util/geometry.ts，保持算法一致。
"""
from __future__ import annotations

import math
from typing import List, Optional

from .types import Path, PathComponent, Point, Vector, pack_component, query_path

EPSILON = 0.0001


def distance(p0: Point, p1: Point) -> float:
    return math.hypot(p0.x - p1.x, p0.y - p1.y)


def points_equal(p0: Point, p1: Point) -> bool:
    return p0.x == p1.x and p0.y == p1.y


def manhattan_distance(p0: Point, p1: Point) -> float:
    return abs(p0.x - p1.x) + abs(p0.y - p1.y)


def path_position(path: Path, time: float):
    """返回 ``time`` 时刻在路径上的位置、朝向与速度。"""
    if len(path) < 2:
        raise ValueError(f"Invalid path: {path}")

    first = query_path(path, 0)
    if time < first.t:
        return {"position": first.position, "facing": first.facing, "velocity": 0.0}
    last = query_path(path, len(path) - 1)
    if last.t < time:
        return {"position": last.position, "facing": last.facing, "velocity": 0.0}

    for i in range(len(path) - 1):
        seg_start = query_path(path, i)
        seg_end = query_path(path, i + 1)
        if seg_start.t <= time <= seg_end.t:
            denom = seg_end.t - seg_start.t
            interp = 0.0 if denom == 0 else (time - seg_start.t) / denom
            return {
                "position": Point(
                    seg_start.position.x + interp * (seg_end.position.x - seg_start.position.x),
                    seg_start.position.y + interp * (seg_end.position.y - seg_start.position.y),
                ),
                "facing": seg_start.facing,
                "velocity": distance(seg_start.position, seg_end.position) / denom if denom else 0.0,
            }
    raise ValueError("Timestamp checks not exhaustive?")


def vector(p0: Point, p1: Point) -> Vector:
    return Vector(p1.x - p0.x, p1.y - p0.y)


def vector_length(v: Vector) -> float:
    return math.hypot(v.dx, v.dy)


def normalize(v: Vector) -> Optional[Vector]:
    length = vector_length(v)
    if length < EPSILON:
        return None
    return Vector(v.dx / length, v.dy / length)


def orientation_degrees(v: Vector) -> float:
    if math.hypot(v.dx, v.dy) < EPSILON:
        raise ValueError(f"Can't compute orientation of too small vector {v}")
    two_pi = 2 * math.pi
    radians = (math.atan2(v.dy, v.dx) + two_pi) % two_pi
    return (radians / two_pi) * 360


def compress_path(dense: List[PathComponent]) -> Path:
    """压缩路径：去掉可被相邻两点线性插值还原的中间点。"""
    packed = [pack_component(p) for p in dense]
    if len(dense) <= 2:
        return packed

    out = [pack_component(dense[0])]
    last = dense[0]
    candidate: Optional[PathComponent] = None
    for point in dense[1:]:
        if candidate is None:
            candidate = point
            continue
        # 若 candidate 能在 last→point 之间被线性插值还原，则可跳过。
        probe = path_position([pack_component(last), pack_component(point)], candidate.t)
        position_close = distance(probe["position"], candidate.position) < EPSILON
        facing_diff = Vector(
            probe["facing"].dx - candidate.facing.dx, probe["facing"].dy - candidate.facing.dy
        )
        facing_close = vector_length(facing_diff) < EPSILON
        if position_close and facing_close:
            candidate = point
            continue
        out.append(pack_component(candidate))
        last = candidate
        candidate = point
    if candidate is not None:
        out.append(pack_component(candidate))
    return out
