"""世界地图：背景层、对象（碰撞）层、动画精灵。

地图数据由 tools/convert_map.py 从原项目 data/gentle.js 转换而来，
索引方式与原项目一致：``layer[x][y]``，x 为列方向（width），y 为行方向（height）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

TileLayer = List[List[int]]  # layer[x][y] -> tileIndex 或 -1


@dataclass
class AnimatedSprite:
    x: float   # 像素坐标
    y: float
    w: float
    h: float
    layer: int
    sheet: str
    animation: str


@dataclass
class WorldMap:
    width: int
    height: int

    tile_set_url: str
    tile_set_dim_x: int
    tile_set_dim_y: int
    tile_dim: int

    bg_tiles: List[TileLayer]
    object_tiles: List[TileLayer]
    animated_sprites: List[AnimatedSprite] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "WorldMap":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "WorldMap":
        return cls(
            width=data["mapwidth"],
            height=data["mapheight"],
            tile_set_url=data["tilesetpath"],
            tile_set_dim_x=data["tilesetpxw"],
            tile_set_dim_y=data["tilesetpxh"],
            tile_dim=data["tiledim"],
            bg_tiles=data["bgtiles"],
            object_tiles=data["objmap"],
            animated_sprites=[AnimatedSprite(**s) for s in data["animatedsprites"]],
        )

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "tileSetUrl": self.tile_set_url,
            "tileSetDimX": self.tile_set_dim_x,
            "tileSetDimY": self.tile_set_dim_y,
            "tileDim": self.tile_dim,
            "bgTiles": self.bg_tiles,
            "objectTiles": self.object_tiles,
            "animatedSprites": [
                {
                    "x": s.x, "y": s.y, "w": s.w, "h": s.h,
                    "layer": s.layer, "sheet": s.sheet, "animation": s.animation,
                }
                for s in self.animated_sprites
            ],
        }
