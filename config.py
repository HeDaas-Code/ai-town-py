"""全局配置：常量、环境变量、LLM 配置。

常量值移植自原项目 convex/constants.ts。
LLM 配置读取环境变量，兼容任意 OpenAI 协议端点（OpenAI / Ollama / Together / 自建）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_ROOT / "assets"
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_MAP_PATH = DATA_DIR / "maps" / "gentle.json"
DEFAULT_DB_PATH = PROJECT_ROOT / "ai_town.db"

# ---------------------------------------------------------------------------
# 引擎 / 模拟常量（移植自 convex/constants.ts）
# ---------------------------------------------------------------------------
TICK_DURATION_MS = 16          # 单 tick 时长，ms（约 60FPS）
STEP_DURATION_MS = 1000        # 单 step 时长，ms
MAX_TICKS_PER_STEP = 600
MAX_INPUTS_PER_STEP = 32
MAX_STEP_MS = 10 * 60 * 1000

PATHFINDING_TIMEOUT_MS = 60 * 1000
PATHFINDING_BACKOFF_MS = 1000
CONVERSATION_DISTANCE = 1.3
MIDPOINT_THRESHOLD = 4
TYPING_TIMEOUT_MS = 15 * 1000
COLLISION_THRESHOLD = 0.75

MAX_HUMAN_PLAYERS = 8
CONVERSATION_COOLDOWN_MS = 15_000
ACTIVITY_COOLDOWN_MS = 10_000
PLAYER_CONVERSATION_COOLDOWN_MS = 60_000
INVITE_ACCEPT_PROBABILITY = 0.8
INVITE_TIMEOUT_MS = 60_000
AWKWARD_CONVERSATION_TIMEOUT_MS = 60_000
MAX_CONVERSATION_DURATION_MS = 10 * 60_000
MAX_CONVERSATION_MESSAGES = 8
INPUT_DELAY_MS = 1000
NUM_MEMORIES_TO_SEARCH = 3
MESSAGE_COOLDOWN_MS = 2000
AGENT_WAKEUP_THRESHOLD_MS = 1000
HUMAN_IDLE_TOO_LONG_MS = 5 * 60 * 1000
MAX_PATHFINDS_PER_STEP = 16
ACTION_TIMEOUT_MS = 120_000
ENGINE_ACTION_DURATION_MS = 30_000

# 角色移动速度（tiles / second），见 data/characters.ts movementSpeed
MOVEMENT_SPEED = 0.75

ACTIVITIES = [
    {"description": "reading a book", "emoji": "📖", "duration_ms": 60_000},
    {"description": "daydreaming", "emoji": "🤔", "duration_ms": 60_000},
    {"description": "gardening", "emoji": "🥕", "duration_ms": 60_000},
]

DEFAULT_NAME = "Me"

# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------
OPENAI_EMBEDDING_DIMENSION = 1536
OLLAMA_EMBEDDING_DIMENSION = 1024


@dataclass
class LLMConfig:
    provider: str          # 'openai' | 'ollama' | 'custom'
    url: str               # 不带尾斜杠
    chat_model: str
    embedding_model: str
    stop_words: list
    api_key: str | None
    embedding_dimension: int


def get_llm_config() -> LLMConfig:
    """从环境变量解析 LLM 配置。

    优先级：
    1. 显式 ``LLM_API_URL`` + ``LLM_MODEL`` + ``LLM_EMBEDDING_MODEL``（custom）
    2. ``OPENAI_API_KEY``（openai）
    3. 默认 Ollama 本地（``OLLAMA_HOST``）
    """
    api_url = os.environ.get("LLM_API_URL")
    if api_url:
        chat_model = os.environ.get("LLM_MODEL")
        if not chat_model:
            raise ValueError("LLM_API_URL 已设置但缺少 LLM_MODEL")
        embedding_model = os.environ.get("LLM_EMBEDDING_MODEL")
        if not embedding_model:
            raise ValueError("LLM_API_URL 已设置但缺少 LLM_EMBEDDING_MODEL")
        return LLMConfig(
            provider="custom",
            url=api_url.rstrip("/"),
            chat_model=chat_model,
            embedding_model=embedding_model,
            stop_words=[],
            api_key=os.environ.get("LLM_API_KEY"),
            embedding_dimension=int(os.environ.get("EMBEDDING_DIMENSION", OPENAI_EMBEDDING_DIMENSION)),
        )

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        return LLMConfig(
            provider="openai",
            url="https://api.openai.com",
            chat_model=os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            embedding_model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-ada-002"),
            stop_words=[],
            api_key=openai_key,
            embedding_dimension=OPENAI_EMBEDDING_DIMENSION,
        )

    # 默认回退到 Ollama 本地
    return LLMConfig(
        provider="ollama",
        url=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
        chat_model=os.environ.get("OLLAMA_MODEL", "llama3"),
        embedding_model=os.environ.get("OLLAMA_EMBEDDING_MODEL", "mxbai-embed-large"),
        stop_words=["<|eot_id|>"],
        api_key=None,
        embedding_dimension=int(os.environ.get("EMBEDDING_DIMENSION", OLLAMA_EMBEDDING_DIMENSION)),
    )


@dataclass
class AppConfig:
    """顶层应用配置，由 main.py 组装后注入各模块。"""

    map_path: Path = DEFAULT_MAP_PATH
    db_path: Path = DEFAULT_DB_PATH
    num_agents: int = 5
    headless: bool = False               # 无窗口模式（CI / 测试）
    target_fps: int = 60
    window_width: int = 1024
    window_height: int = 768
    enable_llm: bool = True              # False 时使用占位回复，便于离线演示
    world_id: str = "default"
    reset: bool = False                  # 启动前清掉该 world_id 的存档
