"""记忆存储 + 检索 + 反思。

移植自原项目 convex/agent/memory.ts。每条记忆带：
- description  : 自然语言摘要（喂给 LLM 当上下文）
- importance   : 0-9 重要度
- lastAccess   : 上次被检索到的时间戳
- data         : {type: 'conversation' | 'reflection', ...}
- embedding    : 用于向量检索（落 SQLite）

检索时按 relevance + recency + importance 综合排序，与原项目一致。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import NUM_MEMORIES_TO_SEARCH, PLAYER_CONVERSATION_COOLDOWN_MS
from db import Database
from llm import LLMClient, LLMMessage


# 反射阈值：最近 100 条记忆的重要度之和超过此值才触发反思
REFLECTION_IMPORTANCE_THRESHOLD = 500
# 同一记忆多久内不重复 touch
MEMORY_ACCESS_THROTTLE_MS = 300_000
# 检索时过取的倍数（原项目用 10x，给 recency/importance 排序留余地）
MEMORY_OVERFETCH = 10


@dataclass
class Memory:
    id: Optional[int]
    player_id: str
    description: str
    importance: float
    last_access: int
    data: Dict[str, Any]
    created_at: int


class MemoryStore:
    """SQLite 后端的记忆存储。"""

    def __init__(self, db: Database, world_id: str):
        self._db = db
        self._world_id = world_id

    def insert(
        self,
        player_id: str,
        agent_id: Optional[str],
        description: str,
        importance: float,
        last_access: int,
        data: Dict[str, Any],
        embedding: Optional[List[float]] = None,
    ) -> int:
        return self._db.insert_memory(
            self._world_id, player_id, agent_id, description, importance,
            last_access, data, embedding,
        )

    def list_recent(
        self, player_id: str, limit: int = 100, type_filter: Optional[str] = None
    ) -> List[Memory]:
        rows = self._db.list_memories(player_id, limit=limit, type_filter=type_filter)
        return [Memory(**r) for r in rows]

    def search(
        self, player_id: str, query_embedding: List[float], n: int = NUM_MEMORIES_TO_SEARCH
    ) -> List[Memory]:
        """向量检索 + relevance/recency/importance 排序。"""
        candidates = self._db.search_memories(
            player_id, query_embedding, limit=n * MEMORY_OVERFETCH
        )
        if not candidates:
            return []
        ts = int(time.time() * 1000)

        # 计算 recency / 排序范围
        recencies = [
            0.99 ** ((ts - m["lastAccess"]) / 1000 / 60 / 60) for m, _ in candidates
        ]
        importances = [m["importance"] for m, _ in candidates]
        scores = [s for _, s in candidates]
        rel_range = (min(scores), max(scores)) if scores else (0.0, 0.0)
        imp_range = (min(importances), max(importances)) if importances else (0.0, 0.0)
        rec_range = (min(recencies), max(recencies)) if recencies else (0.0, 0.0)

        def normalize(v: float, lo: float, hi: float) -> float:
            return 0.0 if hi == lo else (v - lo) / (hi - lo)

        ranked = sorted(
            (
                (m, normalize(s, *rel_range) + normalize(imp, *imp_range)
                 + normalize(rec, *rec_range))
                for (m, s), imp, rec in zip(candidates, importances, recencies)
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        top = ranked[:n]
        # touch
        for m, _ in top:
            if m["lastAccess"] < ts - MEMORY_ACCESS_THROTTLE_MS:
                self._db.touch_memory(m["id"], ts)
        return [Memory(id=m["id"], player_id=m["player_id"] if "player_id" in m else player_id,
                       description=m["description"], importance=m["importance"],
                       last_access=m["lastAccess"], data=m["data"],
                       created_at=m["createdAt"]) for m, _ in top]

    def latest_of_type(self, player_id: str, type_str: str) -> Optional[Memory]:
        rows = self._db.list_memories(player_id, limit=1, type_filter=type_str)
        if not rows:
            return None
        return Memory(**rows[0])

    def sum_importance_since(self, player_id: str, since_ts: int) -> float:
        """统计某时间点之后所有记忆的重要度之和（用于触发反思）。"""
        rows = self._db.list_memories(player_id, limit=10000)
        return sum(r["importance"] for r in rows if r["createdAt"] > since_ts)


# ---- 高阶操作 ----
def remember_conversation(
    llm: LLMClient,
    db: Database,
    memory_store: MemoryStore,
    world_id: str,
    agent_id: str,
    player_id: str,
    conversation_id: str,
    player_name: str,
    other_player_name: str,
) -> str:
    """对话结束后，让 agent 总结这次对话并存为记忆。"""
    messages = db.list_messages(world_id, conversation_id)
    if not messages:
        return ""

    # 构造 LLM prompt：以第一人称总结对话
    llm_messages: List[LLMMessage] = [
        LLMMessage(
            role="user",
            content=(
                f"You are {player_name}, and you just finished a conversation with "
                f"{other_player_name}. I would like you to summarize the conversation from "
                f"{player_name}'s perspective, using first-person pronouns like 'I,' and add "
                f"if you liked or disliked this interaction."
            ),
        )
    ]
    for msg in messages:
        author_name = player_name if msg["author"] == player_id else other_player_name
        recipient_name = other_player_name if msg["author"] == player_id else player_name
        llm_messages.append(
            LLMMessage(
                role="user",
                content=f"{author_name} to {recipient_name}: {msg['text']}",
            )
        )
    llm_messages.append(LLMMessage(role="user", content="Summary:"))

    result = llm.chat_completion(llm_messages, max_tokens=500)
    description = (
        f"Conversation with {other_player_name} at "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}: {result.content}"
    )

    importance = _calculate_importance(llm, description)
    embedding = llm.fetch_embedding(description).embedding

    # 谁参与过这对话
    other_player_id = next(
        (m["author"] for m in messages if m["author"] != player_id), None
    )
    player_ids = [other_player_id] if other_player_id else []

    memory_store.insert(
        player_id=player_id,
        agent_id=agent_id,
        description=description,
        importance=importance,
        last_access=int(time.time() * 1000),
        data={"type": "conversation", "conversationId": conversation_id,
              "playerIds": player_ids},
        embedding=embedding,
    )

    # 触发反思（可选）
    try:
        reflect_on_memories(llm, memory_store, player_id, player_name)
    except Exception:  # noqa: BLE001
        # 反思失败不影响主流程
        pass

    return description


def _calculate_importance(llm: LLMClient, description: str) -> float:
    """让 LLM 给记忆打 0-9 的重要度分。"""
    result = llm.chat_completion(
        [
            LLMMessage(
                role="user",
                content=(
                    "On the scale of 0 to 9, where 0 is purely mundane (e.g., brushing teeth, "
                    "making bed) and 9 is extremely poignant (e.g., a break up, college "
                    "acceptance), rate the likely poignancy of the following piece of memory.\n"
                    f"Memory: {description}\n"
                    'Answer on a scale of 0 to 9. Respond with number only, e.g. "5"'
                ),
            )
        ],
        max_tokens=1,
        temperature=0.0,
    )
    raw = result.content.strip()
    try:
        return float(raw)
    except ValueError:
        # 兜底：从字符串里抠数字
        for tok in raw:
            if tok.isdigit():
                return float(tok)
        return 5.0


def reflect_on_memories(
    llm: LLMClient,
    memory_store: MemoryStore,
    player_id: str,
    player_name: str,
) -> bool:
    """累积重要度足够后，让 LLM 抽出 3 条高层洞察。"""
    last_reflection = memory_store.latest_of_type(player_id, "reflection")
    last_ts = last_reflection.created_at if last_reflection else 0
    total = memory_store.sum_importance_since(player_id, last_ts)
    if total <= REFLECTION_IMPORTANCE_THRESHOLD:
        return False

    memories = memory_store.list_recent(player_id, limit=100)
    if not memories:
        return False

    prompt = [
        "[no prose]",
        "[Output only JSON]",
        f"You are {player_name}, statements about you:",
    ]
    for idx, m in enumerate(memories):
        prompt.append(f"Statement {idx}: {m.description}")
    prompt.append("What 3 high-level insights can you infer from the above statements?")
    prompt.append(
        "Return in JSON format, where the key is a list of input statements that contributed to "
        "your insights and value is your insight. Make the response parseable by Typescript "
        'JSON.parse() function. DO NOT escape characters or include "\\n" or white space in response.'
    )
    prompt.append(
        'Example: [{insight: "...", statementIds: [1,2]}, {insight: "...", statementIds: [1]}, ...]'
    )

    result = llm.chat_completion(
        [LLMMessage(role="user", content="\n".join(prompt))],
        max_tokens=500,
    )
    try:
        insights = json.loads(result.content)
    except json.JSONDecodeError:
        # 容错：尝试从 markdown ```json ... ``` 块里提取
        text = result.content
        if "```" in text:
            seg = text.split("```")[1]
            if seg.startswith("json"):
                seg = seg[4:]
            insights = json.loads(seg)
        else:
            return False

    for item in insights:
        description = item.get("insight", "")
        if not description:
            continue
        importance = _calculate_importance(llm, description)
        embedding = llm.fetch_embedding(description).embedding
        related_ids = [
            memories[i].id for i in item.get("statementIds", []) if 0 <= i < len(memories)
        ]
        memory_store.insert(
            player_id=player_id,
            agent_id=None,
            description=description,
            importance=importance,
            last_access=int(time.time() * 1000),
            data={"type": "reflection", "relatedMemoryIds": related_ids},
            embedding=embedding,
        )
    return True
