"""Chunk（区块）数据结构。

生成式地图的基本单元。每个 chunk 是一个固定大小的瓦片网格（如 32x32），
包含背景层、碰撞/建筑层、动画精灵、功能区（biome）和边界 portal。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Tuple

TileLayer = List[List[int]]  # layer[x][y] -> tileIndex 或 -1


@dataclass
class AnimatedSprite:
    """地图上的动画精灵（火焰、水流、风车等）。"""

    x: float   # 像素坐标
    y: float
    w: float
    h: float
    layer: int
    sheet: str
    animation: str


@dataclass
class Portal:
    """chunk 边界入口/出口，用于跨区块寻路。"""

    edge: str  # "N" | "S" | "E" | "W"
    local_x: float  # chunk 内坐标（tile 坐标）
    local_y: float
    # 连接信息在生成/加载时填充
    connected_chunk: Tuple[int, int] = (0, 0)
    connected_portal_idx: int = 0


@dataclass
class Chunk:
    """地图区块。"""

    cx: int
    cy: int
    size: int
    biome: str = "unknown"
    bg_tiles: List[TileLayer] = field(default_factory=list)
    obj_tiles: List[TileLayer] = field(default_factory=list)
    animated_sprites: List[AnimatedSprite] = field(default_factory=list)
    portals: List[Portal] = field(default_factory=list)
    generated: bool = False
    modified: bool = False

    def world_offset(self) -> Tuple[int, int]:
        """返回该 chunk 在世界坐标系中的左上角像素坐标。"""
        return self.cx * self.size, self.cy * self.size

    def in_bounds(self, local_x: int, local_y: int) -> bool:
        return 0 <= local_x < self.size and 0 <= local_y < self.size

    def get_bg(self, local_x: int, local_y: int, layer: int = 0) -> int:
        if not self.in_bounds(local_x, local_y):
            return -1
        if layer >= len(self.bg_tiles):
            return -1
        return self.bg_tiles[layer][local_x][local_y]

    def set_bg(self, local_x: int, local_y: int, tile: int, layer: int = 0) -> None:
        if not self.in_bounds(local_x, local_y):
            return
        while layer >= len(self.bg_tiles):
            self.bg_tiles.append([[-1] * self.size for _ in range(self.size)])
        self.bg_tiles[layer][local_x][local_y] = tile
        self.modified = True

    def get_obj(self, local_x: int, local_y: int, layer: int = 0) -> int:
        if not self.in_bounds(local_x, local_y):
            return -1
        if layer >= len(self.obj_tiles):
            return -1
        return self.obj_tiles[layer][local_x][local_y]

    def set_obj(self, local_x: int, local_y: int, tile: int, layer: int = 0) -> None:
        if not self.in_bounds(local_x, local_y):
            return
        while layer >= len(self.obj_tiles):
            self.obj_tiles.append([[-1] * self.size for _ in range(self.size)])
        self.obj_tiles[layer][local_x][local_y] = tile
        self.modified = True

    def to_dict(self) -> dict:
        return {
            "cx": self.cx,
            "cy": self.cy,
            "size": self.size,
            "biome": self.biome,
            "bgTiles": self.bg_tiles,
            "objTiles": self.obj_tiles,
            "animatedSprites": [
                {
                    "x": s.x, "y": s.y, "w": s.w, "h": s.h,
                    "layer": s.layer, "sheet": s.sheet, "animation": s.animation,
                }
                for s in self.animated_sprites
            ],
            "portals": [
                {
                    "edge": p.edge,
                    "localX": p.local_x,
                    "localY": p.local_y,
                    "connectedChunk": p.connected_chunk,
                    "connectedPortalIdx": p.connected_portal_idx,
                }
                for p in self.portals
            ],
            "generated": self.generated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(
            cx=d["cx"],
            cy=d["cy"],
            size=d["size"],
            biome=d.get("biome", "unknown"),
            bg_tiles=d.get("bgTiles", []),
            obj_tiles=d.get("objTiles", []),
            animated_sprites=[AnimatedSprite(**s) for s in d.get("animatedSprites", [])],
            portals=[
                Portal(
                    edge=p["edge"],
                    local_x=p["localX"],
                    local_y=p["localY"],
                    connected_chunk=tuple(p["connectedChunk"]),
                    connected_portal_idx=p["connectedPortalIdx"],
                )
                for p in d.get("portals", [])
            ],
            generated=d.get("generated", False),
        )


# ---- 坐标转换工具 ----

def world_to_chunk(wx: float, wy: float, chunk_size: int) -> Tuple[int, int, int, int]:
    """世界 tile 坐标 -> (chunk_x, chunk_y, local_x, local_y)。"""
    cx = math.floor(wx / chunk_size)
    cy = math.floor(wy / chunk_size)
    lx = int(wx - cx * chunk_size)
    ly = int(wy - cy * chunk_size)
    return cx, cy, lx, ly


def chunk_to_world(cx: int, cy: int, lx: int, ly: int, chunk_size: int) -> Tuple[float, float]:
    """chunk 坐标 -> 世界 tile 坐标。"""
    return cx * chunk_size + lx, cy * chunk_size + ly
