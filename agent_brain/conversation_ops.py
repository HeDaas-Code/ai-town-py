"""对话消息生成：start / continue / leave。

移植自原项目 convex/agent/conversation.ts。每次 agent 要说话时，
brain 调这里的函数让 LLM 生成一条消息，然后通过 DialogueRouter 转发。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from config import PLAYER_CONVERSATION_COOLDOWN_MS
from db import Database
from llm import LLMClient, LLMMessage
from .memory import MemoryStore


def _stop_words(other_name: str, player_name: str) -> List[str]:
    """OpenAI stop words 只支持 4 个，原项目只用一组变形。"""
    base = f"{other_name} to {player_name}"
    return [base + ":", base.lower() + ":"]


def _trim_prefix(content: str, prefix: str) -> str:
    if content.startswith(prefix):
        return content[len(prefix):].strip()
    return content


def _related_memories_prompt(memories) -> List[str]:
    if not memories:
        return []
    out = ["Here are some related memories in decreasing relevance order:"]
    for m in memories:
        out.append(f" - {m.description}")
    return out


def _agent_prompts(other_player_name: str, agent_desc, other_agent_desc) -> List[str]:
    out: List[str] = []
    if agent_desc:
        out.append(f"About you: {agent_desc.identity}")
        out.append(f"Your goals for the conversation: {agent_desc.plan}")
    if other_agent_desc:
        out.append(f"About {other_player_name}: {other_agent_desc.identity}")
    return out


def _previous_messages_prompt(
    db: Database, world_id: str, player_id: str, other_player_id: str,
    player_name: str, other_player_name: str, conversation_id: str,
) -> List[LLMMessage]:
    msgs = db.list_messages(world_id, conversation_id)
    out: List[LLMMessage] = []
    for m in msgs:
        if m["author"] == player_id:
            author_name, recipient_name = player_name, other_player_name
        else:
            author_name, recipient_name = other_player_name, player_name
        out.append(LLMMessage(
            role="user",
            content=f"{author_name} to {recipient_name}: {m['text']}",
        ))
    return out


def _load_prompt_context(
    db: Database, world_id: str, conversation_id: str, player_id: str, other_player_id: str,
    player_descriptions: Dict[str, Any], agent_descriptions: Dict[str, Any],
):
    """从内存里的 descriptions map 拿到 name/identity/plan。"""
    pd = player_descriptions.get(player_id)
    od = player_descriptions.get(other_player_id)
    if pd is None or od is None:
        raise ValueError("Missing player description")
    player_name = pd.name
    other_player_name = od.name
    # agent_descriptions 以 agent_id 为 key，但我们这里只有 player_id；
    # 调用方需要确保传入的是 {player_id: AgentDescription} 视图
    agent_desc = agent_descriptions.get(player_id)
    other_agent_desc = agent_descriptions.get(other_player_id)
    return player_name, other_player_name, agent_desc, other_agent_desc


def start_conversation_message(
    llm: LLMClient,
    db: Database,
    memory_store: MemoryStore,
    world_id: str,
    conversation_id: str,
    player_id: str,
    other_player_id: str,
    player_descriptions: Dict[str, Any],
    agent_descriptions: Dict[str, Any],
    last_conversation: Optional[Dict[str, Any]] = None,
) -> str:
    """对话刚开始，生成第一条消息。"""
    (player_name, other_player_name, agent_desc, other_agent_desc) = _load_prompt_context(
        db, world_id, conversation_id, player_id, other_player_id,
        player_descriptions, agent_descriptions,
    )

    embedding = llm.fetch_embedding(f"{player_name} is talking to {other_player_name}").embedding
    memories = memory_store.search(player_id, embedding, n=3)

    # 找到「和对方之前那次对话」的记忆，如果有则提示 LLM 引用细节
    memory_with_other = next(
        (m for m in memories
         if m.data.get("type") == "conversation"
         and other_player_id in m.data.get("playerIds", [])),
        None,
    )

    prompt = [f"You are {player_name}, and you just started a conversation with {other_player_name}."]
    prompt.extend(_agent_prompts(other_player_name, agent_desc, other_agent_desc))
    if last_conversation:
        prev_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_conversation["created"] / 1000))
        now_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        prompt.append(
            f"Last time you chatted with {other_player_name} it was {prev_ts}. "
            f"It's now {now_ts}."
        )
    prompt.extend(_related_memories_prompt(memories))
    if memory_with_other:
        prompt.append(
            "Be sure to include some detail or question about a previous conversation in your greeting."
        )
    last_prompt = f"{player_name} to {other_player_name}:"
    prompt.append(last_prompt)

    result = llm.chat_completion(
        [LLMMessage(role="system", content="\n".join(prompt))],
        max_tokens=300,
        stop=_stop_words(other_player_name, player_name),
    )
    return _trim_prefix(result.content, last_prompt)


def continue_conversation_message(
    llm: LLMClient,
    db: Database,
    memory_store: MemoryStore,
    world_id: str,
    conversation_id: str,
    player_id: str,
    other_player_id: str,
    player_descriptions: Dict[str, Any],
    agent_descriptions: Dict[str, Any],
) -> str:
    (player_name, other_player_name, agent_desc, other_agent_desc) = _load_prompt_context(
        db, world_id, conversation_id, player_id, other_player_id,
        player_descriptions, agent_descriptions,
    )

    embedding = llm.fetch_embedding(f"What do you think about {other_player_name}?").embedding
    memories = memory_store.search(player_id, embedding, n=3)

    started_ts = int(time.time() * 1000)  # 简化：用当前时间近似
    prompt = [
        f"You are {player_name}, and you're currently in a conversation with {other_player_name}.",
        f"The conversation started at "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_ts / 1000))}. "
        f"It's now {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}.",
    ]
    prompt.extend(_agent_prompts(other_player_name, agent_desc, other_agent_desc))
    prompt.extend(_related_memories_prompt(memories))
    prompt.append(
        f"Below is the current chat history between you and {other_player_name}."
    )
    prompt.append(
        "DO NOT greet them again. Do NOT use the word \"Hey\" too often. "
        "Your response should be brief and within 200 characters."
    )

    llm_messages: List[LLMMessage] = [LLMMessage(role="system", content="\n".join(prompt))]
    llm_messages.extend(_previous_messages_prompt(
        db, world_id, player_id, other_player_id, player_name, other_player_name, conversation_id
    ))
    last_prompt = f"{player_name} to {other_player_name}:"
    llm_messages.append(LLMMessage(role="user", content=last_prompt))

    result = llm.chat_completion(
        llm_messages, max_tokens=300,
        stop=_stop_words(other_player_name, player_name),
    )
    return _trim_prefix(result.content, last_prompt)


def leave_conversation_message(
    llm: LLMClient,
    db: Database,
    world_id: str,
    conversation_id: str,
    player_id: str,
    other_player_id: str,
    player_descriptions: Dict[str, Any],
    agent_descriptions: Dict[str, Any],
) -> str:
    (player_name, other_player_name, agent_desc, other_agent_desc) = _load_prompt_context(
        db, world_id, conversation_id, player_id, other_player_id,
        player_descriptions, agent_descriptions,
    )

    prompt = [
        f"You are {player_name}, and you're currently in a conversation with {other_player_name}.",
        "You've decided to leave the question and would like to politely tell them "
        "you're leaving the conversation.",
    ]
    prompt.extend(_agent_prompts(other_player_name, agent_desc, other_agent_desc))
    prompt.append(f"Below is the current chat history between you and {other_player_name}.")
    prompt.append(
        "How would you like to tell them that you're leaving? "
        "Your response should be brief and within 200 characters."
    )

    llm_messages: List[LLMMessage] = [LLMMessage(role="system", content="\n".join(prompt))]
    llm_messages.extend(_previous_messages_prompt(
        db, world_id, player_id, other_player_id, player_name, other_player_name, conversation_id
    ))
    last_prompt = f"{player_name} to {other_player_name}:"
    llm_messages.append(LLMMessage(role="user", content=last_prompt))

    result = llm.chat_completion(
        llm_messages, max_tokens=300,
        stop=_stop_words(other_player_name, player_name),
    )
    return _trim_prefix(result.content, last_prompt)


def find_conversation_candidate(
    db: Database,
    world_id: str,
    now_ms: int,
    player_id: str,
    player_position: Any,
    other_free_players: List[Any],
) -> Optional[str]:
    """从空闲玩家里挑一个对话对象：跳过刚聊过的，按距离排序取最近的。"""
    candidates = []
    for other in other_free_players:
        other_id = other["id"]
        if other_id == player_id:
            continue
        last_together = db.last_participated_together(world_id, player_id, other_id)
        if last_together is not None and now_ms < last_together + PLAYER_CONVERSATION_COOLDOWN_MS:
            continue
        candidates.append(other)
    if not candidates:
        return None

    def dist(p):
        return ((p["position"]["x"] - player_position["x"]) ** 2 +
                (p["position"]["y"] - player_position["y"]) ** 2) ** 0.5

    candidates.sort(key=dist)
    return candidates[0]["id"]
