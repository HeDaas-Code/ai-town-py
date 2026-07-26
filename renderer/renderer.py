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

from config import ASSETS_DIR
from dialogue import RoutedMessage
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

        # 静态地图缓存
        self._static_map: Optional[pygame.Surface] = None

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

    # ---- 静态地图 ----
    def _build_static_map(self) -> pygame.Surface:
        """把 bg / object 层一次性 blit 到大 Surface 上。

        与原项目 PixiStaticMap.create 一致：
        ``layer[x][y]`` -> tileIndex，若为 -1 跳过。
        """
        w = self.world_map.width * self.tile_dim
        h = self.world_map.height * self.tile_dim
        surf = pygame.Surface((w, h), pygame.SRCALPHA).convert_alpha()
        all_layers = list(self.world_map.bg_tiles) + list(self.world_map.object_tiles)
        for x in range(self.world_map.width):
            for y in range(self.world_map.height):
                px = x * self.tile_dim
                py = y * self.tile_dim
                for layer in all_layers:
                    if x >= len(layer) or y >= len(layer[x]):
                        continue
                    idx = layer[x][y]
                    if idx is None or idx < 0:
                        continue
                    tile = self.tileset.get(idx)
                    if tile is not None:
                        surf.blit(tile, (px, py))
        return surf

    def _get_static_map(self) -> pygame.Surface:
        if self._static_map is None:
            self._static_map = self._build_static_map()
        return self._static_map

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
    def draw(self, game, now_ms: int, dt_sec: float, viewer_player_id: Optional[str] = None) -> None:
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
        # clamp 到地图边界
        self.camera.clamp(
            self.world_map.width * self.tile_dim,
            self.world_map.height * self.tile_dim,
        )

        # 2. 清屏
        self.screen.fill((0, 0, 0))

        # 3. 画静态地图（裁剪到可见区域）
        static_map = self._get_static_map()
        cam_x = int(self.camera.camera_x)
        cam_y = int(self.camera.camera_y)
        # 直接 blit 大图，pygame 会按需裁剪
        self.screen.blit(static_map, (-cam_x, -cam_y))

        # 4. 画背景动画精灵（campfire / waterfall 等）
        self._draw_animated_sprites()

        # 5. 画玩家
        # 排序：让 y 大的（屏幕下方）后画，保证遮挡正确
        players = sorted(game.world.players.values(),
                         key=lambda p: p.position.y)
        for player in players:
            self._draw_player(game, player, now_ms,
                              is_viewer=(player.id == viewer_player_id))

        # 6. 画对话气泡
        for player in players:
            self._draw_player_bubble(game, player, now_ms)

        # 7. 画 HUD
        self._draw_hud(game, viewer_player_id)

    # ---- 绘制：动画精灵 ----
    def _draw_animated_sprites(self) -> None:
        """画地图上的动画精灵（火焰 / 水流 / 风车等）。"""
        # 按 sheet 分组减少状态切换
        by_sheet: Dict[str, List] = {}
        for spr in self.world_map.animated_sprites:
            by_sheet.setdefault(spr.sheet, []).append(spr)

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
                self.screen.blit(scaled, (int(sx), int(sy)))

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
        if player.speed > 0.001:
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

    # ---- 工具 ----
    def resize(self, width: int, height: int) -> None:
        self.camera.width = width
        self.camera.height = height
