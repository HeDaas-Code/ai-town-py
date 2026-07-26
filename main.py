"""AI Town Python 版主入口。

启动流程：
1. 读取 ``AppConfig``（命令行 + 环境变量）
2. ``engine.bootstrap.build_game`` 装配引擎、agent、对话路由
3. 启动引擎后台线程（``Game.start``）
4. 创建人类玩家，注册到对话路由
5. Pygame 主循环：渲染 + 输入处理

控制方式
--------
- 方向键 / WASD：移动（点击目标 tile 寻路过去）
- 空格：与最近的 NPC 发起对话
- Enter：在对话中按 Enter 进入文字输入模式
- Esc：离开当前对话 / 退出文字输入模式
- F1：调试覆盖层（FPS / agent 状态）
- F2：手动保存
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# 把项目根加入 sys.path，让 ``from config import ...`` 这种顶层导入能工作
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pygame

from config import AppConfig, DEFAULT_NAME
from data.characters import characters
from dialogue import RoutedMessage
from engine.bootstrap import build_game
from engine.geometry import distance
from engine.ids import GameId
from engine.movement import move_player
from engine.player import Player
from engine.types import Point
from renderer import Renderer


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> AppConfig:
    parser = argparse.ArgumentParser(description="AI Town (Python 版)")
    parser.add_argument("--map", type=Path, default=None,
                        help="地图 JSON 路径，默认 data/maps/gentle.json")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite 数据库路径，默认 ai_town.db")
    parser.add_argument("--num-agents", type=int, default=5,
                        help="初始 agent 数量")
    parser.add_argument("--world-id", type=str, default="default",
                        help="世界 ID，不同 ID 互不影响")
    parser.add_argument("--width", type=int, default=1024, help="窗口宽度")
    parser.add_argument("--height", type=int, default=768, help="窗口高度")
    parser.add_argument("--fps", type=int, default=60, help="目标 FPS")
    parser.add_argument("--headless", action="store_true",
                        help="无窗口模式（CI / 自动化测试）")
    parser.add_argument("--no-llm", action="store_true",
                        help="禁用 LLM，使用占位回复（离线演示）")
    parser.add_argument("--reset", action="store_true",
                        help="重置 world_id 的存档后再启动")
    args = parser.parse_args()

    return AppConfig(
        map_path=args.map or AppConfig.map_path,
        db_path=args.db or AppConfig.db_path,
        num_agents=args.num_agents,
        headless=args.headless,
        target_fps=args.fps,
        window_width=args.width,
        window_height=args.height,
        enable_llm=not args.no_llm,
        world_id=args.world_id,
        reset=args.reset,
    )


# ---------------------------------------------------------------------------
# 人类玩家
# ---------------------------------------------------------------------------
def join_human_player(game, now_ms: int, name: str = DEFAULT_NAME) -> GameId:
    """让一个人类玩家加入游戏。"""
    # 随机选一个 character，避免和 agent 撞
    used = {pd.character for pd in game.player_descriptions.values()}
    available = [c for c in characters if c.name not in used] or characters
    char = random.choice(available)
    return Player.join(
        game, now_ms, name=name, character=char.name,
        description=f"You are {name}, a visitor to the AI town.",
        token_identifier=f"human-{uuid.uuid4().hex[:8]}",
    )


def register_human_to_router(game, player_id: GameId, name: str, renderer: Renderer) -> None:
    """把人类玩家注册到对话路由，让 agent 能给它发消息（气泡渲染）。"""
    from dialogue import AgentEndpoint

    def handler(msg: RoutedMessage) -> None:
        # 人类玩家收到 agent 的消息：渲染器已经通过 observer 显示气泡了，
        # 这里不需要再做啥
        pass

    game.dialogue_router.register(AgentEndpoint(
        agent_id=f"human-{player_id}",
        player_id=player_id,
        name=name,
        handler=handler,
    ))


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run(config: AppConfig) -> None:
    # 如果指定了 --reset，先把存档清掉
    if config.reset:
        from db import Database
        Database(config.db_path).delete_world(config.world_id)
        print(f"[main] reset world '{config.world_id}'")

    game = build_game(config)

    # 启动引擎
    game.start()

    now_ms = int(time.time() * 1000)
    viewer_id = join_human_player(game, now_ms, name=DEFAULT_NAME)
    game.player_descriptions[viewer_id].character = game.player_descriptions[viewer_id].character
    pdesc = game.player_descriptions[viewer_id]

    if config.headless:
        _run_headless(game, config, viewer_id)
        return

    # ---- Pygame ----
    pygame.init()
    pygame.display.set_caption("AI Town (Python)")
    screen = pygame.display.set_mode(
        (config.window_width, config.window_height),
        pygame.SCALED | pygame.RESIZABLE,
        vsync=1,
    )
    clock = pygame.time.Clock()

    renderer = Renderer(game.world_map, screen=screen)
    renderer.camera.width = config.window_width
    renderer.camera.height = config.window_height

    # 把 renderer 注册成对话路由观察者，每条路由消息都画一个气泡
    game.dialogue_router.add_observer(renderer.on_routed_message)
    register_human_to_router(game, viewer_id, pdesc.name, renderer)

    # 让相机立即对准玩家
    vp = game.world.players[viewer_id]
    renderer.camera.snap(
        vp.position.x * renderer.tile_dim,
        vp.position.y * renderer.tile_dim,
    )

    # 输入状态
    input_mode = False        # 是否在文字输入模式
    input_text = ""           # 已输入的文字
    last_move_dir: Optional[Point] = None
    last_move_at = 0.0
    show_debug = False
    running = True

    try:
        while running:
            dt = clock.tick(config.target_fps) / 1000.0
            now_ms = int(time.time() * 1000)

            # ---- 事件 ----
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.VIDEORESIZE:
                    renderer.resize(event.w, event.h)
                elif event.type == pygame.KEYDOWN:
                    if input_mode:
                        if event.key == pygame.K_ESCAPE:
                            input_mode = False
                            input_text = ""
                        elif event.key == pygame.K_RETURN:
                            # 发送消息
                            text = input_text.strip()
                            input_mode = False
                            input_text = ""
                            if text:
                                _send_human_message(game, viewer_id, text, now_ms)
                        elif event.key == pygame.K_BACKSPACE:
                            input_text = input_text[:-1]
                        else:
                            input_text += event.unicode
                    else:
                        if event.key == pygame.K_ESCAPE:
                            # 在对话里 -> 离开对话；否则退出
                            if _in_conversation(game, viewer_id):
                                _leave_conversation(game, viewer_id, now_ms)
                            else:
                                running = False
                        elif event.key == pygame.K_RETURN:
                            if _in_conversation(game, viewer_id):
                                input_mode = True
                                input_text = ""
                        elif event.key == pygame.K_SPACE:
                            _try_start_conversation(game, viewer_id, now_ms)
                        elif event.key == pygame.K_F1:
                            show_debug = not show_debug
                        elif event.key == pygame.K_F2:
                            game.save_step()
                            print("[main] saved")
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    # 左键：寻路到点击位置
                    if not input_mode:
                        _click_move(game, viewer_id, event.pos, renderer, now_ms)

            # ---- 持续按键移动（每 250ms 触发一次寻路）----
            if not input_mode and not _in_conversation(game, viewer_id):
                keys = pygame.key.get_pressed()
                dx = (1 if keys[pygame.K_d] or keys[pygame.K_RIGHT] else 0) - \
                     (1 if keys[pygame.K_a] or keys[pygame.K_LEFT] else 0)
                dy = (1 if keys[pygame.K_s] or keys[pygame.K_DOWN] else 0) - \
                     (1 if keys[pygame.K_w] or keys[pygame.K_UP] else 0)
                if dx != 0 or dy != 0:
                    if now_ms - last_move_at > 250:
                        _step_move(game, viewer_id, dx, dy, now_ms)
                        last_move_at = now_ms

            # ---- 绘制 ----
            renderer.draw(game, now_ms, dt, viewer_player_id=viewer_id)

            # ---- 文字输入框 ----
            if input_mode:
                _draw_input_box(screen, renderer, input_text)

            # ---- 调试覆盖 ----
            if show_debug:
                _draw_debug(screen, renderer, game, viewer_id, clock)

            pygame.display.flip()
    finally:
        try:
            game.save_step()
        except Exception:
            pass
        game.stop()
        pygame.quit()


# ---------------------------------------------------------------------------
# 输入辅助
# ---------------------------------------------------------------------------
def _in_conversation(game, player_id: GameId) -> bool:
    return any(
        any(m.player_id == player_id for m in c.participants.values())
        for c in game.world.conversations.values()
    )


def _step_move(game, player_id: GameId, dx: int, dy: int, now_ms: int) -> None:
    """朝 (dx, dy) 方向走一格。"""
    player = game.world.players.get(player_id)
    if player is None:
        return
    # 必须先停下当前寻路
    if player.pathfinding is not None:
        from engine.movement import stop_player
        stop_player(player)
    # 取整当前位置
    cx = int(math.floor(player.position.x))
    cy = int(math.floor(player.position.y))
    dest = Point(cx + dx, cy + dy)
    try:
        move_player(game, now_ms, player, dest)
    except Exception as e:  # noqa: BLE001
        # 可能被堵，忽略
        pass


def _click_move(game, player_id: GameId, screen_pos, renderer: Renderer, now_ms: int) -> None:
    """点击屏幕坐标，把玩家寻路到对应的 tile。"""
    player = game.world.players.get(player_id)
    if player is None:
        return
    # 屏幕坐标 -> 世界像素 -> tile 坐标
    wx, wy = renderer.camera.screen_to_world(*screen_pos)
    tx = int(math.floor(wx / renderer.tile_dim))
    ty = int(math.floor(wy / renderer.tile_dim))
    dest = Point(tx, ty)
    try:
        move_player(game, now_ms, player, dest)
    except Exception as e:  # noqa: BLE001
        print(f"[main] move failed: {e}")


def _try_start_conversation(game, player_id: GameId, now_ms: int) -> None:
    """和最近的 NPC 发起对话。"""
    player = game.world.players.get(player_id)
    if player is None:
        return
    if _in_conversation(game, player_id):
        return
    # 找最近的可对话玩家（距离 < 2，且不在对话中）
    candidates = []
    for other in game.world.players.values():
        if other.id == player_id:
            continue
        if _in_conversation(game, other.id):
            continue
        d = distance(player.position, other.position)
        if d < 2.5:
            candidates.append((d, other))
    if not candidates:
        print("[main] no nearby NPC to talk to")
        return
    candidates.sort(key=lambda x: x[0])
    _, other = candidates[0]
    game.enqueue_input("startConversation", {
        "playerId": player_id, "invitee": other.id,
    })


def _send_human_message(game, player_id: GameId, text: str, now_ms: int) -> None:
    """人类玩家发消息。"""
    conv = next(
        (c for c in game.world.conversations.values()
         if any(m.player_id == player_id for m in c.participants.values())),
        None,
    )
    if conv is None:
        return
    game.enqueue_input("sendHumanMessage", {
        "playerId": player_id,
        "conversationId": conv.id,
        "text": text,
        "messageUuid": str(uuid.uuid4()),
    })
    # 直接显示气泡（不等 input 处理）
    # 渲染器作为 observer 会在 input 处理时收到，但提前显示更顺滑


def _leave_conversation(game, player_id: GameId, now_ms: int) -> None:
    conv = next(
        (c for c in game.world.conversations.values()
         if any(m.player_id == player_id for m in c.participants.values())),
        None,
    )
    if conv is None:
        return
    game.enqueue_input("leaveConversation", {
        "playerId": player_id, "conversationId": conv.id,
    })


# ---------------------------------------------------------------------------
# UI 辅助
# ---------------------------------------------------------------------------
def _draw_input_box(screen, renderer: Renderer, text: str) -> None:
    font = renderer._font
    if font is None:
        return
    prompt = f"> {text}_"
    surf = font.render(prompt, True, (0, 0, 0))
    pad = 6
    bw = surf.get_width() + pad * 2
    bh = surf.get_height() + pad * 2
    box = pygame.Surface((bw, bh), pygame.SRCALPHA)
    pygame.draw.rect(box, (255, 255, 255, 230), box.get_rect(), border_radius=4)
    pygame.draw.rect(box, (0, 0, 0), box.get_rect(), 1, border_radius=4)
    box.blit(surf, (pad, pad))
    screen.blit(box, (screen.get_width() // 2 - bw // 2,
                       screen.get_height() - bh - 16))


def _draw_debug(screen, renderer: Renderer, game, viewer_id: GameId, clock) -> None:
    font = renderer._small_font
    if font is None:
        return
    lines = [
        f"FPS: {clock.get_fps():.1f}",
        f"Players: {len(game.world.players)}",
        f"Agents: {len(game.world.agents)}",
        f"Conversations: {len(game.world.conversations)}",
        f"Pending inputs: {len(game._pending_inputs)}",
        f"Pending ops: {len(game.pending_operations)}",
    ]
    vp = game.world.players.get(viewer_id)
    if vp is not None:
        lines.append(f"Pos: ({vp.position.x:.2f}, {vp.position.y:.2f})")
        lines.append(f"Speed: {vp.speed:.2f}")
    y = screen.get_height() - len(lines) * 14 - 4
    for line in lines:
        surf = font.render(line, True, (255, 255, 0))
        shadow = font.render(line, True, (0, 0, 0))
        screen.blit(shadow, (screen.get_width() - surf.get_width() - 4, y + 1))
        screen.blit(surf, (screen.get_width() - surf.get_width() - 5, y))
        y += 14


# ---------------------------------------------------------------------------
# Headless 模式
# ---------------------------------------------------------------------------
def _run_headless(game, config: AppConfig, viewer_id: GameId) -> None:
    """无窗口跑 N 秒，用于 CI 验证引擎能跑起来。"""
    print(f"[headless] running for 10s, world_id={config.world_id}")
    end = time.time() + 10
    try:
        while time.time() < end:
            time.sleep(0.5)
            stats = (
                f"players={len(game.world.players)} "
                f"agents={len(game.world.agents)} "
                f"convs={len(game.world.conversations)} "
                f"pending_ops={len(game.pending_operations)}"
            )
            print(f"[headless] {stats}")
    finally:
        game.stop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    config = parse_args()
    run(config)
