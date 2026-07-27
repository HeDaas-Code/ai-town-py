# TUI 封闭小镇 — 设计文档

## 概述

在 `ai_town_py` 项目中新建 `tui-town` 分支，构建一个基于 textual 的终端 UI（TUI）版本的 AI 小镇。

与传统版本的核心区别：
- **封闭小镇**：不接收任何外界输入，所有 player 都是 NPC（agent），用户只能观察
- **TUI 界面**：用 textual 框架渲染终端 UI，三栏布局 + 底部命令栏
- **视角切换**：用户通过编号或名字跳转视角，地图跟随选中 agent

## 目标

- agent 在封闭世界中自主生活：漫游、对话、做活动、形成记忆
- 用户通过 TUI 观察 agent 的状态、目标、今日活动和记忆
- 复用现有引擎层（`engine/`）、大脑层（`agent_brain/`）、持久化层（`db/` + `dialogue/`），仅替换渲染层

## 架构

### 整体布局

```
┌─────────────────────────────────────────────────┐
│  AI Town TUI — 封闭小镇观察台                    │
├──────────────┬────────────────────────┬─────────┤
│  Agent 列表   │  地图视图（俯视）        │ 详情面板│
│              │                        │         │
│ > 1 Alice    │  ......................│ 状态:   │
│   2 Bob      │  .....#####............│ 漫游中  │
│   3 Carol    │  ....#####~~...........│         │
│   4 Dave     │  ...R######~~...@......│ 目标:   │
│   5 Eve      │  ...RR#####~~.........│ 去(5,-3)│
│              │  ....#####............│ 距离3.2 │
│              │                        │         │
│              │  @=选中  o=其他NPC     │ 今日:   │
│              │  .=草地 #=山 ~水 R=路  │ 09:30   │
│              │  □=建筑               │ 和Bob聊 │
├──────────────┴────────────────────────┴─────────┤
│ > 输入命令: 2  或  :jump Alice  或  :help        │
└─────────────────────────────────────────────────┘
```

### 线程模型

引擎在独立 daemon 线程跑 `Game.run_forever`（tick 16ms / step 1000ms）。
TUI 在主线程跑 textual 的 asyncio 事件循环。
两者通过以下机制通信：

1. **只读访问**：TUI 直接读 `game.world` / `game.world_map` 的状态（Python GIL 保证线程安全）
2. **消息观察**：TUI 注册为 `game.dialogue_router.add_observer()` 的观察者，接收对话消息
3. **线程安全更新**：observer 回调在引擎线程触发，通过 `app.call_from_thread()` 安全更新 TUI

### 数据流

```
引擎线程 (Game.run_forever)
    │
    ├── tick → 更新 player/agent/conversation 状态
    ├── DialogueRouter observer → 消息推送到 TUI
    └── historical_locations → 位置历史
    │
    ▼ (线程安全：通过 textual 的 call_from_thread)
TUI 线程 (asyncio)
    │
    ├── 500ms 定时器 → 刷新 MapWidget + AgentList + AgentDetail
    ├── 用户输入命令 → 解析 → 切换视角
    └── observer 回调 → 更新 AgentDetail 活动日志
```

## 组件设计

### 1. AgentList（左列）

显示所有 agent 的编号、名字和状态图标。

```
> 1 Alice  [漫步]
  2 Bob    [对话]
  3 Carol  [阅读]
  4 Dave   [寻路]
  5 Eve    [发呆]
```

- 宽度：20 字符
- `>` 标记当前选中的 agent
- 状态图标：`[漫步]` `[对话]` `[阅读]` `[寻路]` `[发呆]`
- 每 500ms 刷新状态

### 2. MapView（中列）

字符矩阵俯视图，以选中 agent 为中心。

- 显示范围：`41×21` 字符（适配 80 列终端）
- 字符映射：
  - `.` 草地
  - `#` 山
  - `~` 水
  - `R` 路
  - `□` 建筑
  - `@` 选中 agent（高亮）
  - `o` 其他 agent
- 刷新频率：500ms
- 选中 agent 始终在地图中央（锁定模式）或可偏移（解锁模式）

地图数据来源：从 `game.world_map.chunk_manager` 读取 chunk 的 `bg_tiles[0]` 和 `obj_tiles[0]`，按 tile ID 映射到字符。

### 3. AgentDetail（右列）

显示选中 agent 的四块信息：

**当前状态**：
```
状态: 漫游中
活动: reading (剩余 45s)
对话: 和 Bob 对话中
```

**当前目标**：
```
目标: 去 (5, -3)
距离: 3.2 tiles
预计: ~5s
```

