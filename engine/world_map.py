"""世界地图：背景层、对象（碰撞）层、动画精灵。

现在改为基于 Chunk（区块）管理：
- 旧版 gentle.json 会被导入为 seed chunk (0,0)
- 新增 chunk_manager 字段供新代码使用
- bg_tiles / object_tiles / width / height 保留为兼容属性，
  由已加载的 chunks 动态拼接而成（Phase 1 只有 seed chunk）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from config import CHUNK_SIZE
from .chunk import AnimatedSprite
from .chunk_manager import ChunkManager

TileLayer = List[List[int]]  # layer[x][y] -> tileIndex 或 -1


@dataclass
class WorldMap:
    tile_set_url: str
    tile_set_dim_x: int
    tile_set_dim_y: int
    tile_dim: int
    chunk_manager: ChunkManager
    # 逻辑地图尺寸（原始地图或生成世界的实际边界），用于兼容旧代码的 width/height
    map_width: int = 0
    map_height: int = 0
    # animated_sprites 也走 chunk_manager，但保留顶层字段方便旧代码访问
    animated_sprites: List[AnimatedSprite] = field(default_factory=list)

    @property
    def width(self) -> int:
        """兼容旧代码：逻辑地图宽度（tile）。"""
        return self.map_width or self.chunk_manager.world_width_tiles

    @property
    def height(self) -> int:
        """兼容旧代码：逻辑地图高度（tile）。"""
        return self.map_height or self.chunk_manager.world_height_tiles

    @property
    def bg_tiles(self) -> List[TileLayer]:
        """兼容旧代码：把所有已加载 chunk 的背景层拼接成大图层。"""
        return self.chunk_manager.combined_bg_tiles()

    @property
    def object_tiles(self) -> List[TileLayer]:
        """兼容旧代码：把所有已加载 chunk 的碰撞层拼接成大图层。"""
        return self.chunk_manager.combined_obj_tiles()

    @classmethod
    def load(cls, path: Path, chunk_size: int = CHUNK_SIZE) -> "WorldMap":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data, chunk_size)

    @classmethod
    def from_dict(cls, data: dict, chunk_size: int = CHUNK_SIZE) -> "WorldMap":
        has_chunk_manager = "chunkManager" in data
        cm = (
            ChunkManager.from_dict(data.get("chunkManager", {}))
            if has_chunk_manager
            else ChunkManager.from_legacy_data(data, chunk_size)
        )
        return cls(
            tile_set_url=data["tilesetpath"],
            tile_set_dim_x=data["tilesetpxw"],
            tile_set_dim_y=data["tilesetpxh"],
            tile_dim=data["tiledim"],
            chunk_manager=cm,
            # chunk-based 无限世界不再使用固定地图边界，
            # width/height 通过 chunk_manager 动态计算。
            map_width=0,
            map_height=0,
            animated_sprites=[AnimatedSprite(**s) for s in data.get("animatedsprites", [])],
        )

    def to_dict(self) -> dict:
        """只保存地图元数据；chunk 数据通过 save_chunks 增量保存。"""
        return {
            "tilesetpath": self.tile_set_url,
            "tilesetpxw": self.tile_set_dim_x,
            "tilesetpxh": self.tile_set_dim_y,
            "tiledim": self.tile_dim,
            "mapwidth": self.map_width,
            "mapheight": self.map_height,
            "chunkManager": {
                "chunkSize": self.chunk_manager.chunk_size,
                "seed": self.chunk_manager.seed,
            },
            # animated_sprites 也已由各个 chunk 自行保存，这里只保存顶层字段兼容旧读取
            "animatedsprites": [
                {
                    "x": s.x, "y": s.y, "w": s.w, "h": s.h,
                    "layer": s.layer, "sheet": s.sheet, "animation": s.animation,
                }
                for s in self.animated_sprites
            ],
        }
