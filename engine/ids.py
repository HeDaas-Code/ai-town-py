"""游戏内 ID 分配。

采用与原项目一致的 ``<type>:<number>`` 字符串形式，前缀字符：
agents=a / conversations=c / players=p / operations=o。
单进程内由 World 维护单调递增的 nextId，保证唯一。
"""
from __future__ import annotations

from typing import Literal

IdTypes = Literal["agents", "conversations", "players", "operations"]

_ID_SHORT_CODES = {"agents": "a", "conversations": "c", "players": "p", "operations": "o"}
_SHORT_TO_TYPE = {v: k for k, v in _ID_SHORT_CODES.items()}

GameId = str  # 类型别名，运行期即普通字符串，便于序列化


def alloc_game_id(id_type: IdTypes, id_number: int) -> GameId:
    code = _ID_SHORT_CODES.get(id_type)
    if code is None:
        raise ValueError(f"Invalid game ID type: {id_type}")
    return f"{code}:{id_number}"


def parse_game_id(id_type: IdTypes, game_id: str) -> GameId:
    if not game_id or len(game_id) < 2 or game_id[1] != ":":
        raise ValueError(f"Invalid game ID: {game_id}")
    type_char = game_id[0]
    actual_type = _SHORT_TO_TYPE.get(type_char)
    if actual_type != id_type:
        raise ValueError(f"Invalid game ID type: expected {id_type}, got {actual_type}")
    try:
        number = int(game_id[2:])
    except ValueError as exc:
        raise ValueError(f"Invalid game ID number: {game_id}") from exc
    if number < 0:
        raise ValueError(f"Invalid game ID number: {game_id}")
    return game_id