**今日活动日志**（按时间倒序，最多 20 条）：
```
10:20  开始 reading
10:15  到达 (5, -3)
09:30  和 Bob 发起对话
09:25  离开 (2, 1)
09:20  开始 daydreaming
```

**近期记忆**（最近 3 条摘要）：
```
[重要度 7] 和 Bob 聊了关于天气的话题
[重要度 5] 上午在公园散步时看到 Carol
[重要度 8] 反思：小镇居民都很友好
```

- 宽度：40 字符
- 可滚动查看更多日志

### 4. CommandBar（底栏）

命令输入栏，支持以下命令：

| 命令 | 说明 |
|---|---|
| `<数字>` | 跳转到对应编号的 agent |
| `:jump <名字>` | 按名字跳转 agent |
| `:lock` | 锁定视角，地图持续跟随选中 agent |
| `:unlock` | 解锁视角，地图停在当前位置 |
| `:list` | 在详情面板显示所有 agent 概览 |
| `:help` | 显示命令帮助 |
| `:quit` | 退出 |

- 高度：3 字符
- `>` 提示符 + 输入区
- Enter 执行命令

## 封闭小镇特性

### 不接收外界输入

- **bootstrap 改造**：创建 N 个 agent，不创建人类玩家
- 所有 player 都是 agent，由 `AgentBrain` 驱动决策
- 用户不能干预 agent 行为，只能切换视角观察

### bootstrap 修改

新建 `tui_bootstrap.py`，基于现有 `engine/bootstrap.py` 修改：

```python
def build_tui_game(config: AppConfig) -> Game:
    # ... 与 build_game 相同的装配逻辑 ...
    # 但不创建人类玩家
    for i in range(config.num_agents):
        _create_default_agent(game, now_ms, i)
    return game
```

### 活动日志收集

新建 `tui/activity_log.py`：

```python
class ActivityLog:
    """收集 agent 今日活动，供 TUI 显示。"""

    def __init__(self):
        self._logs: Dict[GameId, List[LogEntry]] = defaultdict(list)

    def on_conversation_started(self, agent_id, partner_id, timestamp):
        ...

    def on_conversation_ended(self, agent_id, partner_id, timestamp):
        ...

    def on_destination_reached(self, agent_id, position, timestamp):
        ...

    def on_activity_started(self, agent_id, activity, timestamp):
        ...

    def get_today_logs(self, agent_id) -> List[LogEntry]:
        ...
```

通过注册为 `DialogueRouter` 的 observer + 直接读取 agent 状态变化来收集事件。

## 文件结构

```
ai_town_py/
├── tui/
│   ├── __init__.py
│   ├── app.py              # Textual App 主类，装配所有 widget
│   ├── widgets/
│   │   ├── __init__.py
│   │   ├── agent_list.py   # 左侧 agent 列表 (ListView)
│   │   ├── map_view.py     # 中间地图视图 (自定义 Widget)
│   │   ├── agent_detail.py # 右侧详情面板 (RichLog + 静态文本)
│   │   └── command_bar.py  # 底部命令栏 (Input)
│   ├── observer.py         # 引擎状态观察者（从 game 读数据）
│   └── activity_log.py     # 今日活动日志收集器
├── main_tui.py             # TUI 入口
└── config.py               # 新增 TUI 配置项
```

## 复用清单

### 直接复用（零修改）

- `engine/` 全部：Game、Agent、World、Player、Conversation、movement、chunk 系统
- `agent_brain/` 全部：AgentBrain、conversation_ops、memory
- `db/`、`dialogue/`、`llm/`
- `data/characters.py`

### 新增

- `tui/` 整个目录
- `main_tui.py` 入口
- `config.py` 新增 TUI 配置项（地图刷新间隔、显示范围等）

### 修改

- 无需修改现有文件（新增 `tui_bootstrap.py` 而非改 `bootstrap.py`）

## 技术选型

- **TUI 框架**：textual（pip install textual）
- **终端兼容**：支持 80 列以上终端，推荐 120 列
- **Python 版本**：3.10+（与现有项目一致）
- **依赖**：textual + 现有项目依赖（pygame 可选，TUI 模式不需要）

## 入口与启动

```bash
# 启动 TUI 小镇
python main_tui.py --num-agents 5 --world-id tui-town

# 重置世界
python main_tui.py --reset --world-id tui-town

# 离线模式（无 LLM）
python main_tui.py --no-llm
```

## 非目标（YAGNI）

- 不做地图编辑功能
- 不做 agent 干预功能（用户不能命令 agent 做什么）
- 不做多窗口/分屏
- 不做录音/回放
- 不做 Web 版本
