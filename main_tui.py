"""TUI entry point for the closed AI town.

Builds the game (agents only, no human player), starts the engine on its
daemon thread, then runs the Textual TUI on the main thread. On exit,
stops the engine cleanly.

Usage:
    python main_tui.py --num-agents 5 --world-id tui-town
    python main_tui.py --reset --world-id tui-town
    python main_tui.py --no-llm
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import AppConfig, DEFAULT_DB_PATH, DEFAULT_MAP_PATH
from db import Database
from engine.bootstrap import build_game
from tui.app import TownApp


def parse_args() -> AppConfig:
    p = argparse.ArgumentParser(description="AI Town TUI — closed town observer")
    p.add_argument("--map", type=Path, default=DEFAULT_MAP_PATH)
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--num-agents", type=int, default=5)
    p.add_argument("--world-id", type=str, default="tui-town")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--reset", action="store_true")
    args = p.parse_args()
    return AppConfig(
        map_path=args.map,
        db_path=args.db,
        num_agents=args.num_agents,
        world_id=args.world_id,
        enable_llm=not args.no_llm,
        reset=args.reset,
    )


def run(config: AppConfig) -> None:
    if config.reset:
        Database(config.db_path).delete_world(config.world_id)
    game = build_game(config)
    game.start()
    try:
        app = TownApp(game)
        app.run()
    finally:
        try:
            game.save_step()
        except Exception:
            pass
        game.stop()


if __name__ == "__main__":
    run(parse_args())
