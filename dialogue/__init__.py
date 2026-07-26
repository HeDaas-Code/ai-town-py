"""对话路由包：所有 agent 在此注册，消息按 conversation 在此转发。"""
from .router import AgentEndpoint, DialogueRouter, MessageBus, RoutedMessage

__all__ = ["DialogueRouter", "MessageBus", "AgentEndpoint", "RoutedMessage"]
