# AI Town (Python 版)

AI Town 是 a16z / Convex 团队开源的一个「生成式 agent 小镇」沙盒演示：若干带个性的 agent 在一张 RPG 风格的地图里自主漫游、做活动、寻找对话对象、走过去发起对话、交换消息、再回头把对话写进自己的长期记忆。本项目是 AI Town 的忠实 Python 重写版：保留原项目的世界观、地图资源、角色 spritesheet 与人格设定，但把整套运行栈从 TypeScript + React/PixiJS + Convex 后端替换为自研 Python 引擎、Pygame 渲染器、OpenAI 兼容 LLM 客户端和 SQLite 持久化后端，不依赖任何 Convex / Node 服务即可在本地一键跑起来。

## 功能特性

- 自研 tick / step 双层模拟引擎（`engine/game.py`），单线程驱动世界状态、输入队列与操作调度，与原项目 `STEP_DURATION_MS = 1000` 等核心常量对齐。
- 自研有限状态机框架（`engine/state_machine.py`），承载玩家移动、对话成员、agent 行为等多种状态机。
- 自研 A\* 寻路（`engine/movement.py` + `engine/geometry.py` + `engine/minheap.py`），不引入任何第三方寻路库。
- 自研 Pygame 渲染器（`renderer/renderer.py`）：瓦片地图、角色帧动画、5 种精灵动画（campfire / gentlesparkle / gentlewaterfall / windmill / gentlesplash）、对话气泡、相机跟随。
- OpenAI 兼容 LLM 客户端（`llm/client.py`），支持 OpenAI / Ollama / Together / 任意 OpenAI 协议端点，内置重试与停止词。
- SQLite 数据后端（`db/db.py`），WAL 模式 + `RLock` 保证多线程安全，世界可在重启后从存档恢复。
- 独立的「对话路由」模块（`dialogue/router.py`），所有 agent 必须注册到此路由，消息先落盘再派发，并支持观察者（渲染器借此画气泡）。
- `--no-llm` 离线模式（`llm/offline.py`）提供确定性占位回复，方便无网络 / 无 API Key 的环境跑通整条链路。
- `--headless` 无窗口模式，可在 CI 中用 dummy SDL 驱动跑冒烟测试。
- 复用原项目全部美术资源：`32x32folk.png`（角色）、`rpg-tileset.png` + `tilemap.json`（地图）、`magecity.png`、`gentle-obj.png`、5 套动画 spritesheet、字体与背景音乐。
- 8 套角色 spritesheet（f1..f8）与 5 个默认人格（Lucky / Bob / Stella / Alice / Pete）直接从原项目 `data/spritesheets/*.ts` 与 `data/characters.ts` 迁移。

<!-- 在此处放置项目截图或动画 GIF -->

## 快速开始

要求 Python 3.10+ 与 `pygame-ce 2.5+`（或原生 `pygame`），无其他第三方依赖（HTTP 用标准库 `urllib`，数据库用标准库 `sqlite3`）。

```bash
cd /workspace/ai_town_py
pip install pygame-ce
```

