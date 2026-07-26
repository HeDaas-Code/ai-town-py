# AI Town (Python 版) 架构文档

本文档基于 `/workspace/ai_town_py/` 下的实际源码撰写，描述系统的分层结构、引擎主循环、状态机、寻路、agent 决策、大脑后台线程、对话路由、LLM 客户端、数据模型、渲染管线、存档恢复与线程模型。所有常量、字段名、调用顺序均与代码一致。

---

## 1. 总览

本项目是 a16z / Convex 团队开源的 [AI Town](https://github.com/a16z-infra/ai-town) 的忠实 Python 重写版：保留原项目的世界观、地图资源、角色 spritesheet 与默认人格（Lucky / Bob / Stella / Alice / Pete），但把整套运行栈从 TypeScript + React/PixiJS + Convex 后端替换为自研 Python 引擎、Pygame 渲染器、OpenAI 兼容 LLM 客户端与 SQLite 持久化后端，单进程即可在本地一键跑起来。

系统按职责分为四层，自上而下依赖关系单向：

```
+----------------------------------------------------------+
|  渲染层  renderer/renderer.py                             |
|  Pygame 绘制：Tileset / Spritesheet / Camera / Bubbles   |
+----------------------------------------------------------+
                       ^ 读取 world / world_map
                       |
+----------------------------------------------------------+
|  引擎层  engine/                                          |
|  Game.tick / begin_step / save_step                       |
|  Player / Agent / Conversation / Movement / StateMachine |
+----------------------------------------------------------+
        ^ enqueue_input                ^ schedule_operation
        |                               |
+--------------------------+   +----------------------------+
| 人类玩家 (main.py)       |   | 大脑层  agent_brain/        |
| Pygame 事件 → input      |   | ThreadPoolExecutor(4)       |
+--------------------------+   | agentDoSomething /          |
                               | agentGenerateMessage /      |
                               | agentRememberConversation   |
                               +----------------------------+
                                       ^ deliver / on_finish
                                       |
+----------------------------------------------------------+
|  持久化层  db/db.py  +  dialogue/router.py                |
|  SQLite (WAL + RLock)  +  DialogueRouter (RLock)          |
+----------------------------------------------------------+
```

- **渲染层 (`renderer/`)**：基于 Pygame，负责把 `WorldMap` 的 bg / object 层切成瓦片并 blit、播放动画精灵、绘制角色帧动画与对话气泡、维护跟随玩家的相机。仅读取引擎状态，不直接修改。
- **引擎层 (`engine/`)**：自研 tick / step 双层模拟引擎。`Game` 单线程驱动世界状态、输入队列与操作调度；`Player` / `Agent` / `Conversation` 三类对象各自维护状态机；`movement.py` 实现 A* 寻路；`state_machine.py` 提供通用 FSM 框架。
- **大脑层 (`agent_brain/`)**：把所有阻塞式 LLM 调用丢到 `ThreadPoolExecutor(max_workers=4)` 后台跑，完成后通过 `on_finish` 回调把 `finish_xxx` input 推回引擎 input 队列。三类操作：`agentDoSomething` / `agentGenerateMessage` / `agentRememberConversation`。
- **持久化层 (`db/` + `dialogue/`)**：`Database` 用 SQLite（WAL + RLock）存储世界快照、玩家/agent 描述、对话消息、记忆与归档；`DialogueRouter` 是本项目相对原项目显式抽出的一层，承担消息的落盘 + 转发 + 观察者通知。

引擎运行在独立的 daemon 线程里，主线程跑 Pygame 事件循环；二者通过 `Game._input_lock`、`Database._lock`、`DialogueRouter._lock` 三把锁同步共享状态。

---

## 2. 引擎主循环

引擎主循环定义在 `engine/game.py` 的 `Game.run_forever()`，常量集中在 `config.py`：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `TICK_DURATION_MS` | `16` | 单 tick 时长，约 60 FPS |
| `STEP_DURATION_MS` | `1000` | 单 step 时长，1 秒 |
| `MAX_TICKS_PER_STEP` | `600` | 一个 step 内最多 600 个 tick |
| `MAX_INPUTS_PER_STEP` | `32` | 每个 step 开头最多处理 32 条 input |
| `ACTION_TIMEOUT_MS` | `120_000` | agent 操作超时阈值，120 秒 |
| `MAX_PATHFINDS_PER_STEP` | `16` | 每个 step 最多寻路 16 次 |

### 2.1 主循环骨架

`Game.run_forever()` 在引擎线程里持续运行，按 TICK / STEP 双节拍驱动：

```
run_forever():
  last_step = last_tick = now
  while not _stop:
    now = time.time() * 1000
    if now - last_tick >= 16:        # TICK 节拍
      tick(now)
      last_tick = now
    if now - last_step >= 1000:      # STEP 节拍
      begin_step(now)                # 处理 input + 派发 operation 给 brain
      save_step()                    # 落盘到 SQLite
      last_step = now
      next_engine_ts = now
    sleep 到下一个 tick 边界（不忙等）
```

### 2.2 每个 tick 的执行顺序

`Game.tick(now)` 严格按以下顺序更新所有对象（顺序非常重要，原项目 `convex/engine/abstractGame.ts` 一致）：

```
1. player.tick         # 清理：人类玩家长时间无输入则 leave
2. player.tick_pathfinding  # 寻路状态机：needsPath → moving；超时停下
3. player.tick_position     # 沿路径插值移动，碰阻挡则 backoff
4. conversation.tick   # 双方 walkingOver 且足够近 → participating；互朝对方
5. agent.tick          # agent 决策：是否 start_operation
6. 记录历史位置        # historical_locations[player_id][now] = {x, y, dx, dy, speed}
```

每个阶段都对所有玩家 / 对话 / agent 遍历一次，保证阶段间状态一致：例如所有玩家先完成 `tick_pathfinding`（更新路径状态），再统一 `tick_position`（按新路径走），避免互相依赖顺序问题。

### 2.3 每个 step 的执行顺序

`Game.begin_step(now)` 在 step 开头一次性处理 input 队列并把待执行 operation 派发给 brain，`Game.save_step()` 在 step 结尾落盘：

```
begin_step(now):
  1. historical_locations.clear()
  2. num_pathfinds = 0                       # 重置本 step 寻路计数
  3. with _input_lock:
       inputs = _pending_inputs[:32]         # 取最多 32 条
       _pending_inputs = _pending_inputs[32:]
  4. for inp in inputs: handle_input(now, inp.name, inp.args)
       # 失败的 input 仅打印日志，不中断
  5. if brain is not None and pending_operations:
       _sync_brain_snapshots()                # 把 descriptions 快照同步到 brain
       ops = pending_operations
       pending_operations = []
       for op in ops: brain.submit(op.name, op.args)

save_step():
  1. diff = take_diff()                      # world.to_dict() + historicalLocations
  2. db.save_world(world_id, diff)
  3. 若 descriptions_modified：
       db.save_player_descriptions / save_agent_descriptions / save_map
  4. db.set_engine_state(world_id, next_engine_ts, generation)
```

### 2.4 线程拓扑

`Game.start()` 用 `threading.Thread(target=run_forever, name="ai-town-engine", daemon=True)` 启动引擎线程，daemon 标志确保主进程退出时引擎线程会被回收；`Game.stop()` 设置 `_stop` Event 并 `join(timeout=2.0)`。主线程跑 Pygame 事件循环（`main.py:run`），通过 `Game.enqueue_input` 与引擎交互。

```
+-------------------+        enqueue_input        +-----------------------+
|  主线程 (Pygame)   | -------------------------> |  引擎线程 (daemon)     |
|  事件循环 + 渲染   |                             |  run_forever()        |
+-------------------+                             |   tick / begin_step   |
        ^                                         |   save_step           |
        | 读取 world / world_map                  +-----------------------+
        |                                                  ^
        |                                                  | submit(op)
        |                                                  v
        |                                       +-----------------------+
        +---------------------------------------|  brain 线程池 (4)      |
              on_finish → enqueue_input         |  LLM 阻塞调用         |
                                                +-----------------------+
```

---

## 3. 状态机

`engine/state_machine.py` 提供通用 FSM 框架，承载玩家寻路、对话成员、agent 操作三套状态机。

### 3.1 通用 FSM 框架

`StateMachine` 是极简容器，持有当前 `State` 与一组 `kind → handler` 映射：

```python
@dataclass
class State:
    kind: str
    data: Dict[str, Any]

@dataclass
class StateMachine:
    state: State
    _handlers: Dict[str, Callable[[Any, float], None]]

    def on(self, kind, handler): self._handlers[kind] = handler
    def transition(self, new_state): self.state = new_state
    def tick(self, ctx, now): self._handlers.get(self.state.kind, noop)(ctx, now)
```

### 3.2 玩家寻路状态机 `PlayerPathfindingState`

挂在 `Player.pathfinding["state"]` 上，三种状态：

```
            find_route 成功               points_equal(pos, dest)
  needsPath ---------------> moving ----------------------------> (stop_player)
     |                          |
     | find_route=None          | blocked / 超时
     v                          v
  (stop_player)              waiting(now + random*1000ms)
                                |
                                | until < now
                                v
                             needsPath
```

- `needsPath`：需要计算路径。`tick_pathfinding` 检查 `game.num_pathfinds < MAX_PATHFINDS_PER_STEP`（16）后才调用 `find_route`，避免单 step 内寻路爆量。
- `waiting`：路径被阻挡时退避一段时间再重试，退避时长 `random.random() * PATHFINDING_BACKOFF_MS`（最大 1 秒）。
- `moving`：沿路径走，`tick_position` 用 `path_position(path, now)` 插值得到当前应该处的位置。

`PATHFINDING_TIMEOUT_MS = 60_000`：整个寻路任务超过 60 秒强制 `stop_player`。

### 3.3 对话成员状态机 `MembershipStatus`

挂在 `ConversationMembership.status` 上，三种状态，对应原项目 `conversationMembership.ts`：

```
  start()                  accept_invite() / 接近                 d < CONVERSATION_DISTANCE
  (发起方)        invited -----------------> walkingOver ---------------------------------> participating
                  (被邀方)                                       <---------------------------------
                                                                    conversation.tick 检测到双方足够近
                                              |
                                              | INVITE_TIMEOUT_MS (60s) 超时 / reject_invite
                                              v
                                          (conversation.stop → leave)
```

- `invited`：被邀方初始状态。`Agent.tick` 里若 `other_player.human` 或 `random() < INVITE_ACCEPT_PROBABILITY (0.8)` 则 `accept_invite`，否则 `reject_invite`。
- `walkingOver`：双方都朝对方走。`INVITE_TIMEOUT_MS = 60_000` 超时则 `leave`。
- `participating`：双方足够近（`distance < CONVERSATION_DISTANCE = 1.3`），`conversation.tick` 把双方移到相邻网格点并互朝对方。

`left` 状态由 `Conversation.stop()` 实现：从 `world.conversations` 移除该对话，给两个 agent 设置 `last_conversation = now` 与 `to_remember = conv_id`。

### 3.4 Agent 操作门控 `InProgressOperation`

`Agent` 持有 `in_progress_operation: Optional[InProgressOperation]`，是简单的「忙则等」门控：

```python
@dataclass
class InProgressOperation:
    name: str
    operation_id: str
    started: float
```

`Agent.tick` 开头先检查：若 `in_progress_operation is not None` 且 `now < started + ACTION_TIMEOUT_MS (120s)`，直接 `return`（等 brain 完成）；超过 120 秒则视为超时，清空 `in_progress_operation` 让 agent 可以发起新操作。`start_operation` 也会强制要求 `in_progress_operation is None`，否则抛 `RuntimeError`。

---

## 4. A\* 寻路

寻路相关代码集中在三个文件：

- `engine/movement.py`：A\* 主流程 + `move_player` / `stop_player` / `blocked`
- `engine/geometry.py`：距离、路径插值、路径压缩
- `engine/minheap.py`：开放表用的最小堆

### 4.1 算法骨架

`find_route(game, now, player, destination)` 经典 A\*：

```
1. start_pos = player.position (允许非整点)
2. current = PathCandidate(start_pos, cost=manhattan_distance(start, dest))
3. heap = MinHeap(lambda a, b: a.cost > b.cost)   # cost 小者优先
4. while current is not None:
     if points_equal(current.position, destination): break
     if manhattan(current, dest) < manhattan(best, dest): best = current
     for cand in explore(current): heap.push(cand)
     current = heap.pop()
5. 若 current is None (没到 dest): current = best; new_destination = best.position
6. dense = 从 current 反向回溯到 start 的 PathComponent 链
7. return {path: compress_path(dense), new_destination}
```

`cost = length + remaining`，其中 `length` 是已走距离，`remaining = manhattan_distance(pos, destination)` 是曼哈顿启发式。每个网格点维护最优 cost（`min_distances[y][x]`），新候选若不优于已有则丢弃。

### 4.2 非整点起点处理

`explore()` 内对非整点位置先对齐到网格：

```python
if x != floor(x):                       # 横向非整点
    neighbors += [(Point(floor(x), y), Vector(-1, 0)),
                  (Point(floor(x)+1, y), Vector(1, 0))]
if y != floor(y):                       # 纵向非整点
    neighbors += [(Point(x, floor(y)), Vector(0, -1)),
                  (Point(x, floor(y)+1), Vector(0, 1))]
if x == floor(x) and y == floor(y):     # 整点：四邻域扩展
    neighbors += [(x+1, y, E), (x-1, y, W), (x, y+1, S), (x, y-1, N)]
```

这样玩家可以从连续位置（如 `x=3.7`）平滑接入网格寻路，不会出现「卡在非整点上无法规划」的问题。

### 4.3 阻挡判定 `blocked()`

`blocked_with_positions(position, other_positions, world_map)` 返回阻挡原因字符串或 `None`：

1. NaN 检查：`isnan(x)` 或 `isnan(y)` 抛 `ValueError`。
2. 越界：`x < 0` 或 `y < 0` 或 `x >= width` 或 `y >= height` → `"out of bounds"`。
3. 地图阻挡：遍历 `world_map.object_tiles`，若 `layer[ix][iy] != -1` → `"world blocked"`。
4. 玩家碰撞：与其他玩家位置 `distance < 0.75`（`COLLISION_THRESHOLD`）→ `"player"`。

`COLLISION_THRESHOLD = 0.75` 防止两个玩家位置重叠。`tick_position` 检测到下一位置被阻挡时，会进入 `waiting` 状态等待 `random * PATHFINDING_BACKOFF_MS` 毫秒后重试。

### 4.4 路径压缩 `compress_path()`

A\* 产生的 `dense` 路径包含每个网格点，`compress_path(dense)` 去掉可被相邻两端点线性插值还原的中间点：

```
对 dense 中每个中间点 candidate：
  probe = path_position([last, point], candidate.t)   # 用 last→point 直线插值
  若 probe.position 与 candidate.position 距离 < EPSILON 且 facing 也接近：
    candidate = point  # candidate 可被跳过
  否则：
    out.append(candidate); last = candidate; candidate = point
```

压缩后路径用打包五元组 `(x, y, dx, dy, t)` 表示（`engine/types.py: PackedPathComponent`），便于序列化与 SQLite 存储。

### 4.5 寻路节流

`Game.num_pathfinds` 在每个 `begin_step` 开头清零，`Player.tick_pathfinding` 进入 `needsPath` 分支前检查 `game.num_pathfinds < MAX_PATHFINDS_PER_STEP (16)`，超过则本 step 不再寻路，等下一个 step。这避免单 step 内大量玩家同时寻路导致卡顿。

### 4.6 最小堆 `MinHeap`

`engine/minheap.py` 用标准库 `heapq` + 比较器包装实现：

```python
class MinHeap(Generic[T]):
    def __init__(self, priority): self._greater = priority   # priority(a,b)=True 表示 a>b
    def push(self, item): heappush(self._data, [_Item(item, self._greater), counter])
    def pop(self): return heappop(self._data)[0].item
```

`_Item.__lt__` 把外部 `greater(a, b)` 转成 `a < b ⟺ greater(b, a)`，配合 `itertools.count()` 作为 tie-breaker 保证稳定排序。

---

## 5. Agent 决策

`engine/agent.py: Agent.tick(game, now)` 是 agent 自主行为的入口。决策按优先级从上到下短路：

### 5.1 决策流程

```
Agent.tick(game, now):
  player = world.players[self.player_id]

  # 1. 操作门控
  if in_progress_operation is not None:
    if now < started + ACTION_TIMEOUT_MS (120s): return   # 等 brain
    in_progress_operation = None                           # 超时放弃

  conversation = world.player_conversation(player)
  member = conversation.participants.get(player.id) if conversation else None
  recently_attempted_invite = (last_invite_attempt and now < last_invite_attempt + CONVERSATION_COOLDOWN_MS)
  doing_activity = (player.activity and player.activity.until > now)
  if doing_activity and (conversation or player.pathfinding):
    player.activity.until = now                            # 中断活动

  # 2. 闲着没事 -> 决策「做什么」
  if (conversation is None and not doing_activity
      and (player.pathfinding is None or not recently_attempted_invite)):
    start_operation("agentDoSomething", {
      player, otherFreePlayers, agent, map
    })
    return

  # 3. 有对话待记忆 -> 记忆
  if to_remember is not None:
    start_operation("agentRememberConversation", {playerId, agentId, conversationId: to_remember})
    to_remember = None
    return

  # 4. 在对话里 -> 按成员状态分发
  if conversation and member:
    other = 对方 player / member
    if member.status.kind == "invited":      # 接受 / 拒绝邀请
      accept or reject (按 INVITE_ACCEPT_PROBABILITY=0.8 或 human)
    elif member.status.kind == "walkingOver": # 朝对方走，超时则 leave
      if distance < CONVERSATION_DISTANCE: return
      if pathfinding is None: move_player(对方或中点)
    elif member.status.kind == "participating":
      # 生成消息：start / continue / leave
      ...
```

### 5.2 `participating` 分支的消息生成

`participating` 状态下根据对话进度决定消息类型：

| 条件 | 消息类型 | 说明 |
| --- | --- | --- |
| `last_message is None` 且（自己是发起方 或 `awkward_deadline < now`） | `agentGenerateMessage` type=`start` | 对话第一条消息 |
| `started + MAX_CONVERSATION_DURATION_MS < now` 或 `num_messages > MAX_CONVERSATION_MESSAGES (8)` | type=`leave` | 对话太久，主动结束 |
| 上一条是自己发的且未到 `AWKWARD_CONVERSATION_TIMEOUT_MS` | 不操作 | 等对方回 |
| 上一条是对方发的且未过 `MESSAGE_COOLDOWN_MS (2000)` | 不操作 | 冷却中 |
| 否则 | type=`continue` | 正常续聊 |

调用 `conversation.set_is_typing(now, player, msg_uuid)` 占位打字状态后，`start_operation("agentGenerateMessage", {type, playerId, conversationId, otherPlayerId, messageUuid})`。`MAX_CONVERSATION_DURATION_MS = 10 * 60_000`（10 分钟）。

### 5.3 `walkingOver` 分支的移动决策

- 距离 `pd = distance(player, other)`：
  - `pd < CONVERSATION_DISTANCE (1.3)`：什么都不做（等 `conversation.tick` 把双方转成 `participating`）。
  - `pd < MIDPOINT_THRESHOLD (4)`：直接走到对方所在网格点 `floor(other.x), floor(other.y)`。
  - 否则：走到两人中点 `floor((px+ox)/2), floor((py+oy)/2)`，避免长距离相向走。
- `INVITE_TIMEOUT_MS = 60_000` 超时则 `conversation.leave`。

### 5.4 `start_operation` 的副作用

```python
def start_operation(self, game, now, name, args):
    if self.in_progress_operation is not None: raise RuntimeError(...)
    operation_id = game.alloc_id("operations")           # 分配 o:N 形式 ID
    game.schedule_operation(name, {"operationId": operation_id, **args})
    self.in_progress_operation = InProgressOperation(name, operation_id, started=now)
```

`game.schedule_operation` 把 operation 追加到 `Game.pending_operations` 列表，等下一个 `begin_step` 派发给 brain。

---

## 6. 大脑后台线程

`agent_brain/agent_ops.py: AgentBrain` 是 LLM 操作的执行器，与 `Game` 解耦：`Game` 调 `brain.submit(name, args)`，brain 在线程池里跑完后通过 `on_finish` 回调把 input 推回 `Game.enqueue_input`。

### 6.1 线程池配置

```python
class AgentBrain:
    def __init__(self, llm, db, world_id, dialogue_router, memory_store,
                 on_finish, max_workers=4):
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="brain")
        self.player_descriptions_snapshot: Dict = {}
        self.agent_descriptions_snapshot: Dict = {}
```

固定 4 个 worker 线程，线程名前缀 `brain`。`submit(name, args)` 直接 `_executor.submit(self._run, name, args)` 返回 `Future`。

### 6.2 三类操作

`AgentBrain._HANDLERS` 映射：

| 操作名 | handler | 调用方 | 完成后回写的 input |
| --- | --- | --- | --- |
| `agentDoSomething` | `_agent_do_something` | `Agent.tick` 闲着时 | `finishDoSomething` |
| `agentGenerateMessage` | `_agent_generate_message` | `Agent.tick` participating 时 | `agentFinishSendingMessage` |
| `agentRememberConversation` | `_agent_remember_conversation` | `Agent.tick` to_remember 时 | `finishRememberConversation` |

### 6.3 操作执行流程

以 `_agent_generate_message` 为例：

```
_agent_generate_message(args):
  1. 从 args 取 msg_type / player_id / conversation_id / other_player_id / message_uuid / operation_id
  2. 从 player_descriptions_snapshot / agent_descriptions_snapshot 拿描述
  3. 按 msg_type 调对应函数（阻塞调 LLM）：
       start    -> start_conversation_message(...)
       continue -> continue_conversation_message(...)
       leave    -> leave_conversation_message(...)
  4. router.deliver(RoutedMessage(...))   # 落盘 + 转发 + 通知观察者
  5. on_finish("agentFinishSendingMessage", {agentId, conversationId, timestamp, operationId, leaveConversation})
```

`_agent_do_something` 内会按状态决策：刚结束对话 / 刚做过活动 → 漫游到随机点；否则做随机活动；若在寻路且未冷却 → `find_conversation_candidate` 找人聊。`_agent_remember_conversation` 调 `remember_conversation(...)` 让 LLM 总结对话并写入 `memories` 表，附带 embedding。

### 6.4 失败处理

`_run` 包了 try/except：失败时若操作是 `agentGenerateMessage`，仍发 `agentFinishSendingMessage`（带 `leaveConversation=False`）以清掉 `in_progress_operation` 与 typing 状态；其他操作仅打印日志，让 engine 在 120 秒后超时清理。

### 6.5 描述快照同步

`Game._sync_brain_snapshots()` 在 `begin_step` 派发 operation 前调用，把当前的 `player_descriptions` 与 `agent_descriptions` 拷贝到 brain 的 snapshot 字段：

```python
brain.player_descriptions_snapshot = dict(self.player_descriptions)
agent_by_player = {a.player_id: ad for aid, ad in agent_descriptions.items()
                   for a in [world.agents.get(aid)] if a is not None}
brain.agent_descriptions_snapshot = agent_by_player
```

注意 agent_descriptions 按 `player_id` 重新索引，因为对话函数只看 `player_id`。这样 brain 在 LLM 调用期间读的是 step 开始时刻的快照，不会受引擎后续修改影响。

---

## 7. 对话路由

`dialogue/router.py` 是本项目相对原项目最显式的一层抽象。原 AI Town 的「A 给 B 发消息」由 Convex 的 mutation / query 隐式承担；本项目把它抽出来做成 `DialogueRouter`，所有 agent / 人类玩家在此注册，消息先落盘再派发。

### 7.1 核心数据结构

```python
@dataclass
class RoutedMessage:
    conversation_id: str
    author: str          # 发送者 playerId
    recipient: str       # 接收者 playerId
    text: str
    message_uuid: str
    timestamp: int
    leave_conversation: bool = False

@dataclass
class AgentEndpoint:
    agent_id: str
    player_id: str
    name: str
    handler: Callable[[RoutedMessage], None]                      # 收到消息时的回调
    on_conversation_started: Optional[Callable[[str, str, str], None]] = None
    on_conversation_ended: Optional[Callable[[str], None]] = None
```

### 7.2 `DialogueRouter` 接口

| 方法 | 作用 |
| --- | --- |
| `register(endpoint)` | 把 `player_id → endpoint` 加入 `_endpoints` |
| `unregister(player_id)` | 移除 endpoint |
| `get_endpoint(player_id)` | 查 endpoint |
| `list_registered()` | 列出所有已注册 player_id |
| `add_observer(observer)` | 加全局观察者（每条路由消息都回调） |
| `on_conversation_started(conv_id, a, b)` | 广播对话开始事件给两端 endpoint |
| `on_conversation_ended(conv_id)` | 广播对话结束事件 |
| `deliver(msg)` | 转发消息（核心） |
| `conversation_history(conv_id)` | 查 SQLite 里的对话历史 |

所有方法都在 `threading.RLock` 保护下操作 `_endpoints` / `_conversations` / `_observers` 三张表。

### 7.3 `deliver()` 流程

```
deliver(msg):
  +--------------------------------------------+
  | 1. msg_id = bus.persist(msg)               |  写入 SQLite messages 表
  |    db.insert_message(world_id, conv_id,    |
  |                       author, text, uuid)  |
  +--------------------------------------------+
                   |
                   v
  +--------------------------------------------+
  | 2. with _lock:                             |  拷贝 observers 与 recipient_ep 引用
  |      observers = list(_observers)          |  （锁内只做引用拷贝，锁外回调）
  |      recipient_ep = _endpoints.get(recip)  |
  +--------------------------------------------+
                   |
                   v
  +--------------------------------------------+
  | 3. for obs in observers: obs(msg)          |  通知 UI / 调试日志
  |    异常被吞掉，不影响主流程                  |
  +--------------------------------------------+
                   |
                   v
  +--------------------------------------------+
  | 4. if recipient_ep: recipient_ep.handler(msg) |  派发给接收 agent
  |    异常被吞掉                                  |
  +--------------------------------------------+
  return msg_id
```

观察者用于 UI 即时显示气泡（`renderer.on_routed_message` 注册成 observer），handler 用于 agent 侧反应（目前 bootstrap 里注册的 handler 仅打印日志，因为 agent 在下一 tick 通过 `conversation.last_message` 自动感知消息并触发回复）。

### 7.4 生命周期事件

`Conversation.start` 调 `router.on_conversation_started(conv_id, player.id, invitee.id)`，`Conversation.stop` 调 `router.on_conversation_ended(conv_id)`。路由把对话记录到 `_conversations: Dict[conv_id, (player_a, player_b)]`，并触发两端 endpoint 的可选 `on_conversation_started` / `on_conversation_ended` 回调（异常被吞）。

### 7.5 调用关系图

```
+----------------+  deliver(RoutedMessage)   +-----------------------+
|  agent_brain   | ------------------------> |  DialogueRouter       |
|  (LLM 线程)    |                            |   _lock: RLock        |
+----------------+                            |   _endpoints          |
        ^                                     |   _observers          |
        |                                     |   _conversations      |
        | on_finish → enqueue_input           +-----------+-----------+
        |                                                 |
        |                                       persist   |  notify
        |                                                 v
        |                                       +-----------------+
        |                                       |  SQLite         |
        |                                       |  messages 表    |
        |                                       +-----------------+
        |
        +-----  Renderer.on_routed_message(msg) → add_bubble(author, text, ts)
                                              (主线程画气泡)
```

---

## 8. LLM 客户端

`llm/client.py: LLMClient` 是 OpenAI 兼容协议的薄封装，所有 LLM 调用（chat / embedding）都走这里。`llm/offline.py` 提供 `OfflineLLMClient` 用于 `--no-llm` 离线模式。

### 8.1 `chat_completion`

```python
def chat_completion(self, messages, *, max_tokens=300, temperature=None,
                    stop=None, response_format=None) -> ChatResult:
    body = {
      "model": config.chat_model,
      "messages": [m.to_dict() for m in messages],
      "max_tokens": max_tokens,
    }
    if temperature is not None: body["temperature"] = temperature
    # 合并 stop words：调用方 + 全局（如 Ollama 的 <|eot_id|>）
    stop_words = list(stop or []) + list(config.stop_words)
    if stop_words: body["stop"] = stop_words
    if response_format is not None: body["response_format"] = response_format

    result = self._retry(self._post_json, f"{url}/v1/chat/completions", body)
    content = result["choices"][0]["message"]["content"]
    return ChatResult(content=content, retries=result["retries"], ms=result["ms"])
```

返回 `ChatResult(content, retries, ms)`，方便上层统计重试次数与耗时。

### 8.2 `fetch_embedding`（provider 感知）

```python
def fetch_embedding(self, text) -> EmbeddingResult:
    if config.provider == "ollama":
        body = {"model": config.embedding_model, "prompt": text.replace("\n", " ")}
        result = self._retry(self._post_json, f"{url}/api/embeddings", body)
        embedding = result["embedding"]
    else:
        body = {"model": config.embedding_model, "input": [text.replace("\n", " ")]}
        result = self._retry(self._post_json, f"{url}/v1/embeddings", body)
        data = sorted(result["data"], key=lambda d: d.get("index", 0))
        embedding = data[0]["embedding"]
    return EmbeddingResult(embedding, retries=result["retries"], ms=result["ms"])
```

- **Ollama**：`POST /api/embeddings`，请求体 `{model, prompt}`，响应直接含 `embedding` 字段。默认维度 `OLLAMA_EMBEDDING_DIMENSION = 1024`。
- **OpenAI / Custom**：`POST /v1/embeddings`，请求体 `{model, input: [...]}`，响应 `data` 数组按 `index` 排序后取首条。默认维度 `OPENAI_EMBEDDING_DIMENSION = 1536`。

### 8.3 重试策略

`_retry(fn, *args)` 按以下规则重试：

- `RETRY_BACKOFF_MS = [1000, 10000, 20000]`：最多重试 3 次，退避时长依次 1s / 10s / 20s。
- `RETRY_JITTER_MS = 100`：每次退避加 0~100ms 抖动，避免惊群。
- 仅当 `LLMError.retryable=True` 时重试；非可重试错误（如 4xx 除 429）立即抛出。
- 可重试条件（`_post_json` 内判定）：
  - `HTTPError.code == 429`（限流）→ `retryable=True`
  - `HTTPError.code >= 500`（服务端错误）→ `retryable=True`
  - `URLError`（网络层错误）→ `retryable=True`
  - 其他 HTTPError → `retryable=False`
- 非 `LLMError` 的异常（如 `json.JSONDecodeError`）也按相同退避重试。

### 8.4 Stop words 合并

调用方传的 `stop` 与 `config.stop_words` 合并后一起放进请求体 `stop` 字段。`config.stop_words` 在 `get_llm_config()` 里按 provider 设置：

- Ollama：`["<|eot_id|>"]`（llama3 系列的结束符）
- OpenAI / Custom：`[]`（由调用方自己提供）

### 8.5 `LLMConfig` provider 探测

`config.py: get_llm_config()` 按优先级探测 provider：

```
1. LLM_API_URL 已设置？
   -> provider="custom"，要求 LLM_MODEL / LLM_EMBEDDING_MODEL 也设置
   -> api_key = LLM_API_KEY (可选)
   -> embedding_dimension = EMBEDDING_DIMENSION 或 1536

2. OPENAI_API_KEY 已设置？
   -> provider="openai"，url="https://api.openai.com"
   -> chat_model = OPENAI_CHAT_MODEL 或 "gpt-4o-mini"
   -> embedding_model = OPENAI_EMBEDDING_MODEL 或 "text-embedding-ada-002"
   -> embedding_dimension = 1536

3. 否则回退 Ollama 本地
   -> provider="ollama"，url=OLLAMA_HOST 或 "http://127.0.0.1:11434"
   -> chat_model = OLLAMA_MODEL 或 "llama3"
   -> embedding_model = OLLAMA_EMBEDDING_MODEL 或 "mxbai-embed-large"
   -> stop_words = ["<|eot_id|>"]
   -> embedding_dimension = 1024
```

`LLMClient._auth_headers()` 仅在 `config.api_key` 非空时返回 `{"Authorization": "Bearer <key>"}`，Ollama 本地无需鉴权。

---

## 9. 数据模型

`db/db.py` 顶部 `SCHEMA` 字符串定义了 11 张表，启动时由 `Database._init_schema()` 用 `executescript` 一次性建表（`CREATE TABLE IF NOT EXISTS`）。所有写操作走单连接 + WAL + `threading.RLock`。

### 9.1 表清单

| # | 表名 | 用途 | 对应原项目 Convex 表 |
| --- | --- | --- | --- |
| 1 | `worlds` | 世界快照 | `worlds` |
| 2 | `maps` | 世界地图 | `maps` |
| 3 | `player_descriptions` | 玩家描述 | `playerDescriptions` |
| 4 | `agent_descriptions` | agent 描述 | `agentDescriptions` |
| 5 | `messages` | 对话消息 | `messages` |
| 6 | `memories` | 长期记忆 | `memories` |
| 7 | `memory_embeddings` | 记忆向量 | `memoryEmbeddings` |
| 8 | `archived_conversations` | 归档对话 | `archivedConversations` |
| 9 | `archived_players` | 归档玩家 | `archivedPlayers` |
| 10 | `participated_together` | 「谁和谁聊过」索引 | `participatedTogether` |
| 11 | `world_engine_state` | 引擎推进状态 | （引擎恢复用） |

### 9.2 各表字段

**`worlds`**：世界完整快照，每个 step 覆盖写一次。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK | world_id（如 `"default"`） |
| `state_json` | TEXT | `Game.take_diff()` 序列化后的 JSON |
| `updated_at` | INTEGER | 上次落盘时间戳（ms） |

**`maps`**：每个 world 一张地图。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT PK | world_id |
| `map_json` | TEXT | `WorldMap.to_dict()` 序列化后的 JSON |

**`player_descriptions`**：玩家身份信息，复合主键 `(world_id, player_id)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT | world_id |
| `player_id` | TEXT | 玩家 ID（`p:N`） |
| `character` | TEXT | 角色名（如 `"f1"`） |
| `description` | TEXT | 自我介绍 |
| `name` | TEXT | 显示名（如 `"Lucky"`） |

**`agent_descriptions`**：agent 人格，复合主键 `(world_id, agent_id)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT | world_id |
| `agent_id` | TEXT | agent ID（`a:N`） |
| `identity` | TEXT | 人格描述（喂给 LLM 的 `About you`） |
| `plan` | TEXT | 对话目标（喂给 LLM 的 `Your goals`） |

**`messages`**：所有对话消息，按 `id` 自增，索引 `idx_messages_conv(world_id, conversation_id)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | 消息自增 ID |
| `world_id` | TEXT | world_id |
| `conversation_id` | TEXT | 对话 ID（`c:N`） |
| `author` | TEXT | 发送者 playerId |
| `text` | TEXT | 消息正文 |
| `message_uuid` | TEXT | 调用方提供的 UUID（幂等） |
| `created_at` | INTEGER | 写入时间戳（ms） |

**`memories`**：agent 长期记忆，索引 `idx_memories_player(player_id)` 与 `idx_memories_player_type(player_id, data_json)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | 记忆自增 ID |
| `world_id` | TEXT | world_id |
| `player_id` | TEXT | 记忆归属玩家 |
| `agent_id` | TEXT NULL | 关联 agent（反思型记忆可为 NULL） |
| `description` | TEXT | 自然语言摘要（喂给 LLM） |
| `importance` | REAL | 0-9 重要度（LLM 打分） |
| `last_access` | INTEGER | 上次被检索到的时间戳 |
| `data_json` | TEXT | `{type: "conversation"/"reflection", ...}` |
| `embedding_id` | INTEGER NULL | 关联 `memory_embeddings.id` |
| `created_at` | INTEGER | 创建时间戳 |

**`memory_embeddings`**：记忆向量，BLOB 存 little-endian float32 数组。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | 向量自增 ID |
| `player_id` | TEXT | 归属玩家 |
| `embedding` | BLOB | `struct.pack("<Nf", *vec)` 打包的向量 |

**`archived_conversations`**：对话结束后的归档，复合主键 `(world_id, id)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT | world_id |
| `id` | TEXT | 对话 ID |
| `created` | INTEGER | 创建时间戳 |
| `creator` | TEXT | 发起方 playerId |
| `ended` | INTEGER | 结束时间戳 |
| `last_message` | TEXT NULL | 最后一条消息 JSON |
| `num_messages` | INTEGER | 消息数 |
| `participants` | TEXT | 参与者 playerId 列表 JSON |

**`archived_players`**：玩家离开后的归档，复合主键 `(world_id, id)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT | world_id |
| `id` | TEXT | 玩家 ID |
| `player_json` | TEXT | `Player.to_dict()` 序列化后的 JSON |

**`participated_together`**：「谁和谁聊过」的双向边索引，索引 `idx_pt_edge(world_id, player1, player2)` 与 `idx_pt_conv(world_id, conversation_id, player1)`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT | world_id |
| `conversation_id` | TEXT | 对话 ID |
| `player1` | TEXT | 一方 playerId |
| `player2` | TEXT | 另一方 playerId |
| `ended` | INTEGER | 对话结束时间戳 |

**`world_engine_state`**：引擎推进状态，主键 `world_id`。
| 列 | 类型 | 说明 |
| --- | --- | --- |
| `world_id` | TEXT PK | world_id |
| `last_engine_ts` | INTEGER | 上次 step 时间戳 |
| `generation` | INTEGER DEFAULT 0 | 世代号（暂未使用） |

### 9.3 向量编码与检索

`_pack_embedding(vec)` 用 `struct.pack(f"<{len(vec)}f", *vec)` 打包成 little-endian float32 字节流；`_unpack_embedding(blob, expected)` 反向解包，`expected > 0` 时维度不匹配返回空列表跳过。`_cosine(a, b)` 标准余弦相似度。

`Database.search_memories(player_id, query_embedding, limit=30)` 先 SQL 拉出该玩家所有带 embedding 的记忆，Python 端算余弦相似度后排序取 top-N。`MemoryStore.search` 在此基础上 overfetch 10 倍（`MEMORY_OVERFETCH = 10`），再按 `relevance + recency + importance` 三项归一化后重新排序，取前 `NUM_MEMORIES_TO_SEARCH (3)` 条，并对超过 `MEMORY_ACCESS_THROTTLE_MS (300s)` 未访问的记忆 `touch_memory` 更新 `last_access`。

---

## 10. 渲染管线

`renderer/renderer.py: Renderer` 基于 Pygame，对应原项目 `src/components/{PixiGame, PixiStaticMap, PixiViewport, Character}.tsx`。

### 10.1 资源加载

- **`Tileset`**：把整张 PNG（如 `rpg-tileset.png`）按 `tile_dim` 切成 `num_x * num_y` 个 tile Surface，索引 `layer[x][y]` 取对应 tile。
- **`Spritesheet`**：解析 PIXI 风格的 JSON（`frames` / `animations` 字段），按 `frame.x/y/w/h` 从大图切出每帧 Surface。`animation_frames(name)` 返回某动画的帧列表。
- **`_load_spritesheet(json_path)`**：从 spritesheet JSON 加载，优先用 `meta.image`，否则同目录同名 PNG。

启动时加载 8 套角色精灵表（`f1..f8`，共用 `32x32folk.png`）与 5 套动画精灵表（`campfire / gentlesparkle / gentlewaterfall / windmill / gentlesplash`）。

### 10.2 `Camera`

```python
@dataclass
class Camera:
    width, height: int = 1024, 768
    camera_x, camera_y: float = 0.0, 0.0   # 相机左上角对应的世界像素
    zoom: float = 1.0
```

| 方法 | 作用 |
| --- | --- |
| `follow(world_x, world_y)` | 让相机中心对准目标，用 0.2 的插值平滑跟随，避免抖动 |
| `snap(world_x, world_y)` | 瞬移相机中心到指定位置（启动时用） |
| `world_to_screen(x, y)` | 世界像素 → 屏幕像素 |
| `screen_to_world(sx, sy)` | 屏幕像素 → 世界像素（点击拾取用） |
| `clamp(world_w, world_h)` | 限制相机不出地图边界 |

### 10.3 `Renderer.draw()` 绘制顺序

```python
def draw(self, game, now_ms, dt_sec, viewer_player_id=None):
    1. _anim_clock += dt_sec
    2. _prune_bubbles(now_ms)                          # 清理过期气泡
    3. camera.follow(viewer.position)                  # 跟随玩家
    4. camera.clamp(world_map 尺寸)                    # 限制不出界
    5. screen.fill((0,0,0))                            # 清屏
    6. screen.blit(_get_static_map(), (-cam_x, -cam_y))# 静态地图（缓存）
    7. _draw_animated_sprites()                        # 火焰 / 水流 / 风车等
    8. for player in sorted(players, key=y):           # 按 y 排序保证遮挡正确
         _draw_player(game, player, now_ms, is_viewer)
    9. for player in players:                          # 气泡
         _draw_player_bubble(game, player, now_ms)
   10. _draw_hud(game, viewer_player_id)               # HUD
```

### 10.4 静态地图缓存

`_build_static_map()` 把 `bg_tiles + object_tiles` 所有层一次性 blit 到一张 `width*tile_dim × height*tile_dim` 的大 Surface（`pygame.SRCALPHA`），存到 `self._static_map`。后续每帧直接 `screen.blit(static_map, (-cam_x, -cam_y))`，Pygame 会按需裁剪。只有首次绘制时才构建，避免每帧重新切瓦片。

### 10.5 角色绘制

`_draw_player` 根据 `player.facing` 映射到 4 方向动画名（`right / down / left / up`，用 `atan2` 算角度后 `// 90`），从对应 `Spritesheet` 取帧：

- 移动中（`speed > 0.001`）：`frame_idx = int(anim_clock * ANIMATION_FPS=6) % len(frames)` 循环播放。
- 静止：显示第一帧。
- 人类玩家额外画黄色圆点标识（`_draw_viewer_indicator`）。
- 对话中且 `is_typing` 是自己：头顶画 `...`。
- 有活动：头顶画活动 emoji（如 `📖`）。

角色精灵居中绘制（`frame.get_rect(center=(sx, sy))`），让脚底落在 tile 中心。

### 10.6 气泡系统

```python
@dataclass
class SpeechBubble:
    player_id: str
    text: str
    born_at: int

SPEECH_BUBBLE_TTL_MS = 4000   # 4 秒
```

- `on_routed_message(msg)`：作为 `DialogueRouter` 的 observer，每条路由消息都调一次，`add_bubble(msg.author, msg.text, msg.timestamp)`。同一玩家的旧气泡会被替换。
- `_prune_bubbles(now_ms)`：每帧清理超过 TTL 的气泡。
- `_draw_player_bubble`：渲染气泡 Surface（白底圆角矩形 + 黑边 + 文字），最后 1 秒淡出（`alpha = 255 * (ttl - age) / 1000`），画在角色头顶上方 32 像素处。

### 10.7 HUD

`_draw_hud` 在屏幕左上角画简单文字（带黑色阴影）：玩家名、对话状态（`In conversation with: <name>` + 提示键位）、玩家数 / agent 数。`main.py:_draw_debug`（F1 切换）在右下角画 FPS / Players / Agents / Conversations / Pending inputs / Pending ops / Pos / Speed。

---

## 11. 存档与恢复

`engine/bootstrap.py: build_game(config)` 负责装配完整可运行的 `Game`，处理首次启动与存档恢复两种情况。

### 11.1 装配流程

```
build_game(config):
  1. db = Database(config.db_path)                  # 打开 SQLite，自动建表
  2. world_map = WorldMap.load(config.map_path)     # 从 JSON 加载地图
  3. state = db.load_world(config.world_id)         # 尝试从 SQLite 恢复
     if state is not None:                          # 已有存档
       world = World.from_dict(state["world"])
       player_descs = { p.playerId: PlayerDescription.from_dict(p)
                        for p in state["playerDescriptions"] }
       agent_descs  = { a.agentId:  AgentDescription.from_dict(a)
                        for a in state["agentDescriptions"] }
     else:                                          # 首次启动
       world = World()                              # 空：next_id=0, players/agents/conversations={}
       player_descs = {}
       agent_descs  = {}
  4. router = DialogueRouter(db, config.world_id)
  5. game = Game(config, db, world_map, world, router)
     game.player_descriptions = player_descs
     game.agent_descriptions  = agent_descs
  6. llm = OfflineLLMClient() if not config.enable_llm else LLMClient()
     memory_store = MemoryStore(db, config.world_id)
     brain = AgentBrain(llm, db, config.world_id, router, memory_store,
                        on_finish=lambda name, args: game.enqueue_input(name, args))
     game.attach_brain(brain)                       # brain._on_finish = enqueue_input
  7. if state is None:                              # 首次启动：创建 N 个 agent
       for i in range(config.num_agents):
         _create_default_agent(game, now_ms, i)     # 从 data.characters.descriptions 取人格
  8. _register_agents_to_router(game)               # 把所有 agent 注册到 router
  9. return game
```

### 11.2 首次启动

`World()` 默认 `next_id=0`、`players / conversations / agents` 全空。`_create_default_agent` 从 `data.characters.descriptions` 取预设人格（Lucky / Bob / Stella / Alice / Pete），调 `Player.join` 随机找一个未阻挡的网格点出生，再创建 `Agent` 与 `AgentDescription`。`game.descriptions_modified = True` 触发 `save_step` 时把 descriptions 一起落盘。

### 11.3 存档恢复

`db.load_world(world_id)` 从 `worlds` 表读 `state_json` 反序列化。`World.from_dict` 重建 `players / conversations / agents` 三个 dict，每个对象都用各自的 `from_dict` 反序列化（包括 `Player.pathfinding.state` 这种嵌套状态机）。`player_descriptions` 与 `agent_descriptions` 也从 SQLite 恢复。注意 `world_map` 始终从磁盘 JSON 加载（不存 SQLite，除非 `descriptions_modified`），保证地图与代码版本一致。

### 11.4 `--reset` 流程

`main.py:run` 在 `build_game` 前检查 `config.reset`：

```python
if config.reset:
    Database(config.db_path).delete_world(config.world_id)
```

`Database.delete_world(world_id)` 一次性清掉该 world_id 在所有表中的相关行：

```
1. DELETE FROM worlds WHERE id=?
2. 取出该 world 下所有 memories 的 player_id，删 memory_embeddings
3. DELETE FROM maps / player_descriptions / agent_descriptions /
            messages / memories /
            archived_conversations / archived_players /
            participated_together / world_engine_state
   WHERE world_id=?
```

注意 `worlds` 表的列叫 `id`（不是 `world_id`），`memory_embeddings` 没有 `world_id` 列要按 `player_id` 间接清理。

### 11.5 `World` 序列化

`World.to_dict()` 返回 `{nextId, players, conversations, agents}` 三个列表 + nextId；`World.from_dict(d)` 反向重建。`Game.take_diff()` 在此基础上加 `historicalLocations`（按 tick 累积的位置缓冲），并在 `descriptions_modified` 时附加 `playerDescriptions / agentDescriptions / worldMap`。`save_step` 把 diff 写入 `worlds.state_json`（覆盖），descriptions 写入各自表（upsert）。

---

## 12. 线程模型

整个系统在单进程内运行，共四类线程：

### 12.1 线程清单

| 线程 | 数量 | 创建方式 | 职责 |
| --- | --- | --- | --- |
| 主线程 | 1 | Pygame 主循环 | 事件处理 + 渲染 + 人类玩家输入 |
| 引擎线程 | 1 (daemon) | `Game.start()` 创建 `Thread(name="ai-town-engine", daemon=True)` | `run_forever()`：tick / begin_step / save_step |
| Brain worker | 4 | `ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain")` | 阻塞式 LLM 调用 |
| (SQLite WAL) | 若干 | SQLite 内部 | WAL 模式的后台 checkpoint |

### 12.2 共享状态与锁

| 共享状态 | 守护锁 | 访问者 |
| --- | --- | --- |
| `Game._pending_inputs` | `Game._input_lock` (Lock) | 主线程 `enqueue_input` 写；引擎线程 `begin_step` 读 |
| `Database._conn` | `Database._lock` (RLock) | 引擎线程 `save_step` 写；brain worker `insert_message` / `list_messages` / `search_memories` 读写；主线程 F2 手动保存 |
| `DialogueRouter._endpoints / _conversations / _observers` | `DialogueRouter._lock` (RLock) | brain worker `deliver` 写消息；主线程 `register` / `add_observer`；引擎线程 `on_conversation_started/ended` |

`Game.pending_operations` 列表只在引擎线程内读写（`schedule_operation` 由 `Agent.tick` 在引擎线程调用），无需额外锁。`Game.world` 也只在引擎线程内修改，主线程只读（渲染时遍历 `world.players / conversations`），靠 GIL 保证不出现撕裂读，但理论上有读到中间状态的风险——本项目接受这一权衡以避免渲染层加锁。

### 12.3 线程协作图

```
+-------------------+  enqueue_input        +-----------------------+
| 主线程 (Pygame)    | ------------------->  | 引擎线程 (daemon)      |
| - 事件循环         |  (写 _input_lock)     | run_forever():         |
| - Renderer.draw    |                       |   tick (16ms)          |
| - 输入 → enqueue   | <-------------------  |   begin_step (1000ms):  |
|                   |  读取 world (无锁)     |     handle inputs      |
|                   |                       |     dispatch ops        |
+-------------------+                       |   save_step            |
        ^                                   +-----------+-----------+
        | register / add_observer                       ^
        | (写 router._lock)                             | schedule_operation
        |                                               v
        |                                  +-----------------------+
        +---- on_routed_message (obs) <----| brain worker x4       |
              (router.deliver 通知)        | ThreadPoolExecutor    |
                                            | LLM 阻塞调用          |
                                            | router.deliver        |
                                            | db.insert_message     |
                                            +-----------+-----------+
                                                        ^
                                                        | _lock (Database)
                                                        v
                                            +-----------------------+
                                            | SQLite (WAL)          |
                                            | 11 张表 + 索引         |
                                            +-----------------------+
```

### 12.4 退出流程

主线程 `main.py:run` 的 `finally` 块负责优雅退出：

```python
finally:
    try: game.save_step()         # 最后再落一次盘
    except Exception: pass
    game.stop()                   # _stop.set() + engine thread join(timeout=2.0)
                                  # brain.shutdown(wait=False)  # 不等 LLM 调用完成
    pygame.quit()
```

`Game.stop()` 设置 `_stop` Event 让 `run_forever` 退出循环，`join(timeout=2.0)` 等引擎线程结束；`brain.shutdown(wait=False)` 不等待进行中的 LLM 调用，因为 daemon 线程池会在主进程退出时被强制回收。`save_step` 用 try/except 包住，避免最后落盘失败阻塞退出。

---

## 附录：关键常量速查

下表汇总 `config.py` 中所有影响行为的常量，便于排查问题：

| 常量 | 值 | 用途 |
| --- | --- | --- |
| `TICK_DURATION_MS` | `16` | tick 节拍（~60 FPS） |
| `STEP_DURATION_MS` | `1000` | step 节拍（1 秒） |
| `MAX_TICKS_PER_STEP` | `600` | step 内最多 600 tick |
| `MAX_INPUTS_PER_STEP` | `32` | step 开头最多处理 32 input |
| `MAX_STEP_MS` | `10 * 60 * 1000` | step 最大时长（10 分钟） |
| `PATHFINDING_TIMEOUT_MS` | `60_000` | 寻路总超时 60s |
| `PATHFINDING_BACKOFF_MS` | `1000` | 寻路阻挡退避上限 1s |
| `CONVERSATION_DISTANCE` | `1.3` | 双方足够近算「到达」 |
| `MIDPOINT_THRESHOLD` | `4` | 距离 < 4 走对方位置，否则走中点 |
| `TYPING_TIMEOUT_MS` | `15_000` | typing 状态超时 15s |
| `COLLISION_THRESHOLD` | `0.75` | 玩家碰撞距离 |
| `MAX_HUMAN_PLAYERS` | `8` | 最多 8 个人类玩家 |
| `CONVERSATION_COOLDOWN_MS` | `15_000` | 对话后冷却 15s |
| `ACTIVITY_COOLDOWN_MS` | `10_000` | 活动后冷却 10s |
| `PLAYER_CONVERSATION_COOLDOWN_MS` | `60_000` | 玩家对话冷却 60s |
| `INVITE_ACCEPT_PROBABILITY` | `0.8` | agent 接受邀请概率 |
| `INVITE_TIMEOUT_MS` | `60_000` | walkingOver 超时 60s |
| `AWKWARD_CONVERSATION_TIMEOUT_MS` | `60_000` | 尴尬沉默超时 60s |
| `MAX_CONVERSATION_DURATION_MS` | `10 * 60_000` | 对话最长 10 分钟 |
| `MAX_CONVERSATION_MESSAGES` | `8` | 对话最多 8 条消息 |
| `INPUT_DELAY_MS` | `1000` | 输入延迟 1s |
| `NUM_MEMORIES_TO_SEARCH` | `3` | 检索 top-3 记忆 |
| `MESSAGE_COOLDOWN_MS` | `2000` | 消息冷却 2s |
| `AGENT_WAKEUP_THRESHOLD_MS` | `1000` | agent 唤醒阈值 |
| `HUMAN_IDLE_TOO_LONG_MS` | `5 * 60 * 1000` | 人类玩家 5 分钟无输入自动离开 |
| `MAX_PATHFINDS_PER_STEP` | `16` | step 内最多寻路 16 次 |
| `ACTION_TIMEOUT_MS` | `120_000` | agent 操作超时 120s |
| `ENGINE_ACTION_DURATION_MS` | `30_000` | 引擎动作时长 30s |
| `MOVEMENT_SPEED` | `0.75` | 角色速度 0.75 tile/s |
| `OPENAI_EMBEDDING_DIMENSION` | `1536` | OpenAI embedding 维度 |
| `OLLAMA_EMBEDDING_DIMENSION` | `1024` | Ollama embedding 维度 |
