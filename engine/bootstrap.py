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
from engine.chunk import Chunk
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
    # 绑定数据库到 chunk_manager，用于按需加载/卸载和增量保存
    world_map.chunk_manager.attach_db(db, config.world_id)

    # 尝试从 SQLite 恢复地图元数据和 chunks
    # 先保留 gentle.json 中的 seed chunk，再用 saved_map 覆盖元数据，
    # 最后用数据库中的 chunks 覆盖/补充。这样即使 chunks 表为空也不会丢失 seed chunk。
    saved_map = db.load_map(config.world_id)
    saved_chunks = db.load_chunks(config.world_id)
    if saved_map is not None:
        meta = WorldMap.from_dict(saved_map)
        world_map.tile_set_url = meta.tile_set_url
        world_map.tile_set_dim_x = meta.tile_set_dim_x
        world_map.tile_set_dim_y = meta.tile_set_dim_y
        world_map.tile_dim = meta.tile_dim
        # chunk-based 无限世界不使用固定边界，保留当前动态值
        world_map.chunk_manager.chunk_size = meta.chunk_manager.chunk_size
        world_map.chunk_manager.seed = meta.chunk_manager.seed
    for c in saved_chunks:
        if c["data"] is not None:
            chunk = Chunk.from_dict(c["data"])
            chunk.modified = c.get("modified", False)
            world_map.chunk_manager.chunks[(chunk.cx, chunk.cy)] = chunk
        elif c["summary"] is not None:
            # 只有摘要的已卸载 chunk，用于 ChunkGraph 长距离寻路
            world_map.chunk_manager.summaries[(c["cx"], c["cy"])] = c["summary"]

    # 尝试从 SQLite 恢复世界状态；没有就新建
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