无窗口冒烟测试（不联网、不调 LLM、不开窗口，适合 CI）：

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python main.py --headless --no-llm --reset --num-agents 3
```

带 Pygame 窗口 + 离线占位 LLM（无需 API Key）：

```bash
python main.py --no-llm --reset
```

使用真实 OpenAI：

```bash
export OPENAI_API_KEY=sk-...
python main.py --reset
```

使用自定义 OpenAI 兼容端点（Together / 自建 / 兼容代理）：

```bash
export LLM_API_URL=https://your-endpoint.com
export LLM_MODEL=your-chat-model
export LLM_EMBEDDING_MODEL=your-embedding-model
export LLM_API_KEY=your-key  # 可选
python main.py --reset
```

使用本地 Ollama（未配置任何 LLM 环境变量时的默认回退）：

```bash
ollama serve
# 另开一个终端
python main.py --reset
```

## 架构

```text
ai_town_py/
├── main.py                # 入口：CLI 解析 + Pygame 主循环 + 输入处理
├── config.py              # AppConfig / LLMConfig + 引擎常量（移植自 convex/constants.ts）
│
├── engine/                # 自研游戏引擎（不依赖任何引擎库）
│   ├── game.py            #   Game：tick / step 双循环、输入队列、操作调度
│   ├── bootstrap.py       #   build_game()：装配 db / llm / brain / router
│   ├── world.py           #   World：玩家、agent、对话、活动等世界状态
│   ├── world_map.py       #   WorldMap：bgTiles / objectTiles / animatedSprites
│   ├── player.py          #   Player 状态机（移动、对话成员）
│   ├── agent.py           #   Agent 状态机（自主行为）
│   ├── conversation.py    #   Conversation 状态机
│   ├── movement.py        #   A* 寻路 + move_player / stop_player
│   ├── geometry.py        #   Point / distance / 几何工具
│   ├── minheap.py         #   最小堆（A* 开放表用）
│   ├── state_machine.py   #   通用 FSM 框架
│   ├── player_description.py
│   ├── types.py
│   └── ids.py             #   GameId 类型
│
├── agent_brain/           # LLM 操作调度器（后台线程池）
│   ├── agent_ops.py       #   AgentBrain：4 worker ThreadPoolExecutor
│   ├── conversation_ops.py#   开场 / 续聊 / 离场消息生成
│   └── memory.py          #   MemoryStore + Generative Agents 风格的 reflect
│
├── dialogue/
│   └── router.py          # DialogueRouter + MessageBus + AgentEndpoint + RoutedMessage
│
├── llm/
│   ├── client.py          # LLMClient：chat_completion / fetch_embedding，含重试
│   └── offline.py         # OfflineLLMClient：确定性占位回复（--no-llm）
│
├── db/
│   └── db.py              # SQLite 封装：WAL + RLock，自动建表
│
├── renderer/
│   └── renderer.py        # Pygame 渲染器：Camera / Tileset / Spritesheet
│
├── data/
│   ├── maps/gentle.json   # 默认地图
│   ├── spritesheets/      # 原项目 f1.ts ... f8.ts（运行时解析）
│   └── characters.py      # 8 角色 spritesheet + 5 默认人格
│
├── assets/                # 复用原项目的全部美术资源
│   ├── fonts/  spritesheets/  32x32folk.png  rpg-tileset.png  tilemap.json
│   ├── magecity.png  gentle-obj.png  player.png  heart-empty.png
│   └── background.mp3
│
├── tools/
│   └── convert_map.py     # 原项目 JS 地图 -> JSON 转换器
│
└── ai_town.db             # 默认 SQLite 数据库（运行时生成）
```

## 对话路由

`dialogue/router.py` 是本项目相对原项目最显式的一层抽象。原 AI Town 里 agent 之间并不直接互发消息：A 想对 B 说话时，A 把消息写进 `messages` 表 + 触发一次 `agentFinishSendingMessage`，B 在下一 tick 通过 `previousMessages` 拉取历史再生成回复——这层「转发」由 Convex 的 mutation / query 系统隐式承担。本项目把这一层抽出来做成显式的 `DialogueRouter`：

1. **注册**：所有 agent（以及人类玩家）启动时必须调 `router.register(AgentEndpoint(...))`，提交自己的 `player_id`、`name` 与消息 `handler`。
2. **派发**：A 调 `router.deliver(RoutedMessage(...))` 时，路由按顺序做三件事：
   - `MessageBus.persist` 先把消息落盘到 SQLite 的 `messages` 表；
   - 同步调用接收方 `AgentEndpoint.handler`（在路由线程内执行）；
   - 通知所有全局观察者（`add_observer` 注册，渲染器借此实时画气泡）。
3. **生命周期**：对话开始 / 结束事件由 `on_conversation_started` / `on_conversation_ended` 广播给两端 endpoint 的可选回调。
4. **线程安全**：路由被引擎主线程与 `agent_brain` 后台线程共同访问，内部用 `threading.RLock` 保护 endpoints / conversations / observers 三张表。

这样 `agent_brain` 只需要一行 `router.deliver(conversation_id, from=alice, to=bob, text=...)` 就能完成「A 的话转发给 B」，无需关心 B 当前在哪个线程、handler 是否已就绪。已验证：开启 LLM 跑 100 秒以上，`messages` 表行数稳定增长 > 0，渲染器作为观察者也能即时画出气泡。

## LLM 配置

LLM 配置由 `config.py` 中的 `get_llm_config()` 从环境变量解析，按优先级支持 4 种 provider：

| Provider | 触发条件 | 关键环境变量 | 默认模型 |
| --- | --- | --- | --- |
| **Custom**（任意 OpenAI 兼容端点） | 设置了 `LLM_API_URL` | `LLM_API_URL`、`LLM_MODEL`、`LLM_EMBEDDING_MODEL`、`LLM_API_KEY`(可选)、`EMBEDDING_DIMENSION`(可选) | 由用户指定 |
| **OpenAI** | 未设置 `LLM_API_URL` 但设置了 `OPENAI_API_KEY` | `OPENAI_API_KEY`、`OPENAI_CHAT_MODEL`(可选)、`OPENAI_EMBEDDING_MODEL`(可选) | `gpt-4o-mini` / `text-embedding-ada-002` |
| **Ollama**（本地，默认回退） | 上面两者都未设置 | `OLLAMA_HOST`(可选)、`OLLAMA_MODEL`(可选)、`OLLAMA_EMBEDDING_MODEL`(可选) | `llama3` / `mxbai-embed-large` |
| **Offline**（占位回复） | 启动时传 `--no-llm` | 无 | 不联网，由 `llm/offline.py` 返回确定性回复 |

Custom 端点的 embedding 维度默认按 OpenAI 的 1536 处理，Ollama 默认按 1024 处理，可用 `EMBEDDING_DIMENSION` 环境变量覆盖。`LLMClient` 内置指数退避重试，Ollama 的回复会按 `<|eot_id|>` 等停止词截断。

## 控制方式

| 操作 | 按键 |
| --- | --- |
| 移动 | 方向键 / WASD（按住时每 250ms 寻路到相邻 tile） |
| 寻路到点击位置 | 鼠标左键点击地图 |
| 与最近 NPC 发起对话 | 空格（要求距离 < 2.5 tile） |
| 进入文字输入模式 | 对话中按 Enter |
| 发送消息 | 输入模式下按 Enter |
| 取消输入 | 输入模式下按 Esc |
| 离开当前对话 | 对话中（非输入态）按 Esc |
| 退出程序 | 空闲态按 Esc |
| 切换调试覆盖层（FPS / 玩家 / agent / 对话数 / 玩家位置） | F1 |
| 手动保存世界 | F2 |

## CLI 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--map PATH` | `data/maps/gentle.json` | 地图 JSON 路径 |
| `--db PATH` | `ai_town.db` | SQLite 数据库路径 |
| `--num-agents N` | `5` | 初始 agent 数量 |
| `--world-id STR` | `default` | 世界 ID，不同 ID 之间相互独立 |
| `--width` / `--height` | `1024` / `768` | 窗口尺寸 |
| `--fps N` | `60` | 目标 FPS |
| `--headless` | 关 | 无窗口模式（CI / 自动化测试） |
| `--no-llm` | 关 | 使用占位 LLM 回复（离线演示） |
| `--reset` | 关 | 启动前清掉该 `world_id` 的存档 |

