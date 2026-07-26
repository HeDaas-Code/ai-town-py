"""确定性 2D 噪声生成（Value Noise + FBM）。

不依赖外部库，使用整数哈希产生可重复的伪随机值。
"""
from __future__ import annotations

import math
from typing import List


def _hash(n: int) -> float:
    """把整数映射到 [-1, 1] 的伪随机浮点数。"""
    n = (n << 13) ^ n
    n = (n * (n * n * 15731 + 789221) + 1376312589) & 0x7FFFFFFF
    return 1.0 - (n / 1073741824.0)


def _hash2(x: int, y: int, seed: int) -> float:
    return _hash(x * 374761393 + y * 668265263 + seed * 1013904223)


def _smoothstep(t: float) -> float:
    """平滑插值：3t^2 - 2t^3"""
    return t * t * (3.0 - 2.0 * t)


def value_noise(x: float, y: float, seed: int) -> float:
    """单个 octave 的 2D Value Noise，返回值范围 [-1, 1]。"""
    ix = math.floor(x)
    iy = math.floor(y)
    fx = x - ix
    fy = y - iy

    v00 = _hash2(int(ix), int(iy), seed)
    v10 = _hash2(int(ix) + 1, int(iy), seed)
    v01 = _hash2(int(ix), int(iy) + 1, seed)
    v11 = _hash2(int(ix) + 1, int(iy) + 1, seed)

    sx = _smoothstep(fx)
    sy = _smoothstep(fy)

    top = v00 + (v10 - v00) * sx
    bottom = v01 + (v11 - v01) * sx
    return top + (bottom - top) * sy


def fbm_noise(
    x: float,
    y: float,
    seed: int,
    octaves: int = 4,
    persistence: float = 0.5,
    lacunarity: float = 2.0,
    initial_frequency: float = 1.0,
) -> float:
    """分形布朗运动噪声：叠加多个 octave 的 Value Noise。

    返回值范围大致 [-1, 1]。
    """
    total = 0.0
    amplitude = 1.0
    frequency = initial_frequency
    max_value = 0.0

    for _ in range(octaves):
        total += value_noise(x * frequency, y * frequency, seed) * amplitude
        max_value += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    return total / max_value if max_value > 0 else 0.0


def normalized_fbm(
    x: float,
    y: float,
    seed: int,
    octaves: int = 4,
) -> float:
    """返回 [0, 1] 范围的 FBM 噪声。"""
    return (fbm_noise(x, y, seed, octaves) + 1.0) * 0.5


def deterministic_shuffle(items: List, seed: int) -> List:
    """确定性地打乱列表。"""
    result = list(items)
    n = len(result)
    for i in range(n - 1, 0, -1):
        j = int(abs(_hash(seed * 1000003 + i))) % (i + 1)
        result[i], result[j] = result[j], result[i]
    return result


def deterministic_choice(items: List, seed: int) -> object:
    """确定性地从列表选一个元素。"""
    if not items:
        return None
    idx = int(abs(_hash(seed))) % len(items)
    return items[idx]
