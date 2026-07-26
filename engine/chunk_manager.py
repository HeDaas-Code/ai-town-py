"""ChunkManager：管理生成式地图的所有区块。

职责：
- 存储已加载/已生成的 chunks
- 从单张旧地图（gentle.json）导入为 seed chunk (0,0)
- 提供世界坐标 -> tile 查询接口
- 提供兼容旧代码的完整 bg_tiles / obj_tiles 视图
- 基于玩家位置按需加载/卸载 chunk，并把修改过的 chunk 增量持久化到数据库
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from config import CHUNK_SIZE, MAP_SEED
from .chunk import AnimatedSprite, Chunk, Portal, world_to_chunk

if TYPE_CHECKING:
    from db import Database

TileLayer = List[List[int]]

# 默认加载/卸载半径（以 chunk 为单位）
DEFAULT_LOAD_RADIUS = 2
DEFAULT_UNLOAD_RADIUS = 3


class ChunkManager:
    """区块管理器。"""

    def __init__(
        self,
        chunk_size: int = CHUNK_SIZE,
        seed: int = MAP_SEED,
        db: Optional["Database"] = None,
        world_id: Optional[str] = None,
        load_radius: int = DEFAULT_LOAD_RADIUS,
        unload_radius: int = DEFAULT_UNLOAD_RADIUS,
    ):
        self.chunk_size = chunk_size
        self.seed = seed
        self.chunks: Dict[Tuple[int, int], Chunk] = {}
        self.db = db
        self.world_id = world_id
        self.load_radius = load_radius
        self.unload_radius = max(unload_radius, load_radius + 1)

    # ---- 查询 ----
    def get_chunk(self, cx: int, cy: int) -> Optional[Chunk]:
        return self.chunks.get((cx, cy))

    def ensure_chunk(self, cx: int, cy: int) -> Chunk:
        """获取 chunk，不存在则先从数据库加载，否则程序化生成。"""
        key = (cx, cy)
        if key not in self.chunks:
            loaded = self._load_chunk_from_db(cx, cy)
            if loaded is not None:
                self.chunks[key] = loaded
            else:
                self.chunks[key] = self._generate_chunk(cx, cy)
        return self.chunks[key]

    def _load_chunk_from_db(self, cx: int, cy: int) -> Optional[Chunk]:
        """从 SQLite 加载单个 chunk。"""
        if self.db is None or self.world_id is None:
            return None
        row = self.db.load_chunk(self.world_id, cx, cy)
        if row is None:
            return None
        return Chunk.from_dict(row["data"])

    def update_loaded_chunks(self, wx: float, wy: float) -> None:
        """根据玩家世界坐标加载附近 chunk、卸载远处 chunk。

        由主循环或引擎 step 定期调用。
        """
        cx, cy, _, _ = world_to_chunk(wx, wy, self.chunk_size)

        # 1. 加载半径内所有 chunk
        for dx in range(-self.load_radius, self.load_radius + 1):
            for dy in range(-self.load_radius, self.load_radius + 1):
                self.ensure_chunk(cx + dx, cy + dy)

        # 2. 卸载半径外的 chunk（保留修改过的到数据库）
        to_unload = [
            key
            for key in list(self.chunks.keys())
            if abs(key[0] - cx) > self.unload_radius
            or abs(key[1] - cy) > self.unload_radius
        ]
        for key in to_unload:
            self._unload_chunk(key[0], key[1])

    def _unload_chunk(self, cx: int, cy: int) -> None:
        """卸载单个 chunk，如有修改则持久化到数据库。"""
        key = (cx, cy)
        chunk = self.chunks.pop(key, None)
        if chunk is None:
            return
        if chunk.modified and self.db is not None and self.world_id is not None:
            self.db.save_chunks(
                self.world_id,
                [
                    {
                        "cx": chunk.cx,
                        "cy": chunk.cy,
                        "data": chunk.to_dict(),
                        "summary": None,
                        "modified": True,
                    }
                ],
            )

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
        """程序化生成一个新 chunk：biome -> 道路 -> 建筑/装饰。"""
        from .generation import (
            biome_at,
            generate_buildings_and_decorations,
            generate_roads_for_chunk,
        )
        from .generation.tiles import grass_tile

        biome_name = biome_at(cx, cy, self.seed)
        chunk = Chunk(
            cx=cx, cy=cy, size=self.chunk_size, biome=biome_name, generated=True
        )

        # 初始化图层
        chunk.bg_tiles = [[[-1] * self.chunk_size for _ in range(self.chunk_size)]]
        chunk.obj_tiles = [[[-1] * self.chunk_size for _ in range(self.chunk_size)]]

        # 1. 填充草地背景
        for lx in range(self.chunk_size):
            for ly in range(self.chunk_size):
                chunk.bg_tiles[0][lx][ly] = grass_tile(
                    lx, ly, self.seed + cx * 1009 + cy * 997
                )

        # 2. 收集已加载的邻居（用于道路 portal 对齐）
        neighbors: Dict[Tuple[int, int], Chunk] = {}
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            n = self.get_chunk(cx + dx, cy + dy)
            if n is not None:
                neighbors[(cx + dx, cy + dy)] = n

        # 3. 生成道路网络
        generate_roads_for_chunk(chunk, neighbors, self.seed)

        # 4. 生成建筑与装饰
        generate_buildings_and_decorations(chunk, self.seed)

        # 新生成的 chunk 需要持久化（即使玩家没修改），避免重复生成不同结果
        chunk.modified = True
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

    def attach_db(self, db: "Database", world_id: str) -> None:
        """在从序列化恢复后重新绑定数据库。"""
        self.db = db
        self.world_id = world_id

    @classmethod
    def from_dict(
        cls,
        d: dict,
        db: Optional["Database"] = None,
        world_id: Optional[str] = None,
    ) -> "ChunkManager":
        cm = cls(
            chunk_size=d.get("chunkSize", CHUNK_SIZE),
            seed=d.get("seed", MAP_SEED),
            db=db,
            world_id=world_id,
        )
        for key, cd in d.get("chunks", {}).items():
            cx, cy = (int(x) for x in key.split(","))
            chunk = Chunk.from_dict(cd)
            cm.chunks[(cx, cy)] = chunk
        return cm

    # ---- 工厂方法：从旧地图数据导入 ----
    @classmethod
    def from_legacy_data(
        cls,
        data: dict,
        chunk_size: int = CHUNK_SIZE,
        db: Optional["Database"] = None,
        world_id: Optional[str] = None,
    ) -> "ChunkManager":
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

        cm = cls(chunk_size=chunk_size, db=db, world_id=world_id)
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

        # 为 seed chunks 的外边界生成 portal，确保与后续程序化生成 chunk 的道路对齐。
        cls._assign_seed_portals(cm, num_cx, num_cy)
        return cm

    @classmethod
    def _assign_seed_portals(
        cls, cm: "ChunkManager", num_cx: int, num_cy: int
    ) -> None:
        """扫描每个 seed chunk 的外边界，选择可通行位置作为 portal。"""
        # 道路 tile id 集合（与 generation.tiles 保持一致）
        from .generation.tiles import ROAD_TILES

        road_set = set(ROAD_TILES)

        def pick_portal_pos(chunk: Chunk, edge: str) -> Optional[Tuple[float, float]]:
            size = chunk.size
            candidates: List[Tuple[int, int]] = []
            if edge == "N":
                coords = [(x, 0) for x in range(size)]
            elif edge == "S":
                coords = [(x, size - 1) for x in range(size)]
            elif edge == "W":
                coords = [(0, y) for y in range(size)]
            else:  # E
                coords = [(size - 1, y) for y in range(size)]

            # 优先选没有碰撞的位置
            for x, y in coords:
                if chunk.obj_tiles and chunk.obj_tiles[0][x][y] == -1:
                    candidates.append((x, y))
            # 其次选道路位置
            if not candidates:
                for x, y in coords:
                    if chunk.bg_tiles and chunk.bg_tiles[0][x][y] in road_set:
                        candidates.append((x, y))
            # 兜底：中间位置
            if not candidates:
                mid = size // 2
                candidates.append(coords[mid])

            # 确定性选择中间候选
            idx = len(candidates) // 2
            return float(candidates[idx][0]), float(candidates[idx][1])

        for (cx, cy), chunk in cm.chunks.items():
            portals: List[Portal] = []
            if cx == 0:
                pos = pick_portal_pos(chunk, "W")
                if pos:
                    portals.append(Portal(edge="W", local_x=pos[0], local_y=pos[1]))
            if cx == num_cx - 1:
                pos = pick_portal_pos(chunk, "E")
                if pos:
                    portals.append(Portal(edge="E", local_x=pos[0], local_y=pos[1]))
            if cy == 0:
                pos = pick_portal_pos(chunk, "N")
                if pos:
                    portals.append(Portal(edge="N", local_x=pos[0], local_y=pos[1]))
            if cy == num_cy - 1:
                pos = pick_portal_pos(chunk, "S")
                if pos:
                    portals.append(Portal(edge="S", local_x=pos[0], local_y=pos[1]))
            chunk.portals = portals

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