## 数据持久化

所有世界状态由 `db/db.py` 写入 SQLite，启用 WAL 模式以提升并发读、避免写阻塞读，并配 `threading.RLock` 让引擎主线程与 `agent_brain` 后台线程共用同一连接。引擎每个 step 结束时调 `save_world` / `save_diff` 落盘，重启时由 `load_world` 恢复。涉及的数据表（schema 定义在 `db/db.py` 顶部 `SCHEMA` 字符串）：

| 表 | 用途 | 对应原项目 Convex 表 |
| --- | --- | --- |
| `worlds` | 世界快照（id / state_json / updated_at） | `worlds` |
| `player_descriptions` | 玩家描述（playerId / character / description / name） | `playerDescriptions` |
| `agent_descriptions` | agent 描述（agentId / identity / plan） | `agentDescriptions` |
| `maps` | 世界地图（worldId / map_json） | `maps` |
| `messages` | 对话消息（conversationId / author / text / messageUuid / createdAt） | `messages` |
| `memories` | 长期记忆（playerId / description / importance / lastAccess / data_json） | `memories` |
| `memory_embeddings` | 记忆向量（playerId / embedding blob） | `memoryEmbeddings` |
| `archived_conversations` | 归档对话（creator / ended / last_message / num_messages / participants） | `archivedConversations` |
| `archived_players` | 归档玩家（player_json） | `archivedPlayers` |
| `participated_together` | 「谁和谁聊过」索引（player1 / player2 / ended），带两个索引 | `participatedTogether` |
| `world_engine_state` | 引擎推进状态（last_engine_ts / generation） | （引擎恢复用） |

`messages`、`memories`、`participated_together` 上建有索引，便于按对话、按玩家快速查询历史。

## 原项目对照

本项目按原 a16z AI Town 的 TypeScript 模块一一对应重写。主要映射关系：

