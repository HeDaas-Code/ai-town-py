"""角色描述与 spritesheet 配置。

对应原项目 data/characters.ts + data/spritesheets/f{1..8}.ts。
spritesheet 图片复用原项目 public/assets/32x32folk.png（384x256）。

32x32folk.png 的布局：每个角色占 96x128 像素块（3 列 × 4 行的 32x32 帧）。
4 行依次是 down / left / right / up，3 列是同一方向的 3 帧动画。
8 个角色排成 4×2 网格：
    f1(0,0)   f2(96,0)   f3(192,0)   f4(288,0)
    f5(0,128) f6(96,128) f7(192,128) f8(288,128)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from config import ASSETS_DIR


_FOLK_PNG = str(ASSETS_DIR / "32x32folk.png")


# 每个角色在 32x32folk.png 中的左上角像素坐标（x, y）
_CHARACTER_OFFSETS: Dict[str, tuple] = {
    "f1": (0, 0),
    "f2": (96, 0),
    "f3": (192, 0),
    "f4": (288, 0),
    "f5": (0, 128),
    "f6": (96, 128),
    "f7": (192, 128),
    "f8": (288, 128),
}

# 每个角色 4 行动画的行偏移（down=0, left=32, right=64, up=96）
_DIRECTION_ROWS: Dict[str, int] = {
    "down": 0,
    "left": 32,
    "right": 64,
    "up": 96,
}

# 每个方向 3 帧动画的列偏移
_FRAME_COLS = [0, 32, 64]


def _frame(x: int, y: int, size: int = 32) -> Dict[str, Any]:
    return {
        "frame": {"x": x, "y": y, "w": size, "h": size},
        "sourceSize": {"w": size, "h": size},
        "spriteSourceSize": {"x": 0, "y": 0},
    }


def _build_spritesheet(name: str) -> Dict[str, Any]:
    """根据角色名构造 spritesheet 描述。

    优先解析原项目 data/spritesheets/f{n}.ts 的精确坐标（保留原作者的像素级偏移），
    解析失败时回退到几何推导的规则坐标。
    """
    ts_path = Path(__file__).resolve().parent / "spritesheets" / f"{name}.ts"
    if ts_path.exists():
        try:
            return _parse_ts_spritesheet(ts_path)
        except Exception:  # noqa: BLE001
            pass

    # 回退：按几何规则生成
    ox, oy = _CHARACTER_OFFSETS[name]
    frames: Dict[str, Any] = {}
    animations: Dict[str, List[str]] = {}
    for direction, row_offset in _DIRECTION_ROWS.items():
        row_y = oy + row_offset
        names = []
        for i, col_offset in enumerate(_FRAME_COLS):
            n = f"{direction}{'' if i == 0 else i + 1}"
            frames[n] = _frame(ox + col_offset, row_y)
            names.append(n)
        animations[direction] = names
    return {"frames": frames, "meta": {"scale": "1"}, "animations": animations}


_TS_FRAME_RE = re.compile(
    r"(\w+)\s*:\s*\{\s*frame\s*:\s*\{\s*x\s*:\s*(\d+)\s*,\s*y\s*:\s*(\d+)\s*,"
    r"\s*w\s*:\s*(\d+)\s*,\s*h\s*:\s*(\d+)\s*\}"
)


def _parse_ts_spritesheet(path: Path) -> Dict[str, Any]:
    """从原项目 spritesheets/f{n}.ts 解析帧坐标。"""
    text = path.read_text(encoding="utf-8")
    frames: Dict[str, Any] = {}
    for m in _TS_FRAME_RE.finditer(text):
        name, x, y, w, h = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        frames[name] = _frame(x, y, h if h == w else w)
    # animations 块按方向聚合
    animations: Dict[str, List[str]] = {}
    for direction in ("down", "left", "right", "up"):
        names = [direction, f"{direction}2", f"{direction}3"]
        if all(n in frames for n in names):
            animations[direction] = names
    return {"frames": frames, "meta": {"scale": "1"}, "animations": animations}


@dataclass
class Character:
    name: str
    texture_url: str
    spritesheet: Dict[str, Any]
    speed: float


characters: List[Character] = [
    Character(name=f"f{i}", texture_url=_FOLK_PNG,
              spritesheet=_build_spritesheet(f"f{i}"), speed=0.1)
    for i in range(1, 9)
]


@dataclass
class CharacterDescription:
    """agent 的预设身份（对应 data/characters.ts 的 Descriptions）。"""

    name: str
    character: str
    identity: str
    plan: str


descriptions: List[CharacterDescription] = [
    CharacterDescription(
        name="Lucky", character="f1",
        identity=(
            "Lucky is always happy and curious, and he loves cheese. He spends most of his time "
            "reading about the history of science and traveling through the galaxy on whatever "
            "ship will take him. He's very articulate and infinitely patient, except when he sees "
            "a squirrel. He's also incredibly loyal and brave. Lucky has just returned from an "
            "amazing space adventure to explore a distant planet and he's very excited to tell "
            "people about it."
        ),
        plan="You want to hear all the gossip.",
    ),
    CharacterDescription(
        name="Bob", character="f4",
        identity=(
            "Bob is always grumpy and he loves trees. He spends most of his time gardening by "
            "himself. When spoken to he'll respond but try and get out of the conversation as "
            "quickly as possible. Secretly he resents that he never went to college."
        ),
        plan="You want to avoid people as much as possible.",
    ),
    CharacterDescription(
        name="Stella", character="f6",
        identity=(
            "Stella can never be trusted. she tries to trick people all the time. normally into "
            "giving her money, or doing things that will make her money. she's incredibly "
            "charming and not afraid to use her charm. she's a sociopath who has no empathy. "
            "but hides it well."
        ),
        plan="You want to take advantage of others as much as possible.",
    ),
    CharacterDescription(
        name="Alice", character="f3",
        identity=(
            "Alice is a famous scientist. She is smarter than everyone else and has discovered "
            "mysteries of the universe no one else can understand. As a result she often speaks "
            "in oblique riddles. She comes across as confused and forgetful."
        ),
        plan="You want to figure out how the world works.",
    ),
    CharacterDescription(
        name="Pete", character="f7",
        identity=(
            "Pete is deeply religious and sees the hand of god or of the work of the devil "
            "everywhere. He can't have a conversation without bringing up his deep faith. Or "
            "warning others about the perils of hell."
        ),
        plan="You want to convert everyone to your religion.",
    ),
]


__all__ = ["Character", "CharacterDescription", "characters", "descriptions"]
