"""基于 Pygame 的 2D 渲染器。

对应原项目 src/components/{PixiStaticMap,Character,PixiGame}.tsx。

职责
----
- 把 ``WorldMap`` 的 bg / object 层按 ``layer[x][y]`` 索引 blit 到一张大 Surface 上
  （地图静态部分只渲染一次，缓存复用）
- 把 ``animatedSprites`` 按帧切换播放
- 把每个 ``Player`` 在其 ``position`` 处用 spritesheet 当前帧绘制
- 把 ``Conversation`` 的 ``is_typing`` / 最近消息渲染成对话气泡
- ``Camera`` 跟随人类玩家，按屏幕大小做世界→屏幕坐标变换
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pygame

from config import ASSETS_DIR, CONVERSATION_DISTANCE
from dialogue import RoutedMessage
from engine.types import unpack_component
from engine.world_map import WorldMap


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
TILE_DIM = 32                # 每个 tile 的像素边长，原项目固定 32
CHAR_PIXEL = 32              # 角色精灵的像素边长
ANIMATION_FPS = 6            # 角色走路动画帧率（每秒 6 帧）
ANIM_FPS_BACKGROUND = 4      # 背景精灵（火焰 / 水流 / 风车）帧率
SPEECH_BUBBLE_TTL_MS = 4000  # 对话气泡显示时长

# 朝向 -> 动画名（与 data/characters.ts 一致）
_DIRECTIONS = ["right", "down", "left", "up"]


def _facing_to_direction(dx: float, dy: float) -> str:
    """把朝向向量映射到 4 方向动画名。

    与原项目 ``Math.floor(orientation / 90)`` 等价。
    """
    if abs(dx) < 0.001 and abs(dy) < 0.001:
        return "down"
    angle = (math.degrees(math.atan2(dy, dx)) + 360) % 360
    return _DIRECTIONS[int(angle // 90) % 4]


# ---------------------------------------------------------------------------
# 资源加载
# ---------------------------------------------------------------------------
class Tileset:
    """瓦片图集：把整张 PNG 切成 ``num_x * num_y`` 个 tile Surface。"""

    def __init__(self, path: Path, tile_dim: int):
        self.surface = pygame.image.load(str(path)).convert_alpha()
        self.tile_dim = tile_dim
        self.num_x = self.surface.get_width() // tile_dim
        self.num_y = self.surface.get_height() // tile_dim
        self._tiles: List[pygame.Surface] = []
        for ty in range(self.num_y):
            for tx in range(self.num_x):
                rect = pygame.Rect(tx * tile_dim, ty * tile_dim, tile_dim, tile_dim)
                self._tiles.append(self.surface.subsurface(rect))

    def get(self, index: int) -> Optional[pygame.Surface]:
        if index < 0 or index >= len(self._tiles):
            return None
        return self._tiles[index]


class Spritesheet:
    """角色 / 动画精灵表：按 JSON 描述切帧。

    支持 PIXI 风格的 ``frames`` / ``animations`` 字段。
    """

    def __init__(self, image_path: Path, data: Dict[str, Any]):
        self.surface = pygame.image.load(str(image_path)).convert_alpha()
        self.frames: Dict[str, pygame.Surface] = {}
        for name, frame in data.get("frames", {}).items():
            r = frame["frame"]
            rect = pygame.Rect(r["x"], r["y"], r["w"], r["h"])
            self.frames[name] = self.surface.subsurface(rect).copy()
        self.animations: Dict[str, List[str]] = data.get("animations", {})

    def animation_frames(self, name: str) -> List[pygame.Surface]:
        names = self.animations.get(name)
        if not names:
            f = self.frames.get(name)
            return [f] if f else []
        out = []
        for n in names:
            f = self.frames.get(n)
            if f is not None:
                out.append(f)
        return out


def _load_spritesheet(json_path: Path) -> Spritesheet:
    """从 spritesheet JSON 加载，自动找同名 PNG。"""
    data = json.loads(json_path.read_text())
    # 优先使用 meta.image，否则同目录同名 png
    img_rel = data.get("meta", {}).get("image", "")
    if img_rel:
        # meta.image 形如 "./spritesheets/campfire.png" 或 "campfire.png"
        img_name = Path(img_rel).name
        img_path = json_path.parent / img_name
        if not img_path.exists():
            img_path = ASSETS_DIR / img_name
    else:
        img_path = json_path.with_suffix(".png")
    return Spritesheet(img_path, data)


# ---------------------------------------------------------------------------
# 摄像机
# ---------------------------------------------------------------------------
@dataclass
class Camera:
    """世界→屏幕坐标变换。

    世界坐标用 tile 为单位（player.position.x / y 是 tile 坐标 + 小数）；
    屏幕坐标用像素。``camera_x`` / ``camera_y`` 是相机左上角对应的世界像素。
    """
    width: int = 1024
    height: int = 768
    camera_x: float = 0.0
    camera_y: float = 0.0
    # 缩放：1 像素 = 1 世界像素
    zoom: float = 1.0

    def follow(self, world_x: float, world_y: float) -> None:
        """让相机中心对准 ``world_x, world_y``（世界像素）。"""
        target_x = world_x - self.width / (2 * self.zoom)
        target_y = world_y - self.height / (2 * self.zoom)
        # 平滑：相机追着目标走一点，避免抖动
        self.camera_x += (target_x - self.camera_x) * 0.2
        self.camera_y += (target_y - self.camera_y) * 0.2

    def snap(self, world_x: float, world_y: float) -> None:
        """瞬移相机中心到指定位置。"""
        self.camera_x = world_x - self.width / (2 * self.zoom)
        self.camera_y = world_y - self.height / (2 * self.zoom)

    def world_to_screen(self, x_px: float, y_px: float) -> Tuple[float, float]:
        return (x_px - self.camera_x) * self.zoom, (y_px - self.camera_y) * self.zoom

    def screen_to_world(self, sx: float, sy: float) -> Tuple[float, float]:
        return sx / self.zoom + self.camera_x, sy / self.zoom + self.camera_y

    def clamp(self, world_w: int, world_h: int) -> None:
        """限制相机不出地图边界。"""
        max_x = max(0, world_w - self.width / self.zoom)
        max_y = max(0, world_h - self.height / self.zoom)
        self.camera_x = max(0, min(self.camera_x, max_x))
        self.camera_y = max(0, min(self.camera_y, max_y))


# ---------------------------------------------------------------------------
# 气泡
# ---------------------------------------------------------------------------
@dataclass
class SpeechBubble:
    """一条临时显示在角色头顶的对话气泡。"""
    player_id: str
    text: str
    born_at: int


# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------
class Renderer:
    """主渲染器：负责把整个世界画到屏幕上。

    设计上和原项目 PixiGame/PixiStaticMap 对应：
    - 静态地图（bg + obj）只渲染一次，缓存为 Surface
    - 动画精灵按时间播放
    - 角色 / 对话气泡每帧重画
    """

    def __init__(self, world_map: WorldMap, screen: Optional[pygame.Surface] = None):
        self.world_map = world_map
        self.tile_dim = world_map.tile_dim
        self.screen = screen
        self.camera = Camera()

        # 资源
        tileset_path = ASSETS_DIR / Path(world_map.tile_set_url).name
        self.tileset = Tileset(tileset_path, self.tile_dim)

        # 角色精灵表：name -> Spritesheet
        self._character_sheets: Dict[str, Spritesheet] = {}
        self._load_character_sheets()

        # 动画精灵表：sheet 名 -> Spritesheet
        self._anim_sheets: Dict[str, Spritesheet] = {}
        self._load_anim_sheets()

        # 分层地图缓存：按 chunk 缓存背景/建筑 Surface
        self._chunk_surfs: Dict[Tuple[int, int], Tuple[Optional[pygame.Surface], Optional[pygame.Surface]]] = {}

        # 字体（用于对话气泡）
        self._font: Optional[pygame.font.Font] = None
        self._small_font: Optional[pygame.font.Font] = None
        self._init_fonts()

        # 气泡列表
        self._bubbles: List[SpeechBubble] = []

        # 动画时间
        self._anim_clock = 0.0  # 秒

    # ---- 资源加载 ----
    def _load_character_sheets(self) -> None:
        """加载 8 套角色精灵表（共用 32x32folk.png）。"""
        from data.characters import characters
        for c in characters:
            if c.name not in self._character_sheets:
                self._character_sheets[c.name] = Spritesheet(
                    Path(c.texture_url), c.spritesheet,
                )

    def _load_anim_sheets(self) -> None:
        """加载所有动画精灵表（campfire / sparkle / waterfall / windmill / splash）。"""
        sheets_dir = ASSETS_DIR / "spritesheets"
        for json_path in sheets_dir.glob("*.json"):
            try:
                self._anim_sheets[json_path.name] = _load_spritesheet(json_path)
            except Exception as e:  # noqa: BLE001
                print(f"[renderer] failed to load anim {json_path}: {e}")

    def _init_fonts(self) -> None:
        # 优先用项目内置字体
        font_path = ASSETS_DIR / "fonts" / "upheaval_pro.ttf"
        mono_path = ASSETS_DIR / "fonts" / "vcr_osd_mono.ttf"
        try:
            self._font = pygame.font.Font(str(mono_path), 14)
            self._small_font = pygame.font.Font(str(mono_path), 12)
        except Exception:
            self._font = pygame.font.SysFont(None, 16)
            self._small_font = pygame.font.SysFont(None, 12)

    # ---- 分层地图（按 chunk） ----
    def _visible_chunks(self) -> List[Tuple[int, int]]:
        """返回当前视野内（含边界缓冲一个 chunk）的 chunk 坐标列表。"""
        cm = self.world_map.chunk_manager
        cs = cm.chunk_size * self.tile_dim
        left = int(self.camera.camera_x - cs)
        top = int(self.camera.camera_y - cs)
        right = int(self.camera.camera_x + self.camera.width + cs)
        bottom = int(self.camera.camera_y + self.camera.height + cs)

        min_cx = math.floor(left / cs)
        min_cy = math.floor(top / cs)
        max_cx = math.floor(right / cs)
        max_cy = math.floor(bottom / cs)

        visible = []
        for cx in range(min_cx, max_cx + 1):
            for cy in range(min_cy, max_cy + 1):
                if cm.is_loaded(cx, cy):
                    visible.append((cx, cy))
        return visible

    def _build_chunk_bg_surf(self, chunk) -> pygame.Surface:
        """构建单个 chunk 的背景层 Surface。"""
        size_px = chunk.size * self.tile_dim
        surf = pygame.Surface((size_px, size_px), pygame.SRCALPHA).convert_alpha()
        for layer in chunk.bg_tiles:
            for lx in range(chunk.size):
                for ly in range(chunk.size):
                    idx = layer[lx][ly]
                    if idx is None or idx < 0:
                        continue
                    tile = self.tileset.get(idx)
                    if tile is not None:
                        surf.blit(tile, (lx * self.tile_dim, ly * self.tile_dim))
        return surf

    def _build_chunk_building_surf(self, chunk) -> pygame.Surface:
        """构建单个 chunk 的建筑层 Surface。"""
        size_px = chunk.size * self.tile_dim
        surf = pygame.Surface((size_px, size_px), pygame.SRCALPHA).convert_alpha()
        for layer in chunk.obj_tiles:
            for lx in range(chunk.size):
                for ly in range(chunk.size):
                    idx = layer[lx][ly]
                    if idx is None or idx < 0:
                        continue
                    tile = self.tileset.get(idx)
                    if tile is not None:
                        surf.blit(tile, (lx * self.tile_dim, ly * self.tile_dim))
        return surf

    def _get_chunk_surfs(self, cx: int, cy: int) -> Tuple[pygame.Surface, pygame.Surface]:
        """获取 chunk 的背景/建筑 Surface（带缓存）。"""
        cached = self._chunk_surfs.get((cx, cy))
        if cached is not None and cached[0] is not None and cached[1] is not None:
            return cached

        chunk = self.world_map.chunk_manager.get_chunk(cx, cy)
        if chunk is None:
            size_px = self.world_map.chunk_manager.chunk_size * self.tile_dim
            empty = pygame.Surface((size_px, size_px), pygame.SRCALPHA)
            self._chunk_surfs[(cx, cy)] = (empty, empty)
            return empty, empty

        bg = self._build_chunk_bg_surf(chunk)
        building = self._build_chunk_building_surf(chunk)
        self._chunk_surfs[(cx, cy)] = (bg, building)
        return bg, building

    def _clear_chunk_cache(self, cx: int, cy: int) -> None:
        """chunk 数据变化时清除对应缓存。"""
        self._chunk_surfs.pop((cx, cy), None)

    # ---- 气泡 ----
    def add_bubble(self, player_id: str, text: str, now_ms: int) -> None:
        """添加一条对话气泡。"""
        # 去掉旧气泡
        self._bubbles = [b for b in self._bubbles if b.player_id != player_id]
        self._bubbles.append(SpeechBubble(player_id=player_id, text=text, born_at=now_ms))

    def _prune_bubbles(self, now_ms: int) -> None:
        self._bubbles = [
            b for b in self._bubbles if now_ms - b.born_at < SPEECH_BUBBLE_TTL_MS
        ]

    def on_routed_message(self, msg: RoutedMessage) -> None:
        """作为 DialogueRouter 的观察者：每当有消息路由，画一个气泡。"""
        self.add_bubble(msg.author, msg.text, msg.timestamp)

    # ---- 主绘制 ----
    def draw(self, game, now_ms: int, dt_sec: float,
             viewer_player_id: Optional[str] = None,
             debug: bool = False) -> None:
        """主绘制入口。

        Parameters
        ----------
        game : Game
            引擎实例，从中拿 world / world_map / conversations。
        now_ms : int
            当前时间戳（毫秒）。
        dt_sec : float
            上一帧到现在的秒数（用于动画推进）。
        viewer_player_id : str | None
            人类玩家 ID；若有则相机跟随它。
        debug : bool
            是否绘制调试层（碰撞箱、网格、鼠标坐标）。
        """
        if self.screen is None:
            return

        self._anim_clock += dt_sec
        self._prune_bubbles(now_ms)

        # 1. 跟随相机
        if viewer_player_id is not None:
            vp = game.world.players.get(viewer_player_id)
            if vp is not None:
                self.camera.follow(
                    vp.position.x * self.tile_dim,
                    vp.position.y * self.tile_dim,
                )
        # 固定尺寸旧地图：限制相机不出边界；无限生成世界：不限制，
        # 由 ChunkManager 按需加载新区块。
        if self.world_map.map_width and self.world_map.map_height:
            self.camera.clamp(
                self.world_map.width * self.tile_dim,
                self.world_map.height * self.tile_dim,
            )

        # 2. 清屏
        self.screen.fill((0, 0, 0))

        # ---- 场景层（从下到上） ----
        # 3. 背景层 + 4. 建筑层（按 chunk 绘制）
        for cx, cy in self._visible_chunks():
            bg_surf, building_surf = self._get_chunk_surfs(cx, cy)
            offset_x = cx * self.world_map.chunk_manager.chunk_size * self.tile_dim
            offset_y = cy * self.world_map.chunk_manager.chunk_size * self.tile_dim
            sx, sy = self.camera.world_to_screen(offset_x, offset_y)
            self.screen.blit(bg_surf, (int(sx), int(sy)))
            self.screen.blit(building_surf, (int(sx), int(sy)))

        # 5. 动态实体层（人物 + 物品动画精灵）
        # 大五层中，人物层在物品层之上；但实际需要按 y 深度交错遮挡，
        # 所以把两者合并为一个可排序列表，按底部 y 坐标从小到大绘制。
        # y 小的（屏幕上方）先画，y 大的（屏幕下方）后画，后画的覆盖先画的。
        animated = self._build_animated_drawables()
        players = list(game.world.players.values())

        scene_objects: List[Tuple[float, str, Any]] = []
        for player in players:
            scene_objects.append((player.position.y, "player", player))
        for sort_y, surface, sx, sy in animated:
            scene_objects.append((sort_y, "sprite", (surface, sx, sy)))
        scene_objects.sort(key=lambda item: item[0])

        for sort_y, kind, obj in scene_objects:
            if kind == "player":
                self._draw_player(game, obj, now_ms,
                                  is_viewer=(obj.id == viewer_player_id))
            else:
                surface, sx, sy = obj
                self._draw_animated_sprite(surface, sx, sy)

        # ---- UI 层（最上层） ----
        # 6. 对话气泡
        for player in players:
            self._draw_player_bubble(game, player, now_ms)

        # 7. HUD
        self._draw_hud(game, viewer_player_id)

        # 8. 调试层（最高层，覆盖所有内容）
        if debug:
            self._draw_debug_overlay(game)

    # ---- 绘制：动画精灵 ----
    def _build_animated_drawables(self) -> List[Tuple[float, pygame.Surface, float, float]]:
        """构建动画精灵的可绘制项，返回 (sort_y, surface, screen_x, screen_y)。

        不直接绘制，以便与人物按深度排序后统一绘制。
        从所有已加载 chunk 中收集动画精灵。
        """
        drawables: List[Tuple[float, pygame.Surface, float, float]] = []
        cm = self.world_map.chunk_manager
        cs_px = cm.chunk_size * self.tile_dim

        # 按 sheet 分组减少状态切换（跨 chunk 汇总）
        by_sheet: Dict[str, List] = {}
        for (cx, cy), chunk in cm.chunks.items():
            offset_x = cx * cs_px
            offset_y = cy * cs_px
            for spr in chunk.animated_sprites:
                # 转换为世界像素坐标
                world_spr = type(spr)(
                    x=spr.x + offset_x,
                    y=spr.y + offset_y,
                    w=spr.w, h=spr.h, layer=spr.layer,
                    sheet=spr.sheet, animation=spr.animation,
                )
                by_sheet.setdefault(world_spr.sheet, []).append(world_spr)

        for sheet_name, sprites in by_sheet.items():
            sheet = self._anim_sheets.get(sheet_name)
            if sheet is None:
                continue
            frames = sheet.animation_frames(sprites[0].animation)
            if not frames:
                continue
            # 按时间选帧
            frame_idx = int(self._anim_clock * ANIM_FPS_BACKGROUND) % len(frames)
            frame = frames[frame_idx]
            for spr in sprites:
                # 缩放到 spr.w / spr.h（原项目里 windmill 是 208x208）
                if frame.get_width() != spr.w or frame.get_height() != spr.h:
                    scaled = pygame.transform.scale(frame, (int(spr.w), int(spr.h)))
                else:
                    scaled = frame
                sx, sy = self.camera.world_to_screen(spr.x, spr.y)
                # sort_y 用 sprite 底部所在 tile 行，保证与人物按同一深度排序
                sort_y = (spr.y + spr.h) / self.tile_dim
                drawables.append((sort_y, scaled, sx, sy))
        return drawables

    def _draw_animated_sprite(self, surface: pygame.Surface, screen_x: float, screen_y: float) -> None:
        self.screen.blit(surface, (int(screen_x), int(screen_y)))

    # ---- 绘制：单个角色 ----
    def _draw_player(self, game, player, now_ms: int, is_viewer: bool = False) -> None:
        pdesc = game.player_descriptions.get(player.id)
        if pdesc is None:
            return
        sheet = self._character_sheets.get(pdesc.character)
        if sheet is None:
            return

        # 朝向 -> direction 名
        direction = _facing_to_direction(player.facing.dx, player.facing.dy)
        frames = sheet.animation_frames(direction)
        if not frames:
            return

        # 动画帧：移动时按时间循环；静止时显示第一帧
        # player.speed 单位是 tiles/ms（0.75 tiles/s = 0.00075 tiles/ms）
        if player.speed * 1000.0 > 0.1:
            frame_idx = int(self._anim_clock * ANIMATION_FPS) % len(frames)
        else:
            frame_idx = 0
        frame = frames[frame_idx]

        # 世界像素坐标（tile * tile_dim）
        wx = player.position.x * self.tile_dim
        wy = player.position.y * self.tile_dim
        # 让角色脚底落在 tile 中心，所以把 sprite 居中绘制
        sx, sy = self.camera.world_to_screen(wx, wy)
        # sprite 是 32x32，让中心对齐 (sx, sy)
        rect = frame.get_rect(center=(int(sx), int(sy)))
        self.screen.blit(frame, rect)

        # 人类玩家用黄色三角标记
        if is_viewer:
            self._draw_viewer_indicator(rect)

        # 正在打字 -> 💭
        # 正在说话 -> 💬
        conv = next(
            (c for c in game.world.conversations.values()
             if any(m.player_id == player.id for m in c.participants.values())),
            None,
        )
        if conv is not None and conv.is_typing and conv.is_typing["player_id"] == player.id:
            self._draw_emoji("...", rect.right - 6, rect.top - 4)
        elif conv is not None:
            self._draw_emoji("...", rect.right - 6, rect.top - 4)

        # 活动表情
        if player.activity is not None and player.activity.emoji:
            self._draw_emoji(player.activity.emoji, rect.centerx, rect.top - 8)

    def _draw_viewer_indicator(self, rect: pygame.Rect) -> None:
        """画一个黄色的小标记，标识这是 viewer 自己。"""
        # 简单画一个圆点在头顶
        pygame.draw.circle(self.screen, (255, 235, 59),
                           (rect.centerx, rect.top - 4), 3)

    def _draw_emoji(self, text: str, x: int, y: int) -> None:
        """用文字代替 emoji（pygame 字体不一定有 emoji 字形）。"""
        if self._small_font is None:
            return
        surf = self._small_font.render(text, True, (255, 255, 255))
        self.screen.blit(surf, (x - surf.get_width() // 2, y - surf.get_height() // 2))

    # ---- 绘制：对话气泡 ----
    def _draw_player_bubble(self, game, player, now_ms: int) -> None:
        # 找该 player 的最新气泡
        bubble = next(
            (b for b in self._bubbles if b.player_id == player.id),
            None,
        )
        if bubble is None:
            return
        # 渐隐：最后 1 秒淡出
        age = now_ms - bubble.born_at
        ttl = SPEECH_BUBBLE_TTL_MS
        if age > ttl:
            return
        alpha = 255
        if age > ttl - 1000:
            alpha = int(255 * (ttl - age) / 1000)
        alpha = max(0, min(255, alpha))

        if self._font is None:
            return
        # 渲染文字
        text_surf = self._font.render(bubble.text, True, (0, 0, 0))
        pad = 4
        bw = text_surf.get_width() + pad * 2
        bh = text_surf.get_height() + pad * 2
        bubble_surf = pygame.Surface((bw, bh), pygame.SRCALPHA)
        pygame.draw.rect(bubble_surf, (255, 255, 255, int(alpha * 0.85)),
                         bubble_surf.get_rect(), border_radius=4)
        pygame.draw.rect(bubble_surf, (0, 0, 0, alpha),
                         bubble_surf.get_rect(), 1, border_radius=4)
        bubble_surf.blit(text_surf, (pad, pad))
        bubble_surf.set_alpha(alpha)

        wx = player.position.x * self.tile_dim
        wy = player.position.y * self.tile_dim
        sx, sy = self.camera.world_to_screen(wx, wy)
        # 气泡画在角色头顶上方
        x = int(sx) - bw // 2
        y = int(sy) - 32 - bh - 2
        self.screen.blit(bubble_surf, (x, y))

    # ---- 绘制：HUD ----
    def _draw_hud(self, game, viewer_player_id: Optional[str]) -> None:
        """画简单 HUD：玩家名 / 对话状态 / 帮助文字。"""
        if self._small_font is None:
            return
        lines: List[str] = []
        if viewer_player_id is not None:
            pdesc = game.player_descriptions.get(viewer_player_id)
            if pdesc:
                lines.append(f"Player: {pdesc.name}")
            conv = next(
                (c for c in game.world.conversations.values()
                 if any(m.player_id == viewer_player_id for m in c.participants.values())),
                None,
            )
            if conv is not None:
                other_id = next(
                    (pid for pid in conv.participants if pid != viewer_player_id),
                    None,
                )
                other = game.player_descriptions.get(other_id) if other_id else None
                lines.append(f"In conversation with: {other.name if other else '?'}")
                lines.append("[Enter] to type, [Esc] to leave")
            else:
                lines.append("[WASD/Arrows] move, [Space] near NPC to talk")
        lines.append(f"FPS: agents={len(game.world.agents)} players={len(game.world.players)}")

        y = 4
        for line in lines:
            surf = self._small_font.render(line, True, (255, 255, 255))
            # 加阴影
            shadow = self._small_font.render(line, True, (0, 0, 0))
            self.screen.blit(shadow, (5, y + 1))
            self.screen.blit(surf, (4, y))
            y += surf.get_height() + 1

    # ---- 调试层 ----
    def _draw_debug_overlay(self, game) -> None:
        """调试层（最高层）：碰撞网格、寻路路径、对话范围、玩家碰撞箱、鼠标指针、agent 目标。"""
        if self.screen is None:
            return

        # 1. 碰撞网格：object_tiles 中不可通过的格子（按 chunk 遍历）
        grid_color = (255, 0, 0, 60)
        grid_border = (255, 0, 0, 180)
        cell_surf = pygame.Surface((self.tile_dim, self.tile_dim), pygame.SRCALPHA)
        cell_surf.fill(grid_color)

        cm = self.world_map.chunk_manager
        # 复制一份避免后台引擎线程加载/卸载 chunk 时字典改变
        chunks_snapshot = list(cm.chunks.items())
        for (cx, cy), chunk in chunks_snapshot:
            base_x = cx * cm.chunk_size * self.tile_dim
            base_y = cy * cm.chunk_size * self.tile_dim
            for lx in range(chunk.size):
                for ly in range(chunk.size):
                    blocked = any(
                        layer[lx][ly] != -1
                        for layer in chunk.obj_tiles
                        if lx < len(layer) and ly < len(layer[lx])
                    )
                    if not blocked:
                        continue
                    wx = base_x + lx * self.tile_dim
                    wy = base_y + ly * self.tile_dim
                    px, py = self.camera.world_to_screen(wx, wy)
                    # 只画可见区域
                    if -self.tile_dim < px < self.camera.width and -self.tile_dim < py < self.camera.height:
                        self.screen.blit(cell_surf, (int(px), int(py)))
                        pygame.draw.rect(
                            self.screen, grid_border,
                            (int(px), int(py), self.tile_dim, self.tile_dim), 1
                        )

        # 2. 寻路路径与目标点（蓝色）
        players_snapshot = list(game.world.players.values())
        for player in players_snapshot:
            pf = player.pathfinding
            if pf is None:
                continue
            dest = pf["destination"]
            dest_px, dest_py = self.camera.world_to_screen(
                dest.x * self.tile_dim, dest.y * self.tile_dim
            )
            dest_cx = int(dest_px + self.tile_dim / 2)
            dest_cy = int(dest_py + self.tile_dim / 2)

            # 玩家当前位置
            cur_px, cur_py = self.camera.world_to_screen(
                player.position.x * self.tile_dim, player.position.y * self.tile_dim
            )

            # 目标点：蓝色方框 + 圆点
            pygame.draw.rect(
                self.screen, (0, 0, 255),
                (dest_px, dest_py, self.tile_dim, self.tile_dim), 2
            )
            pygame.draw.circle(self.screen, (0, 0, 255), (dest_cx, dest_cy), 4)
            # 玩家到目标点的虚线
            self._draw_dashed_line(
                self.screen, (0, 0, 255),
                (cur_px, cur_py), (dest_cx, dest_cy), dash_len=8
            )

            # 实际寻路路径：青色折线
            state = pf["state"]
            if state.kind == "moving" and state.path:
                path_points = []
                for packed in state.path:
                    comp = unpack_component(packed)
                    px, py = self.camera.world_to_screen(
                        comp.position.x * self.tile_dim, comp.position.y * self.tile_dim
                    )
                    path_points.append((int(px), int(py)))
                if len(path_points) >= 2:
                    pygame.draw.lines(self.screen, (0, 255, 255), False, path_points, 2)
                for px, py in path_points:
                    pygame.draw.circle(self.screen, (0, 255, 255), (px, py), 3)

        # 3. 对话范围（紫色半透明圆）
        conv_radius = int(CONVERSATION_DISTANCE * self.tile_dim)
        conv_surf = pygame.Surface((conv_radius * 2, conv_radius * 2), pygame.SRCALPHA)
        pygame.draw.circle(conv_surf, (255, 0, 255, 40), (conv_radius, conv_radius), conv_radius)
        conversations_snapshot = list(game.world.conversations.values())
        for conversation in conversations_snapshot:
            for member in conversation.participants.values():
                player = game.world.players.get(member.player_id)
                if player is None:
                    continue
                cx, cy = self.camera.world_to_screen(
                    player.position.x * self.tile_dim, player.position.y * self.tile_dim
                )
                self.screen.blit(conv_surf, (int(cx - conv_radius), int(cy - conv_radius)))
                pygame.draw.circle(self.screen, (255, 0, 255), (int(cx), int(cy)), conv_radius, 1)

        # 4. 玩家碰撞箱（绿色圆）
        # movement.py 中人物间碰撞阈值 distance < 0.75 tile，半径取一半
        radius = int(0.375 * self.tile_dim)
        for player in players_snapshot:
            cx, cy = self.camera.world_to_screen(
                player.position.x * self.tile_dim,
                player.position.y * self.tile_dim,
            )
            pygame.draw.circle(self.screen, (0, 255, 0), (int(cx), int(cy)), radius, 2)
            pygame.draw.circle(self.screen, (0, 255, 0), (int(cx), int(cy)), 2)

        # 5. agent 当前操作名（橙色文字）
        if self._small_font is not None:
            agents_snapshot = list(game.world.agents.values())
            for agent in agents_snapshot:
                player = game.world.players.get(agent.player_id)
                if player is None:
                    continue
                label = None
                if agent.in_progress_operation is not None:
                    label = agent.in_progress_operation.name
                elif player.pathfinding is not None:
                    label = "moving"
                elif player.activity is not None:
                    label = player.activity.description
                if label is not None:
                    cx, cy = self.camera.world_to_screen(
                        player.position.x * self.tile_dim,
                        player.position.y * self.tile_dim,
                    )
                    text = f"[{label}]"
                    surf = self._small_font.render(text, True, (255, 165, 0))
                    shadow = self._small_font.render(text, True, (0, 0, 0))
                    self.screen.blit(shadow, (int(cx) + 11, int(cy) - 24))
                    self.screen.blit(surf, (int(cx) + 10, int(cy) - 25))

        # 6. 鼠标 xy 指针
        mx, my = pygame.mouse.get_pos()
        wx = mx + self.camera.camera_x
        wy = my + self.camera.camera_y
        tile_x = wx / self.tile_dim
        tile_y = wy / self.tile_dim

        # 十字线
        pygame.draw.line(self.screen, (255, 255, 0), (mx, 0), (mx, self.camera.height), 1)
        pygame.draw.line(self.screen, (255, 255, 0), (0, my), (self.camera.width, my), 1)
        pygame.draw.circle(self.screen, (255, 255, 0), (mx, my), 4, 1)

        # 坐标文字
        if self._small_font is not None:
            text = f"mouse world: ({wx:.1f}, {wy:.1f})  tile: ({tile_x:.1f}, {tile_y:.1f})"
            surf = self._small_font.render(text, True, (255, 255, 0))
            shadow = self._small_font.render(text, True, (0, 0, 0))
            self.screen.blit(shadow, (11, 11))
            self.screen.blit(surf, (10, 10))

    def _draw_dashed_line(self, surface: pygame.Surface, color,
                          start: Tuple[float, float], end: Tuple[float, float],
                          dash_len: int = 8) -> None:
        """绘制虚线。"""
        import math
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        dist = math.hypot(dx, dy)
        if dist == 0:
            return
        steps = int(dist / dash_len)
        for i in range(0, steps, 2):
            t0 = i / steps
            t1 = min(1.0, (i + 1) / steps)
            x0 = start[0] + dx * t0
            y0 = start[1] + dy * t0
            x1 = start[0] + dx * t1
            y1 = start[1] + dy * t1
            pygame.draw.line(surface, color, (x0, y0), (x1, y1), 1)

    # ---- 工具 ----
    def resize(self, width: int, height: int) -> None:
        self.camera.width = width
        self.camera.height = height
