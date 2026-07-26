"""Game 启动辅助：构建 world、注册 agent、注入 brain / router。

把组装流程从 Game 类里分离出来，便于测试与不同入口（CLI / 测试 / GUI）复用。
"""
from __future__ import annotations

import time
from typing import Optional

from agent_brain import AgentBrain, MemoryStore
from config import AppConfig
from db import Database
from dialogue import AgentEndpoint, DialogueRouter
from engine.agent import Agent
from engine.game import Game
from engine.player import Player
from engine.player_description import AgentDescription
from engine.world import World
from engine.world_map import WorldMap
from llm import LLMClient, OfflineLLMClient


def build_game(
    config: AppConfig,
    db: Optional[Database] = None,
    llm: Optional[LLMClient] = None,
) -> Game:
    """按 config 装配一个完整可运行的 Game。"""
    db = db or Database(config.db_path)
    world_map = WorldMap.load(config.map_path)

    # 尝试从 SQLite 恢复；没有就新建
    state = db.load_world(config.world_id)
    if state is not None:
        world = World.from_dict(state["world"])
        player_descs = {
            p["playerId"]: _player_desc_from_dict(p)
            for p in state.get("playerDescriptions", [])
        }
        agent_descs = {
            a["agentId"]: _agent_desc_from_dict(a)
            for a in state.get("agentDescriptions", [])
        }
    else:
        world = World()
        player_descs = {}
        agent_descs = {}

    # 对话路由
    router = DialogueRouter(db, config.world_id)

    game = Game(
        config=config, db=db, world_map=world_map, world=world, dialogue_router=router,
    )
    game.player_descriptions = player_descs
    game.agent_descriptions = agent_descs

    # LLM 客户端
    if llm is None:
        llm = OfflineLLMClient() if not config.enable_llm else LLMClient()
    memory_store = MemoryStore(db, config.world_id)
    brain = AgentBrain(
        llm=llm, db=db, world_id=config.world_id,
        dialogue_router=router, memory_store=memory_store,
        on_finish=lambda name, args: game.enqueue_input(name, args),
    )
    game.attach_brain(brain)

    # 首次启动：创建 N 个 agent
    if state is None:
        now_ms = int(time.time() * 1000)
        for i in range(config.num_agents):
            _create_default_agent(game, now_ms, i)

    # 把所有现存 agent 注册到对话路由
    _register_agents_to_router(game)

    return game


def _create_default_agent(game: Game, now_ms: int, desc_index: int) -> None:
    """创建一个预设 agent（角色从 data.characters.descriptions 取）。"""
    from data.characters import descriptions
    if desc_index >= len(descriptions):
        desc_index = desc_index % len(descriptions)
    desc = descriptions[desc_index]
    player_id = Player.join(game, now_ms, desc.name, desc.character, desc.identity)
    agent_id = game.alloc_id("agents")
    game.world.agents[agent_id] = Agent(id=agent_id, player_id=player_id)
    game.agent_descriptions[agent_id] = AgentDescription(
        agent_id=agent_id, identity=desc.identity, plan=desc.plan
    )
    game.descriptions_modified = True


def _register_agents_to_router(game: Game) -> None:
    """把每个 agent 注册到 DialogueRouter，并附带 handler。

    handler 收到「对方发给我的消息」时，目前只做日志（agent 在下一 tick
    里会通过 conversation.last_message 自动感知到，从而触发回复）。
    """
    for agent in game.world.agents.values():
        player = game.world.players.get(agent.player_id)
        if player is None:
            continue
        pdesc = game.player_descriptions.get(agent.player_id)
        name = pdesc.name if pdesc else agent.id

        def make_handler(aid=agent.id, pid=agent.player_id):
            def handler(msg):
                # agent 在下一个 tick 里通过 conversation.last_message 看到这条消息，
                # 这里只做日志 / 调试输出
                print(f"[router] {aid} ({pid}) received from {msg.author}: {msg.text[:40]}")
            return handler

        game.dialogue_router.register(AgentEndpoint(
            agent_id=agent.id,
            player_id=agent.player_id,
            name=name,
            handler=make_handler(),
        ))


def _player_desc_from_dict(d):
    from engine.player_description import PlayerDescription
    return PlayerDescription.from_dict(d)


def _agent_desc_from_dict(d):
    from engine.player_description import AgentDescription
    return AgentDescription.from_dict(d)
