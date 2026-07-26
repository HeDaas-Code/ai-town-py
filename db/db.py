"""SQLite 数据后端。

对应原项目 Convex 的表结构：
- worlds               : 世界快照（id, state_json, updated_at）
- player_descriptions  : 玩家描述（playerId, character, description, name）
- agent_descriptions   : agent 描述（agentId, identity, plan）
- maps                 : 世界地图（worldId, map_json）
- messages             : 对话消息（conversationId, author, text, messageUuid, createdAt）
- memories             : 长期记忆（playerId, description, importance, lastAccess, data_json）
- memory_embeddings    : 记忆向量（playerId, embedding_blob）
- archived_conversations / archived_players / participated_together : 归档与索引

所有写操作走单连接 + WAL，避免并发问题。Engine 在每个 step 结束时
调 ``save_world`` / ``save_diff`` 落盘，重启时从 ``load_world`` 恢复。
"""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import DEFAULT_DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS worlds (
    id           TEXT PRIMARY KEY,
    state_json   TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS maps (
    world_id     TEXT PRIMARY KEY,
    map_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    world_id     TEXT NOT NULL,
    cx           INTEGER NOT NULL,
    cy           INTEGER NOT NULL,
    data_json    TEXT NOT NULL,
    summary_json TEXT,
    modified     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (world_id, cx, cy)
);

CREATE TABLE IF NOT EXISTS player_descriptions (
    world_id     TEXT NOT NULL,
    player_id    TEXT NOT NULL,
    character    TEXT NOT NULL,
    description  TEXT NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (world_id, player_id)
);

CREATE TABLE IF NOT EXISTS agent_descriptions (
    world_id     TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    identity     TEXT NOT NULL,
    plan         TEXT NOT NULL,
    PRIMARY KEY (world_id, agent_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    author          TEXT NOT NULL,
    text            TEXT NOT NULL,
    message_uuid    TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(world_id, conversation_id);

CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id      TEXT NOT NULL,
    player_id     TEXT NOT NULL,
    agent_id      TEXT,
    description   TEXT NOT NULL,
    importance    REAL NOT NULL,
    last_access   INTEGER NOT NULL,
    data_json     TEXT NOT NULL,
    embedding_id  INTEGER,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_player ON memories(player_id);
CREATE INDEX IF NOT EXISTS idx_memories_player_type ON memories(player_id, data_json);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id     TEXT NOT NULL,
    embedding     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS archived_conversations (
    world_id        TEXT NOT NULL,
    id              TEXT NOT NULL,
    created         INTEGER NOT NULL,
    creator         TEXT NOT NULL,
    ended           INTEGER NOT NULL,
    last_message    TEXT,
    num_messages    INTEGER NOT NULL,
    participants    TEXT NOT NULL,
    PRIMARY KEY (world_id, id)
);

CREATE TABLE IF NOT EXISTS archived_players (
    world_id     TEXT NOT NULL,
    id           TEXT NOT NULL,
    player_json  TEXT NOT NULL,
    PRIMARY KEY (world_id, id)
);

CREATE TABLE IF NOT EXISTS participated_together (
    world_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    player1         TEXT NOT NULL,
    player2         TEXT NOT NULL,
    ended           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_edge ON participated_together(world_id, player1, player2);
CREATE INDEX IF NOT EXISTS idx_pt_conv ON participated_together(world_id, conversation_id, player1);

CREATE TABLE IF NOT EXISTS world_engine_state (
    world_id        TEXT PRIMARY KEY,
    last_engine_ts  INTEGER NOT NULL,
    generation      INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    """线程安全的 SQLite 封装。所有方法都在一把锁内执行。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        # check_same_thread=False + 显式锁，允许 engine 线程与 operation 线程共用
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL 提升并发读、写不阻塞读
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()

    # ---- 初始化 ----
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ---- world ----
    def save_world(self, world_id: str, state: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO worlds(id, state_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, "
                "updated_at=excluded.updated_at",
                (world_id, json.dumps(state), int(time.time() * 1000)),
            )
            self._conn.commit()

    def load_world(self, world_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM worlds WHERE id=?", (world_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["state_json"])

    def delete_world(self, world_id: str) -> None:
        """彻底清掉一个 world_id 的所有相关数据（用于 ``--reset``）。"""
        with self._lock:
            # worlds 表的列叫 id（不是 world_id）
            self._conn.execute("DELETE FROM worlds WHERE id=?", (world_id,))
            # memory_embeddings 没有 world_id 列，按 player_id 间接清理：
            # 取出该 world 下所有 memories 的 player_id，再删它们的 embedding
            rows = self._conn.execute(
                "SELECT DISTINCT player_id FROM memories WHERE world_id=?", (world_id,)
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    "DELETE FROM memory_embeddings WHERE player_id=?", (r["player_id"],)
                )
            for tbl in (
                "maps", "player_descriptions", "agent_descriptions",
                "messages", "memories",
                "archived_conversations", "archived_players", "participated_together",
                "world_engine_state",
            ):
                self._conn.execute(f"DELETE FROM {tbl} WHERE world_id=?", (world_id,))
            self._conn.commit()

    def save_map(self, world_id: str, map_dict: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO maps(world_id, map_json) VALUES(?, ?) "
                "ON CONFLICT(world_id) DO UPDATE SET map_json=excluded.map_json",
                (world_id, json.dumps(map_dict)),
            )
            self._conn.commit()

    def load_map(self, world_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT map_json FROM maps WHERE world_id=?", (world_id,)
            ).fetchone()
        return json.loads(row["map_json"]) if row else None

    # ---- chunks ----
    def save_chunks(self, world_id: str, chunks: List[Dict[str, Any]]) -> None:
        """增量保存（新增或更新）一组 chunk。"""
        if not chunks:
            return
        with self._lock:
            for c in chunks:
                self._conn.execute(
                    "INSERT INTO chunks(world_id, cx, cy, data_json, summary_json, modified) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(world_id, cx, cy) DO UPDATE SET "
                    "data_json=COALESCE(excluded.data_json, data_json), "
                    "summary_json=COALESCE(excluded.summary_json, summary_json), "
                    "modified=excluded.modified",
                    (
                        world_id,
                        c["cx"],
                        c["cy"],
                        json.dumps(c["data"]),
                        json.dumps(c.get("summary")),
                        1 if c.get("modified", False) else 0,
                    ),
                )
            self._conn.commit()

    def load_chunks(self, world_id: str) -> List[Dict[str, Any]]:
        """加载 world 的所有完整 chunk 数据。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT cx, cy, data_json, summary_json, modified FROM chunks WHERE world_id=?",
                (world_id,),
            ).fetchall()
        return [
            {
                "cx": r["cx"],
                "cy": r["cy"],
                "data": json.loads(r["data_json"]) if r["data_json"] is not None else None,
                "summary": json.loads(r["summary_json"]) if r["summary_json"] else None,
                "modified": bool(r["modified"]),
            }
            for r in rows
        ]

    def load_chunk(self, world_id: str, cx: int, cy: int) -> Optional[Dict[str, Any]]:
        """加载单个 chunk 数据。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT cx, cy, data_json, summary_json, modified FROM chunks "
                "WHERE world_id=? AND cx=? AND cy=?",
                (world_id, cx, cy),
            ).fetchone()
        if row is None:
            return None
        return {
            "cx": row["cx"],
            "cy": row["cy"],
            "data": json.loads(row["data_json"]) if row["data_json"] is not None else None,
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
            "modified": bool(row["modified"]),
        }

    def clear_modified_chunks(self, world_id: str) -> None:
        """保存完成后把 modified 标记清零。"""
        with self._lock:
            self._conn.execute(
                "UPDATE chunks SET modified=0 WHERE world_id=?", (world_id,)
            )
            self._conn.commit()

    # ---- descriptions ----
    def save_player_descriptions(self, world_id: str, descs: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO player_descriptions(world_id, player_id, character, description, name) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(world_id, player_id) DO UPDATE SET "
                "character=excluded.character, description=excluded.description, name=excluded.name",
                [
                    (world_id, d["playerId"], d["character"], d["description"], d["name"])
                    for d in descs
                ],
            )
            self._conn.commit()

    def load_player_descriptions(self, world_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT player_id, character, description, name FROM player_descriptions "
                "WHERE world_id=?",
                (world_id,),
            ).fetchall()
        return [
            {"playerId": r["player_id"], "character": r["character"],
             "description": r["description"], "name": r["name"]}
            for r in rows
        ]

    def save_agent_descriptions(self, world_id: str, descs: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO agent_descriptions(world_id, agent_id, identity, plan) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(world_id, agent_id) DO UPDATE SET "
                "identity=excluded.identity, plan=excluded.plan",
                [(world_id, d["agentId"], d["identity"], d["plan"]) for d in descs],
            )
            self._conn.commit()

    def load_agent_descriptions(self, world_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id, identity, plan FROM agent_descriptions WHERE world_id=?",
                (world_id,),
            ).fetchall()
        return [
            {"agentId": r["agent_id"], "identity": r["identity"], "plan": r["plan"]}
            for r in rows
        ]

    # ---- messages ----
    def insert_message(
        self, world_id: str, conversation_id: str, author: str, text: str, message_uuid: str
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(world_id, conversation_id, author, text, message_uuid, "
                "created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (world_id, conversation_id, author, text, message_uuid, int(time.time() * 1000)),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_messages(self, world_id: str, conversation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT author, text, message_uuid, created_at FROM messages "
                "WHERE world_id=? AND conversation_id=? ORDER BY id ASC",
                (world_id, conversation_id),
            ).fetchall()
        return [
            {"author": r["author"], "text": r["text"],
             "messageUuid": r["message_uuid"], "createdAt": r["created_at"]}
            for r in rows
        ]

    # ---- memories ----
    def insert_memory(
        self,
        world_id: str,
        player_id: str,
        agent_id: Optional[str],
        description: str,
        importance: float,
        last_access: int,
        data: Dict[str, Any],
        embedding: Optional[List[float]] = None,
    ) -> int:
        with self._lock:
            embedding_id = None
            if embedding is not None:
                cur = self._conn.execute(
                    "INSERT INTO memory_embeddings(player_id, embedding) VALUES(?, ?)",
                    (player_id, _pack_embedding(embedding)),
                )
                embedding_id = cur.lastrowid
            cur = self._conn.execute(
                "INSERT INTO memories(world_id, player_id, agent_id, description, importance, "
                "last_access, data_json, embedding_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (world_id, player_id, agent_id, description, importance, last_access,
                 json.dumps(data), embedding_id, int(time.time() * 1000)),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_memories(
        self, player_id: str, *, limit: int = 100, type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """按创建时间倒序拉取记忆。``type_filter`` 过滤 data.type。"""
        with self._lock:
            if type_filter is None:
                rows = self._conn.execute(
                    "SELECT id, description, importance, last_access, data_json, created_at "
                    "FROM memories WHERE player_id=? ORDER BY id DESC LIMIT ?",
                    (player_id, limit),
                ).fetchall()
            else:
                # data_json 里 type 字段用 LIKE 粗匹配
                pattern = f'"type":"{type_filter}"'
                rows = self._conn.execute(
                    "SELECT id, description, importance, last_access, data_json, created_at "
                    "FROM memories WHERE player_id=? AND data_json LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    (player_id, f"%{pattern}%", limit),
                ).fetchall()
        return [
            {"id": r["id"], "description": r["description"], "importance": r["importance"],
             "lastAccess": r["last_access"], "data": json.loads(r["data_json"]),
             "createdAt": r["created_at"]}
            for r in rows
        ]

    def search_memories(
        self, player_id: str, query_embedding: List[float], limit: int = 30
    ) -> List[Tuple[Dict[str, Any], float]]:
        """向量检索：余弦相似度。返回 (memory, score) 列表，按分数倒序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, m.description, m.importance, m.last_access, m.data_json, "
                "m.created_at, e.embedding "
                "FROM memories m JOIN memory_embeddings e ON m.embedding_id = e.id "
                "WHERE m.player_id=?",
                (player_id,),
            ).fetchall()
        scored: List[Tuple[Dict[str, Any], float]] = []
        for r in rows:
            emb = _unpack_embedding(r["embedding"], expected=-1)
            if not emb:
                continue
            score = _cosine(query_embedding, emb)
            memory = {
                "id": r["id"], "description": r["description"], "importance": r["importance"],
                "lastAccess": r["last_access"], "data": json.loads(r["data_json"]),
                "createdAt": r["created_at"],
            }
            scored.append((memory, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def touch_memory(self, memory_id: int, last_access: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_access=? WHERE id=?", (last_access, memory_id)
            )
            self._conn.commit()

    # ---- 归档 ----
    def archive_conversation(self, world_id: str, conv: Dict[str, Any]) -> None:
        participants = [p["playerId"] for p in conv["participants"]]
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO archived_conversations(world_id, id, created, creator, "
                "ended, last_message, num_messages, participants) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (world_id, conv["id"], conv["created"], conv["creator"],
                 int(time.time() * 1000),
                 json.dumps(conv.get("lastMessage")),
                 conv.get("numMessages", 0),
                 json.dumps(participants)),
            )
            # 写 participated_together 双向边
            for i, p1 in enumerate(participants):
                for j, p2 in enumerate(participants):
                    if i == j:
                        continue
                    self._conn.execute(
                        "INSERT INTO participated_together(world_id, conversation_id, player1, "
                        "player2, ended) VALUES(?, ?, ?, ?, ?)",
                        (world_id, conv["id"], p1, p2, int(time.time() * 1000)),
                    )
            self._conn.commit()

    def archive_player(self, world_id: str, player_dict: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO archived_players(world_id, id, player_json) "
                "VALUES(?, ?, ?)",
                (world_id, player_dict["id"], json.dumps(player_dict)),
            )
            self._conn.commit()

    def last_participated_together(
        self, world_id: str, player1: str, player2: str
    ) -> Optional[int]:
        """返回两人上次同处一对话的 ended 时间戳，无则 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT ended FROM participated_together "
                "WHERE world_id=? AND player1=? AND player2=? "
                "ORDER BY ended DESC LIMIT 1",
                (world_id, player1, player2),
            ).fetchone()
        return row["ended"] if row else None

    def last_archived_conversation(
        self, world_id: str, player1: str, player2: str
    ) -> Optional[Dict[str, Any]]:
        last_together = self.last_participated_together(world_id, player1, player2)
        if last_together is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created, creator, ended, last_message, num_messages, participants "
                "FROM archived_conversations WHERE world_id=? AND "
                "EXISTS(SELECT 1 FROM participated_together WHERE "
                "participated_together.world_id=? AND participated_together.player1=? AND "
                "participated_together.conversation_id=archived_conversations.id) "
                "ORDER BY ended DESC LIMIT 1",
                (world_id, world_id, player1),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"], "created": row["created"], "creator": row["creator"],
            "ended": row["ended"], "lastMessage": json.loads(row["last_message"] or "null"),
            "numMessages": row["num_messages"],
            "participants": json.loads(row["participants"]),
        }

    # ---- engine state ----
    def get_engine_state(self, world_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_engine_ts, generation FROM world_engine_state WHERE world_id=?",
                (world_id,),
            ).fetchone()
        if row is None:
            return {"last_engine_ts": 0, "generation": 0}
        return {"last_engine_ts": row["last_engine_ts"], "generation": row["generation"]}

    def set_engine_state(self, world_id: str, last_engine_ts: int, generation: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO world_engine_state(world_id, last_engine_ts, generation) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(world_id) DO UPDATE SET last_engine_ts=excluded.last_engine_ts, "
                "generation=excluded.generation",
                (world_id, last_engine_ts, generation),
            )
            self._conn.commit()


# ---- 向量编码 ----
def _pack_embedding(vec: List[float]) -> bytes:
    """打包成 little-endian float32 数组，便于存储与读取。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes, expected: int = -1) -> List[float]:
    if not blob:
        return []
    n = len(blob) // 4
    if expected > 0 and n != expected:
        # 维度不匹配，跳过
        return []
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
