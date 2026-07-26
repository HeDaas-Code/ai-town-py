# AI Town (Python 版) 开发指南

本文档面向想要在本仓库上做二次开发、调试、扩展的工程师，基于 `/workspace/ai_town_py/` 下的实际源码撰写。所有命令、字段名、文件路径均与代码一致。如需了解整体架构（分层、引擎主循环、状态机、寻路、线程模型等），请先阅读 [architecture.md](./architecture.md)。

---

## 1. 环境准备

- **Python 3.10+**（代码大量使用 `from __future__ import annotations` 与 `X | None` 类型语法）。
- **pygame-ce**（推荐）或原生 **pygame**：

  ```bash
  pip install pygame-ce
  # 或者
  pip install pygame
  ```

- **无其他第三方依赖**：HTTP 请求使用标准库 `urllib`，数据库使用标准库 `sqlite3`，线程池使用标准库 `concurrent.futures`。`requirements.txt` 不存在正是因为唯一外部依赖就是 pygame。
- **可选：本地 LLM**。若想脱离 OpenAI 跑真实对话，可安装 [Ollama](https://ollama.ai)：

  ```bash
  ollama serve
  ollama pull llama3
  ollama pull mxbai-embed-large
  ```

  未配置任何 LLM 环境变量时，`config.get_llm_config()` 默认回退到 Ollama 本地端点（`http://127.0.0.1:11434`）。

---

## 2. 项目布局

完整的目录树与各模块职责见 [architecture.md](./architecture.md) 第 1 节。这里仅列出与日常开发最相关的入口：

| 路径 | 作用 |
| --- | --- |
| `main.py` | CLI 解析 + Pygame 主循环 + 输入处理 |
| `config.py` | `AppConfig` / `LLMConfig` + 全部引擎常量（移植自 `convex/constants.ts`） |
| `engine/game.py` | `Game`：tick / step 双循环、输入队列、操作调度 |
| `engine/bootstrap.py` | `build_game()`：装配 db / llm / brain / router |
| `agent_brain/` | LLM 操作调度器（4 worker 线程池） |
| `dialogue/router.py` | `DialogueRouter` + `AgentEndpoint` + `RoutedMessage` |
| `llm/client.py` / `llm/offline.py` | 在线 / 离线 LLM 客户端 |
| `db/db.py` | SQLite 封装（WAL + RLock，自动建表） |
| `renderer/renderer.py` | Pygame 渲染器 |
| `data/characters.py` | 8 角色 spritesheet + 5 默认人格 |
| `data/maps/gentle.json` | 默认地图 |
| `tools/convert_map.py` | 原项目 JS 地图 -> JSON 转换器 |

---

## 3. 运行与调试

### 3.1 Headless 模式（无窗口，适合 CI / 自动化）

```bash
cd /workspace/ai_town_py
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python main.py --headless --no-llm --reset
```

`SDL_VIDEODRIVER=dummy` 与 `SDL_AUDIODRIVER=dummy` 让 Pygame 在无显示、无声卡的环境下初始化。`--headless` 走 `main.py:_run_headless`，默认跑 10 秒后退出，每 0.5 秒打印一次 `players / agents / convs / pending_ops`。

### 3.2 窗口模式（本地调试）

```bash
python main.py --no-llm --reset
```

`--no-llm` 使用 `OfflineLLMClient` 的占位回复，无需 API Key 即可跑完整渲染与对话链路；`--reset` 在启动前清掉 `default` 世界的存档。

### 3.3 调试快捷键

| 按键 | 作用 |
| --- | --- |
| `F1` | 切换调试覆盖层（FPS / Players / Agents / Conversations / Pending inputs / Pending ops / Pos / Speed），实现见 `main.py:_draw_debug` |
| `F2` | 手动调 `game.save_step()` 落盘一次，控制台打印 `[main] saved` |

### 3.4 Verbose 日志

Python 默认会缓冲 stdout，CI 里可能导致日志延迟。设置环境变量关闭缓冲：

```bash
PYTHONUNBUFFERED=1 python main.py --no-llm --reset
```

---

## 4. 添加自定义 Agent

### 4.1 方式一：编辑预设人格列表

`data/characters.py` 的 `descriptions: List[CharacterDescription]` 是预设 agent 池。`engine/bootstrap.py:_create_default_agent` 按 `desc_index % len(descriptions)` 循环取人格，所以新增条目后启动时会被自动选用。每个 `CharacterDescription` 需要四个字段：

```python
CharacterDescription(
    name="Lucky",              # 显示名（也是 player.name）
    character="f1",            # spritesheet 名，必须是 f1..f8 之一
    identity=("Lucky is always happy and curious, ..."),  # 背景故事，喂给 LLM 的 "About you"
    plan="You want to hear all the gossip.",               # 目标，喂给 LLM 的 "Your goals"
)
```

`character` 字段必须对应 `data/characters.py:characters` 列表里已存在的 8 套 spritesheet 之一（`f1`..`f8`），否则渲染器找不到精灵表。

### 4.2 方式二：运行时动态创建

`engine/game.py` 注册了 `createAgent` input handler（见 `_create_agent`），可在运行时通过 input 队列动态加 agent：

```python
game.enqueue_input("createAgent", {"descriptionIndex": 2})
```

`descriptionIndex` 是 `data.characters.descriptions` 列表的下标，越界时会自动取模。handler 内部调 `Player.join` 让新 agent 在随机未阻挡网格点出生，并创建对应的 `Agent` 与 `AgentDescription`，同时置 `game.descriptions_modified = True` 让下一个 `save_step` 把新描述落盘。

---

## 5. 替换 LLM 后端

`config.py:get_llm_config()` 按优先级支持 4 种 provider，全部通过环境变量切换，无需改代码。

| Provider | 触发条件 | 关键环境变量 | 默认模型 |
| --- | --- | --- | --- |
| **Custom**（任意 OpenAI 兼容端点） | 设置了 `LLM_API_URL` | `LLM_API_URL`、`LLM_MODEL`、`LLM_EMBEDDING_MODEL`、`LLM_API_KEY`(可选)、`EMBEDDING_DIMENSION`(可选) | 由用户指定 |
| **OpenAI** | 未设 `LLM_API_URL` 但设了 `OPENAI_API_KEY` | `OPENAI_API_KEY`、`OPENAI_CHAT_MODEL`(可选)、`OPENAI_EMBEDDING_MODEL`(可选) | `gpt-4o-mini` / `text-embedding-ada-002` |
| **Ollama**（本地，默认回退） | 上面两者都未设置 | `OLLAMA_HOST`(可选)、`OLLAMA_MODEL`(可选)、`OLLAMA_EMBEDDING_MODEL`(可选) | `llama3` / `mxbai-embed-large` |
| **Offline**（占位回复） | 启动时传 `--no-llm` | 无 | 不联网 |

### 5.1 Custom 端点示例

```bash
export LLM_API_URL=https://your-endpoint.com
export LLM_MODEL=your-chat-model
export LLM_EMBEDDING_MODEL=your-embedding-model
export LLM_API_KEY=your-key    # 可选，端点不需要鉴权时不设
python main.py --reset
```

### 5.2 OpenAI 示例

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_CHAT_MODEL=gpt-4o-mini          # 可选
export OPENAI_EMBEDDING_MODEL=text-embedding-ada-002  # 可选
python main.py --reset
```

### 5.3 Ollama 示例（默认回退，无需任何环境变量）

```bash
ollama serve
python main.py --reset
```

### 5.4 Offline 模式

```bash
python main.py --no-llm --reset
```

`AppConfig.enable_llm=False` 时 `engine/bootstrap.py` 装配 `OfflineLLMClient`（`llm/offline.py`）。该客户端不联网：

- `chat_completion` 从 `OfflineLLMClient` 内置的 4 条变体里按 `random.Random(seed_text)` 选一条返回，其中包含 `"Hmm, that's a good point. Tell me more."`，所以对话内容是确定性的占位文本，仅供链路验证。
- `fetch_embedding` 用 `sha256` 把文本哈希成固定维度（默认 64 维）的伪向量。

embedding 维度默认值：OpenAI/Custom 为 `OPENAI_EMBEDDING_DIMENSION = 1536`，Ollama 为 `OLLAMA_EMBEDDING_DIMENSION = 1024`，可用 `EMBEDDING_DIMENSION` 环境变量覆盖。

---

## 6. 添加新地图

### 6.1 从原项目 JS 地图转换

`tools/convert_map.py` 把原 AI Town 的 ESM 风格 JS 地图（`export const bgtiles = ...`）转成 JSON。默认 `data/maps/gentle.json` 就是从原项目 `src/editor/maps/gentle.js` 转换而来的。

```bash
python tools/convert_map.py path/to/gentle.js data/maps/my_map.json
```

脚本默认参数：`argv[1]` 缺省为 `data/gentle.js`，`argv[2]` 缺省为 `data/maps/gentle.json`。转换流程：去 `export const` 前缀、行注释 `//` 转 `#`、对象字面量裸键加引号、删 `.length` 语句，然后 `exec` 出 `bgtiles / objmap / animatedsprites` 三个数组，最后计算 `mapwidth / mapheight` 并写 JSON。

### 6.2 直接手写 JSON

也可以直接按 `WorldMap.to_dict()` 的 schema（见 `engine/world_map.py`）写 JSON。`WorldMap.from_dict` 读取时需要的字段如下：

```python
{
  "tilesetpath": "assets/gentle-obj.png",  # 相对项目根的瓦片图路径
  "tiledim": 32,                            # 单 tile 像素尺寸
  "screenxtiles": 32,                       # 屏幕横向 tile 数
  "screenytiles": 32,                       # 屏幕纵向 tile 数
  "tilesetpxw": 1024,                       # 瓦片图总宽
  "tilesetpxh": 1024,                       # 瓦片图总高
  "mapwidth": 32,                           # 地图宽（tile 数）
  "mapheight": 32,                          # 地图高（tile 数）
  "bgtiles": [[[...], [...]], ...],         # 背景层：layer[x][y] -> tileIndex 或 -1
  "objmap": [[[...], [...]], ...],          # 对象/碰撞层：layer[x][y] -> tileIndex 或 -1（-1 表示无阻挡）
  "animatedsprites": [                      # 动画精灵列表
    {"x": 0, "y": 0, "w": 32, "h": 32, "layer": 0, "sheet": "campfire.json", "animation": "default"}
  ]
}
```

注意 `bgtiles` / `objmap` 的索引顺序是 `layer[x][y]`（x 为列方向，y 为行方向），与原项目一致；`objmap` 中 `!= -1` 的格子会被 `engine/movement.py:blocked` 视为阻挡。

### 6.3 启动时指定地图

```bash
python main.py --map data/maps/my_map.json --reset
```

`--map` 接受任意路径，缺省为 `config.DEFAULT_MAP_PATH`（`data/maps/gentle.json`）。

---

## 7. 添加新角色 Spritesheet

### 7.1 放置 PNG

把角色精灵图放到 `assets/` 目录下（参考现有 `32x32folk.png`）。

### 7.2 编写 spritesheet 描述

spritesheet 描述是一个 JSON 风格的 Python dict，包含 `frames` 与 `animations` 两个字段。格式见 `data/characters.py:_build_spritesheet`：

```python
{
    "frames": {
        "down":  {"frame": {"x": 0,  "y": 0, "w": 32, "h": 32},
                  "sourceSize": {"w": 32, "h": 32}, "spriteSourceSize": {"x": 0, "y": 0}},
        "down2": {"frame": {"x": 32, "y": 0, "w": 32, "h": 32}, ...},
        "down3": {"frame": {"x": 64, "y": 0, "w": 32, "h": 32}, ...},
        # left / right / up 同理，行偏移依次 +32
    },
    "meta": {"scale": "1"},
    "animations": {
        "down":  ["down",  "down2",  "down3"],
        "left":  ["left",  "left2",  "left3"],
        "right": ["right", "right2", "right3"],
        "up":    ["up",    "up2",    "up3"],
    },
}
```

约定：**每个角色 4 个方向（down / left / right / up）× 每方向 3 帧 = 12 帧**。渲染器（`renderer/renderer.py`）按 `player.facing` 映射到这 4 个动画名，移动中以 `ANIMATION_FPS=6` 循环播放，静止时显示首帧。

`_build_spritesheet(name)` 会优先解析 `data/spritesheets/{name}.ts`（原项目 PIXI 风格的 TS 文件，正则提取 `frame: {x, y, w, h}`），解析失败时回退到按 `_CHARACTER_OFFSETS` 几何推导坐标。所以新增角色时既可以放 TS 文件让代码自动解析，也可以直接构造 dict。

### 7.3 注册到 characters 列表

在 `data/characters.py:characters` 列表里追加一个 `Character(name="f9", texture_url=..., spritesheet=..., speed=0.1)`，然后在 `descriptions` 里把某个 `CharacterDescription.character` 指向 `"f9"` 即可使用。

---

## 8. 扩展对话路由

`dialogue/router.py` 是显式抽出的消息转发层。要接入自定义消息处理：

### 8.1 实现 AgentEndpoint

```python
from dialogue import AgentEndpoint, RoutedMessage

def my_handler(msg: RoutedMessage) -> None:
    # msg.conversation_id / msg.author / msg.recipient / msg.text / msg.message_uuid / msg.timestamp
    print(f"[custom] {msg.author} -> {msg.recipient}: {msg.text}")

endpoint = AgentEndpoint(
    agent_id="my-agent-1",
    player_id="p:42",
    name="MyAgent",
    handler=my_handler,
    # 可选生命周期回调
    on_conversation_started=lambda conv_id, a, b: print("started", conv_id),
    on_conversation_ended=lambda conv_id: print("ended", conv_id),
)
```

### 8.2 注册到路由

```python
game.dialogue_router.register(endpoint)
```

注册后，任何 `router.deliver(RoutedMessage(recipient="p:42", ...))` 都会同步调用 `my_handler`（在路由线程内执行，异常被吞掉不阻塞主流程）。

### 8.3 添加 UI 观察者

观察者会收到**每一条**路由消息（不分收件人），适合做 UI 气泡、调试日志、统计：

```python
game.dialogue_router.add_observer(lambda msg: print(f"[obs] {msg.author}: {msg.text}"))
```

`main.py` 就是用这种方式把 `renderer.on_routed_message` 注册成 observer，从而实时画气泡。

### 8.4 人类玩家接入参考

人类玩家的注册流程见 `main.py:register_human_to_router`：它构造一个 `AgentEndpoint`，`agent_id` 形如 `human-{player_id}`，handler 是空函数（因为渲染器已经通过 observer 显示气泡，agent 的回复会在下一 tick 自动触发）。需要发起对话时调 `game.enqueue_input("startConversation", {"playerId": ..., "invitee": ...})`。

---

## 9. 数据库检查

默认数据库文件是项目根的 `ai_town.db`（可用 `--db PATH` 覆盖）。直接用 `sqlite3` CLI 检查：

```bash
sqlite3 ai_town.db
```

### 9.1 常用查询

```sql
-- 列出所有表
.tables

-- 消息总数（验证对话是否真的发生）
SELECT COUNT(*) FROM messages;

-- 所有玩家描述（身份 / 角色 / 名字）
SELECT * FROM player_descriptions;

-- 所有 agent 人格
SELECT * FROM agent_descriptions;

-- 最近 10 条对话消息
SELECT id, conversation_id, author, text, created_at
FROM messages
ORDER BY id DESC
LIMIT 10;

-- 某个世界的归档对话
SELECT id, creator, ended, num_messages, participants
FROM archived_conversations
WHERE world_id = 'default';
```

完整表结构见 `db/db.py` 顶部的 `SCHEMA` 字符串与 [architecture.md](./architecture.md) 第 9 节。

### 9.2 WAL 模式文件

数据库启用 WAL（Write-Ahead Logging）模式以提升并发读。运行时除了 `ai_town.db`，还会出现 `ai_town.db-wal` 与 `ai_town.db-shm` 两个文件，这是 SQLite WAL 的正常副产物，**不要手动删除**。进程正常退出后 WAL 会被 checkpoint 回主库；若强行 kill，下次打开时 SQLite 会自动恢复。

---

## 10. 测试

### 10.1 Headless 冒烟测试

```bash
cd /workspace/ai_town_py
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python main.py --headless --no-llm --reset
```

`main.py:_run_headless` 默认跑 **10 秒**（`end = time.time() + 10`），每 0.5 秒打印一次 `players / agents / convs / pending_ops`。README 中「引擎可稳定运行 100 秒以上」是更长时间的稳定性验证结论——若要跑 100 秒，需把 `_run_headless` 里的 `+ 10` 改成 `+ 100`，或在外层用 `timeout` 包住并多次重启。

### 10.2 验证清单

跑完冒烟测试后应确认：

- **无崩溃**：进程正常退出，无 Python traceback。
- **conversations > 0**：headless 日志里 `convs=` 至少出现过非零值（离线模式下 agent 也会走完邀请 / 接近 / 对话流程）。
- **messages > 0**：`sqlite3 ai_town.db "SELECT COUNT(*) FROM messages;"` 返回 > 0，说明 `DialogueRouter.deliver` 落盘链路正常。
- **agents 移动**：日志里 `pending_ops` 有变化，说明 `Agent.tick` 在持续调度 `agentDoSomething` 操作。

### 10.3 渲染器测试

在 dummy SDL 下跑窗口模式（不加 `--headless`）可验证渲染管线：

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python main.py --no-llm --reset
```

由于无显示设备，主循环会持续渲染但不显示窗口。验证目标：**300+ 帧无错误**（README 已验证）。若需自动停止，可在 `main.py:run` 的主循环里加帧数计数后 `break`，或直接用 `--headless` 跑引擎层验证。

---

## 11. 常见问题

**`ModuleNotFoundError: No module named 'pygame'`**

```bash
pip install pygame-ce
```

或 `pip install pygame`。两者 API 兼容，pygame-ce 是社区维护的更新分支。

**`pygame.error: xdg-runtime-dir not created` / XDG_RUNTIME_DIR invalid**

无显示设备的环境下 Pygame 无法初始化视频驱动。headless 必须加：

```bash
SDL_VIDEODRIVER=dummy python main.py --headless ...
```

**ALSA 相关报错（`ALSA lib ...` / `Unable to open audio device`）**

无声卡环境同理，禁用音频驱动：

```bash
SDL_AUDIODRIVER=dummy python main.py ...
```

**`sqlite3.OperationalError: database is locked`**

SQLite 单连接 + WAL + `RLock` 已尽量规避锁冲突，但同一 `ai_town.db` 同时被两个**写进程**打开仍会冲突。规则：**同一时间只允许一个进程写**。调试时若要用 `sqlite3` CLI 查询，请用只读模式 `sqlite3 -readonly ai_town.db`，或先停掉游戏进程。

**`LLMError: HTTP 401 Unauthorized` / `LLM request failed (401)`**

API Key 无效或未设置。检查：

- OpenAI：`echo $OPENAI_API_KEY` 是否非空且有效。
- Custom：`LLM_API_KEY` 是否符合端点要求（端点不需要鉴权时可不设）。
- Ollama 本地无需 Key，若 401 多半是 `OLLAMA_HOST` 指错了地址。

**Agent 之间不对话**

离线模式下也需要时间。预期时间线：

- 启动后约 **60 秒**（`INVITE_TIMEOUT_MS = 60_000` 量级）才会出现首次对话活动——agent 需要先 `agentDoSomething` 决策、寻路接近、发出邀请、对方接受、双方走过去。
- 对话结束后有 **10 秒**活动冷却（`ACTIVITY_COOLDOWN_MS = 10_000`）才会重新漫游。
- 同一对玩家两次对话之间有 **60 秒**冷却（`PLAYER_CONVERSATION_COOLDOWN_MS = 60_000`），所以同一对 agent 不会立刻再聊。
- 综上，约 **70 秒以上**才会看到稳定的对话活动。若 100 秒后 `messages` 表仍为 0，检查 `pending_ops` 是否在增长、brain 线程是否报错。

**窗口太大 / 太小**

```bash
python main.py --width 1280 --height 720
```

`--width` / `--height` 默认 1024 / 768，窗口可拖拽缩放（`pygame.RESIZABLE`）。

---

## 12. 代码风格

- **PEP 8** + **类型注解**。所有模块顶部写 `from __future__ import annotations`，函数签名用 `str | None`、`List[X]`、`Dict[str, Any]` 等类型。
- **模块级 docstring** 说明本文件对应原 TypeScript 项目的哪个文件，方便对照原项目。例如 `engine/world_map.py` 顶部写「对应原项目 data/gentle.js 地图文件」，`config.py` 写「常量值移植自原项目 convex/constants.ts」。
- **常量集中放 `config.py`**。引擎节拍、超时、冷却、概率、embedding 维度等全部在 `config.py` 顶部，不要在业务模块里写魔法数字。新增常量时请同步更新 [architecture.md](./architecture.md) 附录的「关键常量速查」表。
- **不引入 pygame 以外的外部依赖**。HTTP 用 `urllib.request`，JSON 用 `json`，数据库用 `sqlite3`，线程池用 `concurrent.futures`，哈希用 `hashlib`。新增功能若需要联网 / 序列化 / 数据库，优先用标准库。
- **异常处理**：跨线程回调（observer / handler）里的异常应被吞掉并打印日志，不能影响路由或引擎主流程，参考 `dialogue/router.py:deliver` 的 try/except 模式。
