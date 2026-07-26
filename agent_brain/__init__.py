"""Agent 大脑：在后台线程跑 LLM 操作，把结果作为 input 回写给 engine。

对应原项目 convex/agent/ + convex/aiTown/agentOperations.ts：
- ``agent_do_something``    : 决定下一步（找人聊 / 活动 / 漫游）
- ``agent_generate_message``: 生成对话消息（start / continue / leave）
- ``agent_remember_conversation``: 把刚结束的对话总结成记忆
- ``reflect_on_memories``   : 在累积足够重要度后产生反思

这些函数都是阻塞式 LLM 调用，由 Game 通过线程池调度，完成后把
``finish_xxx`` input 推回 engine 的 input 队列。
"""
from .memory import MemoryStore, remember_conversation, reflect_on_memories
from .conversation_ops import (
    start_conversation_message,
    continue_conversation_message,
    leave_conversation_message,
    find_conversation_candidate,
)
from .agent_ops import AgentBrain, run_agent_operation

__all__ = [
    "MemoryStore", "remember_conversation", "reflect_on_memories",
    "start_conversation_message", "continue_conversation_message",
    "leave_conversation_message", "find_conversation_candidate",
    "AgentBrain", "run_agent_operation",
]