| 本项目 (Python) | 原项目 (TypeScript) | 说明 |
| --- | --- | --- |
| `config.py` | `convex/constants.ts` | 引擎常量（`TICK_DURATION_MS` / `STEP_DURATION_MS` 等）+ LLM 配置 |
| `main.py` | `convex/aiTown/main.ts` + `src/App.tsx` | 入口 + 主循环 + 输入 |
| `engine/game.py` | `convex/aiTown/game.ts` + `convex/engine/abstractGame.ts` | tick / step 引擎与操作调度 |
| `engine/world.py` | `convex/aiTown/world.ts` | 世界状态 |
| `engine/world_map.py` | `convex/aiTown/worldMap.ts` | 瓦片地图 |
| `engine/player.py` | `convex/aiTown/player.ts` | 玩家状态机 |
| `engine/agent.py` | `convex/aiTown/agent.ts` | agent 状态机 |
| `engine/conversation.py` | `convex/aiTown/conversation.ts` + `conversationMembership.ts` | 对话与成员 |
| `engine/movement.py` | `convex/aiTown/movement.ts` | 移动与寻路 |
| `engine/geometry.py` | `convex/util/geometry.ts` | 几何工具 |
| `engine/minheap.py` | `convex/util/minheap.ts` | 最小堆 |
| `engine/state_machine.py` | `convex/engine/abstractGame.ts`（FSM 部分） | 通用状态机框架 |
| `engine/ids.py` | `convex/aiTown/ids.ts` | GameId 类型 |
| `engine/player_description.py` | `convex/aiTown/playerDescription.ts` | 玩家描述 |
| `agent_brain/agent_ops.py` | `convex/aiTown/agentOperations.ts` + `agentInputs.ts` | agent 操作调度 |
| `agent_brain/conversation_ops.py` | `convex/agent/conversation.ts` | 开场 / 续聊 / 离场消息 |
| `agent_brain/memory.py` | `convex/agent/memory.ts` | 记忆 + Generative Agents 风格反思 |
| `dialogue/router.py` | （原项目隐式由 Convex mutation/query 承担） | 本项目显式抽出的对话路由层 |
| `llm/client.py` | `convex/util/llm.ts` | OpenAI 兼容客户端 |
| `llm/offline.py` | （原项目无对应） | 离线占位回复 |
| `db/db.py` + SCHEMA | `convex/schema.ts` + `convex/agent/schema.ts` | 数据库与表结构 |
| `renderer/renderer.py` | `src/components/PixiGame.tsx` + `PixiStaticMap.tsx` + `PixiViewport.tsx` + `Character.tsx` | 渲染器（PixiJS -> Pygame） |
| `data/characters.py` | `data/characters.ts` | 8 角色 + 5 人格 |
| `data/spritesheets/*.ts` | `data/spritesheets/*.ts` | 直接复用，运行时解析 |
| `assets/*` | `public/assets/*` | 直接复用美术资源 |
| `tools/convert_map.py` | `data/convertMap.js` | JS 地图转 JSON |

原项目的 Convex 后端（mutation / query / cron / 实时订阅）在本项目被替换为「单进程引擎 + SQLite + 对话路由 + 后台线程池」组合，去掉了 Convex 运行时依赖。

## 开发 / 测试

无窗口冒烟测试（推荐在 CI 中跑，无需 LLM、无需显示设备）：

```bash
cd /workspace/ai_town_py
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python main.py --headless --no-llm --reset --num-agents 3
```

该命令会在 dummy SDL 驱动下跑 10 秒，每 0.5 秒打印一次玩家 / agent / 对话数 / pending operations。已验证：

- 引擎可稳定运行 100 秒以上，5 个 agent；
- agent 自主漫游、做活动、寻找对话候选、走过去、发起对话、交换消息、离开、记入记忆；
- 消息通过 `DialogueRouter` 路由并落盘到 SQLite，`messages` 表行数 > 0；
- 渲染器在 dummy SDL 模式下可无错渲染 300+ 帧。

开发调试可加 `--headless` 跑数据通路，或加 `--no-llm` 跑完整渲染但用占位回复；接入真实 LLM 前先用离线模式确认行为符合预期。

## 已知限制

- 渲染器目前基于 Pygame 软件绘制，未做 GPU 加速；超大地图或高分辨率窗口下可能掉帧。
- `agent_brain` 固定 4 worker 线程池，未做动态扩缩；大量 agent 同时说话时 LLM 调用可能排队。
- 仅支持二维瓦片地图与单房间对话（二人对话），不实现原项目的多人同房间对话扩展。
- 没有联网多人同步能力，所有玩家状态都在单进程内；原项目的 Convex 实时订阅在本项目不存在。
- 未实现原项目的语音 / 浏览器嵌入；人类玩家交互仅限本机 Pygame 窗口。
- 离线 LLM（`llm/offline.py`）只返回确定性占位文本，不产生有意义的对话内容，仅供链路验证。
- 资源文件沿用原项目 LICENSE，重新分发时需同时遵守原项目资源授权。

## 许可证

本项目代码采用 MIT 许可证发布。`assets/` 目录下的美术资源（图片、字体、音乐、spritesheet JSON）来自原 a16z AI Town 项目，沿用原项目的资源授权，重新分发时请同时遵守原项目对资源的规定。

## 致谢

本项目基于 a16z / Convex 团队开源的 [AI Town](https://github.com/a16z-infra/ai-town) 重写。感谢原项目作者分享的生成式 agent 设计、Generative Agents 风格的记忆与反思机制、以及完整的 RPG 风格美术资源。原项目的世界观、地图、角色 spritesheet 与默认人格（Lucky / Bob / Stella / Alice / Pete）均被本项目直接复用。本项目不附属于 a16z 或 Convex，仅为社区驱动的 Python 学习 / 演示实现。
