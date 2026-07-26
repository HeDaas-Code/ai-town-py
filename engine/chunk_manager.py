"""ChunkManager：管理生成式地图的所有区块。

Phase 1 职责：
- 存储已加载/已生成的 chunks
- 从单张旧地图（gentle.json）导入为 seed chunk (0,0)
- 提供世界坐标 -> tile 查询接口
- 提供兼容旧代码的完整 bg_tiles / obj_tiles 视图
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from config import CHUNK_SIZE, MAP_SEED
from .chunk import AnimatedSprite, Chunk, Portal, world_to_chunk

TileLayer = List[List[int]]


class ChunkManager:
    """区块管理器。"""

    def __init__(self, chunk_size: int = CHUNK_SIZE, seed: int = MAP_SEED):
        self.chunk_size = chunk_size
        self.seed = seed
        self.chunks: Dict[Tuple[int, int], Chunk] = {}

    # ---- 查询 ----
    def get_chunk(self, cx: int, cy: int) -> Optional[Chunk]:
        return self.chunks.get((cx, cy))

    def ensure_chunk(self, cx: int, cy: int) -> Chunk:
        """获取 chunk，不存在则生成。"""
        key = (cx, cy)
        if key not in self.chunks:
            self.chunks[key] = self._generate_chunk(cx, cy)
        return self.chunks[key]

    def is_loaded(self, cx: int, cy: int) -> bool:
        return (cx, cy) in self.chunks

    def loaded_chunks(self) -> List[Tuple[int, int]]:
        return list(self.chunks.keys())

    def get_bg_tile(self, wx: float, wy: float, layer: int = 0) -> int:
        cx, cy, lx, ly = world_to_chunk(wx, wy, self.chunk_size)
        chunk = self.get_chunk(cx, cy)
        if chunk is None:
            return -1
        return chunk.get_bg(lx, ly, layer)

    def get_obj_tile(self, wx: float, wy: float, layer: int = 0) -> int:
        cx, cy, lx, ly = world_to_chunk(wx, wy, self.chunk_size)
        chunk = self.get_chunk(cx, cy)
        if chunk is None:
            return -1
        return chunk.get_obj(lx, ly, layer)

    def set_obj_tile(self, wx: float, wy: float, tile: int, layer: int = 0) -> None:
        cx, cy, lx, ly = world_to_chunk(wx, wy, self.chunk_size)
        chunk = self.ensure_chunk(cx, cy)
        chunk.set_obj(lx, ly, tile, layer)

    def is_blocked(self, wx: float, wy: float) -> bool:
        """查询世界坐标是否被 object_tiles 阻挡。"""
        cx, cy, lx, ly = world_to_chunk(wx, wy, self.chunk_size)
        chunk = self.get_chunk(cx, cy)
        if chunk is None:
            return True
        for layer in chunk.obj_tiles:
            if layer[lx][ly] != -1:
                return True
        return False

    # ---- 世界范围（兼容旧代码） ----
    @property
    def world_width_tiles(self) -> int:
        """已加载 chunks 的总宽度（tile）。Phase 1 只有 seed chunk 时等于 chunk_size。"""
        if not self.chunks:
            return 0
        min_cx = min(cx for cx, _ in self.chunks)
        max_cx = max(cx for cx, _ in self.chunks)
        return (max_cx - min_cx + 1) * self.chunk_size

    @property
    def world_height_tiles(self) -> int:
        """已加载 chunks 的总高度（tile）。"""
        if not self.chunks:
            return 0
        min_cy = min(cy for _, cy in self.chunks)
        max_cy = max(cy for _, cy in self.chunks)
        return (max_cy - min_cy + 1) * self.chunk_size

    def _world_offset(self) -> Tuple[int, int]:
        """已加载 chunks 的左上角世界 tile 坐标。"""
        if not self.chunks:
            return 0, 0
        min_cx = min(cx for cx, _ in self.chunks)
        min_cy = min(cy for _, cy in self.chunks)
        return min_cx * self.chunk_size, min_cy * self.chunk_size

    def combined_bg_tiles(self) -> List[TileLayer]:
        """把已加载 chunks 拼接成旧的 bg_tiles 格式（layer[x][y]）。

        仅用于兼容旧代码；后续会逐步替换为按 chunk 访问。
        """
        return self._combine_layers("bg")

    def combined_obj_tiles(self) -> List[TileLayer]:
        """把已加载 chunks 拼接成旧的 object_tiles 格式。"""
        return self._combine_layers("obj")

    def _combine_layers(self, kind: str) -> List[TileLayer]:
        if not self.chunks:
            return []
        off_x, off_y = self._world_offset()
        width = self.world_width_tiles
        height = self.world_height_tiles

        # 计算最大层数
        max_layers = 0
        for chunk in self.chunks.values():
            layers = chunk.bg_tiles if kind == "bg" else chunk.obj_tiles
            max_layers = max(max_layers, len(layers))

        combined: List[TileLayer] = []
        for _ in range(max_layers):
            layer = [[-1] * height for _ in range(width)]
            combined.append(layer)

        for (cx, cy), chunk in self.chunks.items():
            base_x = cx * self.chunk_size - off_x
            base_y = cy * self.chunk_size - off_y
            src_layers = chunk.bg_tiles if kind == "bg" else chunk.obj_tiles
            for li, src in enumerate(src_layers):
                dst = combined[li]
                for lx in range(self.chunk_size):
                    for ly in range(self.chunk_size):
                        dst[base_x + lx][base_y + ly] = src[lx][ly]
        return combined

    def combined_animated_sprites(self) -> List[AnimatedSprite]:
        """汇总所有 chunk 的动画精灵，转换为世界像素坐标。"""
        sprites: List[AnimatedSprite] = []
        for (cx, cy), chunk in self.chunks.items():
            offset_px_x = cx * self.chunk_size * 32  # tile_dim 需要外部传入，这里先按 tile 算
            offset_px_y = cy * self.chunk_size * 32
            for s in chunk.animated_sprites:
                sprites.append(AnimatedSprite(
                    x=s.x + offset_px_x,
                    y=s.y + offset_px_y,
                    w=s.w, h=s.h, layer=s.layer,
                    sheet=s.sheet, animation=s.animation,
                ))
        return sprites

    # ---- 生成 ----
    def _generate_chunk(self, cx: int, cy: int) -> Chunk:
        """Phase 1 占位生成器：生成一个空白 grass chunk。

        Phase 2 会替换为真正的程序化生成（噪声 + 道路 + 建筑）。
        """
        chunk = Chunk(cx=cx, cy=cy, size=self.chunk_size, biome="grass", generated=True)
        # 背景：全草地（tile 1 假设为草地，具体 id 从 tileset 决定）
        chunk.bg_tiles = [[[-1] * self.chunk_size for _ in range(self.chunk_size)]]
        chunk.obj_tiles = [[[-1] * self.chunk_size for _ in range(self.chunk_size)]]
        # Phase 1 简单填充可行走草地背景
        for lx in range(self.chunk_size):
            for ly in range(self.chunk_size):
                chunk.bg_tiles[0][lx][ly] = 1  # 占位草地 tile id
        return chunk

    # ---- 序列化摘要（用于卸载） ----
    def get_modified_chunks(self) -> List[Chunk]:
        return [c for c in self.chunks.values() if c.modified]

    def to_dict(self) -> dict:
        return {
            "chunkSize": self.chunk_size,
            "seed": self.seed,
            "chunks": {f"{cx},{cy}": c.to_dict() for (cx, cy), c in self.chunks.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChunkManager":
        cm = cls(chunk_size=d.get("chunkSize", CHUNK_SIZE), seed=d.get("seed", MAP_SEED))
        for key, cd in d.get("chunks", {}).items():
            cx, cy = (int(x) for x in key.split(","))
            chunk = Chunk.from_dict(cd)
            cm.chunks[(cx, cy)] = chunk
        return cm

    # ---- 工厂方法：从旧地图数据导入 ----
    @classmethod
    def from_legacy_data(cls, data: dict, chunk_size: int = CHUNK_SIZE) -> "ChunkManager":
        """把单张旧地图 JSON（gentle.json 格式）拆分为一个或多个 seed chunks。

        如果旧地图尺寸大于 chunk_size，会按 chunk_size 网格切成多个 chunk；
        否则整个地图作为 chunk (0,0)。
        """
        import math

        map_w = data.get("mapwidth", chunk_size)
        map_h = data.get("mapheight", chunk_size)
        tile_dim = data.get("tiledim", 32)
        num_cx = math.ceil(map_w / chunk_size)
        num_cy = math.ceil(map_h / chunk_size)

        cm = cls(chunk_size=chunk_size)
        all_sprites = [AnimatedSprite(**s) for s in data.get("animatedsprites", [])]

        for cx in range(num_cx):
            for cy in range(num_cy):
                chunk = Chunk(cx=cx, cy=cy, size=chunk_size, biome="seed", generated=True, modified=True)
                chunk.bg_tiles = cls._slice_layers(
                    data.get("bgtiles", []), cx, cy, chunk_size, map_w, map_h
                )
                chunk.obj_tiles = cls._slice_layers(
                    data.get("objmap", []), cx, cy, chunk_size, map_w, map_h
                )
                # 按像素区域把动画精灵分配到对应 chunk
                base_px_x = cx * chunk_size * tile_dim
                base_px_y = cy * chunk_size * tile_dim
                next_px_x = (cx + 1) * chunk_size * tile_dim
                next_px_y = (cy + 1) * chunk_size * tile_dim
                chunk.animated_sprites = [
                    AnimatedSprite(
                        x=s.x - base_px_x,
                        y=s.y - base_px_y,
                        w=s.w, h=s.h, layer=s.layer,
                        sheet=s.sheet, animation=s.animation,
                    )
                    for s in all_sprites
                    if base_px_x <= s.x < next_px_x and base_px_y <= s.y < next_px_y
                ]
                cm.chunks[(cx, cy)] = chunk
        return cm

    @staticmethod
    def _slice_layers(layers: List, cx: int, cy: int, chunk_size: int, map_w: int, map_h: int) -> List:
        """从旧图层中截取 chunk (cx, cy) 对应的区域。"""
        sliced = []
        for layer in layers:
            new_layer = []
            for lx in range(chunk_size):
                wx = cx * chunk_size + lx
                col = []
                for ly in range(chunk_size):
                    wy = cy * chunk_size + ly
                    if wx < len(layer) and wy < len(layer[wx]):
                        col.append(layer[wx][wy])
                    else:
                        col.append(-1)
                new_layer.append(col)
            sliced.append(new_layer)
        return sliced
