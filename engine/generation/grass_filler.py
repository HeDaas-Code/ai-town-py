"""草地填充器。

把 chunk 的背景层统一填充为草地，使用低频噪声产生自然的区域变体，
避免逐 tile 随机造成的「椒盐噪声」效果。
"""
from __future__ import annotations

from engine.chunk import Chunk
from .tiles import grass_tile


def fill_grass(chunk: Chunk, seed: int) -> None:
    """用规范化草地变体填充整个 chunk 背景层。"""
    size = chunk.size
    # 确保背景层已初始化
    if not chunk.bg_tiles:
        chunk.bg_tiles = [[[-1] * size for _ in range(size)]]

    layer = chunk.bg_tiles[0]
    local_seed = seed + chunk.cx * 1009 + chunk.cy * 997
    for lx in range(size):
        for ly in range(size):
            if layer[lx][ly] == -1:
                layer[lx][ly] = grass_tile(lx, ly, local_seed)
