# 对话路由 (Dialogue Router)

本文件专门描述 `ai_town_py` 的「对话路由」模块。对话路由是本 Python 版
AI Town 区别于原 Convex 版的核心改造点之一：它把原本隐式藏在 Convex
mutation / scheduler 里的「A 的话怎么到 B 那里」这一层显式抽出来，做成一
个一等公民模块。

涉及的核心源文件：

- `ai_town_py/dialogue/router.py` —— 路由主体（`DialogueRouter`、
  `MessageBus`、`RoutedMessage`、`AgentEndpoint`）
- `ai_town_py/engine/bootstrap.py` —— 启动时把所有 agent 注册到路由
  （`_register_agents_to_router`）
- `ai_town_py/agent_brain/agent_ops.py` —— agent 大脑在后台线程里调
  `router.deliver` 发消息（`_agent_generate_message`）
- `ai_town_py/main.py` —— 人类玩家注册到路由（`register_human_to_router`）
  以及把 `renderer.on_routed_message` 注册成观察者

---

## 1. 设计动机

原 AI Town（Convex 版）的 agent 之间并不直接互发消息。当 A 想说话给 B
时，流程是：

1. A 把消息写进 `messages` 表；
2. A 触发一次 `agentFinishSendingMessage` input；
3. B 在自己的下一个 tick 里通过 `previousMessages` query 拉取对话历史，
   再生成回复。

也就是说，「消息从 A 转给 B」这件事被 Convex 的 mutation / query /
scheduler 系统隐式承担了——没有哪一个模块叫做「路由器」。

本项目把这一层显式抽出来，做成一个一等公民模块 `DialogueRouter`，理由
有三：

1. **用户明确要求**：用户指出「所有的 agent 都要在对话路由注册，由对话
   路由来讲一个 agent 的话转发给另一个」。这意味着发送方与接收方必须
   解耦，发送方只认 `recipient`，不直接持有接收方的引用。

2. **集中式注册中心 + 派发器模式**：所有 agent（以及人类玩家）在启动时
   向路由注册自己（`player_id` → `AgentEndpoint`）。任何一方想给另一方
   发消息，只需要构造一条 `RoutedMessage` 交给 `router.deliver`，路由负
   责落盘 + 通知接收方 + 通知观察者。发送方无需关心接收方在哪个线程、
   是否还在线。

3. **可测、可扩展、可观察**：把这一层显式化之后，可以单独对
   `DialogueRouter` 写单元测试（mock 掉 `Database` 与 handler）；可以在
   `deliver` 上加一层做消息过滤 / 审计 / 限流；可以通过 `add_observer`
   不侵入地接入新的下游（如调试日志、metrics）。

落到代码层面，`agent_brain` 只需要一行：

```python
self._router.deliver(RoutedMessage(
    conversation_id=..., author=alice, recipient=bob,
    text=..., message_uuid=..., timestamp=...,
))
```

就能完成「A 的话转发给 B」，无需关心 B 在哪个线程、是否在线。

---

## 2. 核心抽象

`ai_town_py/dialogue/router.py` 定义了四个核心抽象。

### 2.1 `RoutedMessage`

一条被路由转发的消息，是个纯数据类：

| 字段                | 类型 | 含义                                   |
| ------------------- | ---- | -------------------------------------- |
| `conversation_id`   | str  | 所属对话 id                            |
| `author`            | str  | 发送者 playerId                        |
| `recipient`         | str  | 接收者 playerId                        |
| `text`              | str  | 消息正文                               |
| `message_uuid`      | str  | 消息唯一 id（去重 / 幂等用）           |
| `timestamp`         | int  | 毫秒时间戳                             |
| `leave_conversation`| bool | 是否同时表示「我要退出对话」，默认 False |

注意 `author` / `recipient` 用的是 **playerId** 而非 agentId，这样路由
表里 agent 与人类玩家可以统一寻址（见 §3）。

### 2.2 `AgentEndpoint`

一个 agent（或人类玩家）在路由里的注册条目：

| 字段                     | 类型                                          | 含义                                                          |
| ------------------------ | --------------------------------------------- | ------------------------------------------------------------- |
| `agent_id`               | str                                           | 对应的 agentId（人类玩家用 `human-{player_id}`）              |
| `player_id`              | str                                           | 寻址主键（路由表按它索引）                                    |
| `name`                   | str                                           | 显示名，调试日志用                                            |
| `handler`                | `Callable[[RoutedMessage], None]`             | 收到「发给自己的消息」时被调用，在路由线程内同步执行          |
| `on_conversation_started`| `Optional[Callable[[str, str, str], None]]`   | 对话开始回调，签名为 `(conv_id, player_a, player_b)`          |
| `on_conversation_ended`  | `Optional[Callable[[str], None]]`             | 对话结束回调，签名为 `(conv_id,)`                             |

### 2.3 `MessageBus`

低层消息总线，只管「持久化」与「读历史」两件事，不碰派发逻辑：

- `persist(msg) -> int`：调用 `Database.insert_message`，把消息落盘到
  SQLite 的 `messages` 表（字段：world_id、conversation_id、author、
  text、message_uuid），返回新生成的 `message_id`。
- `list_history(conversation_id) -> List[Dict]`：调用
  `Database.list_messages`，返回某对话的历史消息列表。

`Database` 自带一把独立的 `RLock`，所以 `MessageBus` 自己不再加锁。

### 2.4 `DialogueRouter`

高层路由器，对外暴露的全部能力：

| 方法                                                          | 作用                                            |
| ------------------------------------------------------------- | ----------------------------------------------- |
| `register(endpoint)` / `unregister(player_id)`               | 注册 / 注销一个端点                             |
| `get_endpoint(player_id)` / `list_registered()`              | 查询端点 / 列出所有已注册 playerId              |
| `add_observer(callback)`                                      | 订阅全部路由消息（见 §6）                       |
| `on_conversation_started(conv_id, a, b)`                     | 对话开始事件（见 §5）                           |
| `on_conversation_ended(conv_id)`                             | 对话结束事件（见 §5）                           |
| `deliver(msg) -> message_id`                                 | 转发一条消息（见 §4）                           |
| `conversation_history(conv_id)`                              | 查询对话历史（委托给 `MessageBus`）             |

---

## 3. 注册流程

注册发生在两个地方。

### 3.1 agent 注册（`engine/bootstrap.py: _register_agents_to_router`）

`build_game` 在装配完 `Game` 与 `AgentBrain` 之后，调用
`_register_agents_to_router(game)`，遍历 `game.world.agents.values()`：

- 对每个 agent，取出其 `player`、`player_description`，拼出 `name`；
- 用闭包 `make_handler(aid=agent.id, pid=agent.player_id)` 构造一个
  `handler`，闭包内目前只做日志：
  ```python
  print(f"[router] {aid} ({pid}) received from {msg.author}: {msg.text[:40]}")
  ```
  这是因为 agent 并不在 handler 里立即生成回复，而是在下一个 tick 通过
  `conversation.last_message` 自动感知到这条消息，从而触发回复生成（见
  §8）。handler 这里仅用于调试 / 即时反应。
- 调 `game.dialogue_router.register(AgentEndpoint(...))` 注册。

闭包用默认参数 `aid=agent.id, pid=agent.player_id` 捕获循环变量，避免
Python 闭包晚绑定陷阱。

### 3.2 人类玩家注册（`main.py: register_human_to_router`）

`run()` 在创建人类玩家 `viewer_id` 之后，调用
`register_human_to_router(game, viewer_id, pdesc.name, renderer)`：

- 构造一个 **no-op handler**（`def handler(msg): pass`）。人类玩家收到
  agent 的消息时不需要在这里做任何事，因为渲染器已经作为观察者把气泡画
  出来了（见 §6、§9）。
- 注册 `AgentEndpoint`，`agent_id` 取 `f"human-{player_id}"`，
  `player_id` 取人类玩家的 playerId。

### 3.3 为什么按 `player_id` 而不是 `agent_id` 索引

路由表 `self._endpoints: Dict[str, AgentEndpoint]` 的 key 是
`player_id`。这是因为 `RoutedMessage.author` / `recipient` 也用
`player_id`（agent 的 playerId 和人类玩家的 playerId 同处于一个命名空
间）。这样一条 `RoutedMessage` 可以统一寻址到 agent 端点或人类玩家端
点，路由代码无需区分对方是 agent 还是人类。

---

## 4. 消息转发流程

`DialogueRouter.deliver(msg)` 是整个模块的「主方法」，逐步走查：

```
def deliver(self, msg: RoutedMessage) -> int:
    # Step 1: 落盘
    msg_id = self._bus.persist(msg)

    # Step 2: 在锁内做快照（拿到当前 observers 与 recipient endpoint）
    with self._lock:
        observers = list(self._observers)
        recipient_ep = self._endpoints.get(msg.recipient)

    # Step 3: 通知所有全局观察者（渲染器用它画气泡）
    for obs in observers:
        try:
            obs(msg)
        except Exception:
            pass

    # Step 4: 调接收端点的 handler（同步，在调用方线程里）
    if recipient_ep is not None:
        try:
            recipient_ep.handler(msg)
        except Exception:
            pass

    return msg_id
```

要点：

1. **Step 1 — 持久化**：`MessageBus.persist` → `Database.insert_message`
   往 SQLite 的 `messages` 表 INSERT 一行，返回 `message_id`。落盘永远
   最先发生，即使后续观察者 / handler 全炸，消息也已经进了库，历史不会
   丢。
2. **Step 2 — 快照**：在 `self._lock`（RLock）内拷贝一份 observers 列
   表、取出 recipient 端点引用。锁只在这一步持有。
3. **Step 3 — 通知观察者**：在锁外，按注册顺序逐个调用 observer。渲染
   器的 `on_routed_message` 就在这一步被调用，把气泡画到说话者头顶。
4. **Step 4 — 派发给接收方 handler**：在锁外同步调用
   `recipient_ep.handler(msg)`。这个调用发生在 **调用 `deliver` 的那个
   线程**里——也就是 brain worker 线程（agent 发消息时）或 engine 主线
   程（人类玩家发消息时，见 §9）。
5. **异常隔离**：observer 与 handler 的异常都被 `try/except` 吞掉（不
   打日志、不再抛），确保一个坏的观察者 / handler 不能把路由主流程拖
   崩。
6. **返回值**：`message_id`，供调用方（如 brain）记录或回写。

---

## 5. 生命周期事件

路由还承担对话「开始 / 结束」两个事件的广播：

### 5.1 `on_conversation_started(conv_id, player_a, player_b)`

- 在 `self._lock` 内把 `conv_id -> (player_a, player_b)` 写进
  `self._conversations` 映射；
- 对 `player_a` 与 `player_b` 两个端点，若它们注册了
  `on_conversation_started` 回调，就调用之，签名为
  `(conversation_id, player_a, player_b)`；
- 回调异常被吞掉，不影响其他端点。

### 5.2 `on_conversation_ended(conv_id)`

- 在 `self._lock` 内 `pop` 出该对话的双方 pair；
- 若找到，对两个端点调用其 `on_conversation_ended` 回调，签名为
  `(conversation_id,)`；
- 回调异常被吞掉。

这两个事件让端点可以「事件驱动」地维护「我当前在和谁说话」的状态，而
不必轮询 `world.conversations`。当前 bootstrap 注册的 agent 端点没有
填这两个回调（`None`），属于预留扩展点。

---

## 6. 观察者模式

`add_observer(callback)` 把一个回调加进 `self._observers` 列表。每条经
过 `deliver` 的消息（无论 author / recipient 是谁）都会按注册顺序同步
调用全部 observer。

用途：

- **渲染器画气泡**：`main.py` 在启动时执行
  `game.dialogue_router.add_observer(renderer.on_routed_message)`。
  `Renderer.on_routed_message(msg)` 内部调用
  `self.add_bubble(msg.author, msg.text, msg.timestamp)`——注意锚点是
  `msg.author`（说话者），所以气泡画在说话者头顶，而不是接收者头顶
  （这点对人类玩家也成立，见 §9）。
- **调试 / 日志**：可以临时 `add_observer(lambda msg: print(...))` 看
  全部路由流量。
- **metrics / tracing**：见 §10。

特性：

- 允许多个 observer，按注册顺序同步调用；
- observer 异常被 `try/except` 吞掉，绝不向上抛，保证不影响其他
  observer 与后续的 handler 调用；
- observer 是「全局」的，不区分对话 / 房间，所有消息都收。

---

## 7. 线程安全

`DialogueRouter` 被 engine 主线程与 brain 后台线程共同访问，其线程安全
模型如下：

- **锁**：`self._lock = threading.RLock()`，**可重入**。所有公开方法
  （`register` / `unregister` / `get_endpoint` / `list_registered` /
  `add_observer` / `on_conversation_started` / `on_conversation_ended` /
  `deliver`）都在 `with self._lock` 内访问共享状态。
- **`deliver` 只在快照时持锁**：锁仅在 Step 2（拷贝 observers + 取
  recipient endpoint）期间持有，之后 **在调用 observer / handler 之前
  释放**。这是为了避免死锁：handler 或 observer 里如果反过来又调
  `router` 的某个方法（例如 handler 里再 `deliver` 一条），不持锁就不
  会自锁；即便持锁，RLock 的可重入性也能兜住同线程重入。
- **生命周期事件持锁期间调用回调**：`on_conversation_started` /
  `on_conversation_ended` 在 `with self._lock` 内调用端点回调。因为
  `RLock` 可重入，回调里若再调路由方法也不会死锁；回调异常被吞掉。
- **数据库有独立锁**：`Database` 自己持有一把 `RLock` 保护
  `insert_message` / `list_messages` 等操作，`MessageBus` 不再加锁，
  与 `DialogueRouter._lock` 互不影响。
- **并发场景**：`AgentBrain` 用 `ThreadPoolExecutor(max_workers=4)`
  起 4 个 brain worker 线程，它们会并发调用 `router.deliver`；engine
  主线程同时可能 `register` / `unregister` / 触发生命周期事件。RLock +
  快照模式保证这些并发访问安全。

---

## 8. 完整调用链示例

走一遍一个完整的对话回合：Agent A 给 Agent B 发一句 "Hi!"，并触发 B 的
回复。

1. **A 的 brain 决定发消息**：engine 主线程在某次 `agent.tick` 时把
   `agentGenerateMessage` 操作提交给 brain（`AgentBrain.submit`），由
   某个 brain worker 线程执行 `_agent_generate_message(args)`。
2. **生成文本**：`_agent_generate_message` 根据 `args["type"]`
   （`start` / `continue` / `leave`）调用
   `start_conversation_message` / `continue_conversation_message` /
   `leave_conversation_message`，内部调 `llm.chat_completion` 得到
   `text`。
3. **交给路由**：brain 调
   ```python
   self._router.deliver(RoutedMessage(
       conversation_id=conversation_id,
       author=player_id,           # A 的 playerId
       recipient=other_player_id,  # B 的 playerId
       text=text,
       message_uuid=message_uuid,
       timestamp=int(time.time() * 1000),
       leave_conversation=(msg_type == "leave"),
   ))
   ```
4. **`DialogueRouter.deliver` 内部**：
   - `MessageBus.persist(msg)` → SQLite INSERT，拿到 `message_id`；
   - 通知观察者：`renderer.on_routed_message(msg)` →
     `add_bubble(msg.author=A, ...)`，在 A 头顶画气泡；
   - 派发给 B 的 handler（bootstrap 里注册的那个）：打印
     `[router] {B_id} ({B_pid}) received from {A_pid}: Hi!`。
5. **路由返回** `message_id` 给 brain。
6. **brain 回写 finish input**：
   ```python
   self._on_finish("agentFinishSendingMessage", {
       "agentId": agent_id, "conversationId": conversation_id,
       "timestamp": ..., "operationId": operation_id,
       "leaveConversation": msg_type == "leave",
   })
   ```
   `on_finish` 实际是 `lambda name, args: game.enqueue_input(name, args)`
   （见 `bootstrap.build_game`），所以这一步是把 input 推进
   `Game._pending_inputs`。
7. **engine 主线程处理 input**：engine 主循环取出该 input，调
   `_agent_finish_sending_message`：清掉 `agent.in_progress_operation`，
   再调 `_finish_sending_message`，把
   `conv.last_message = {"author": A_pid, "timestamp": ...}`，
   `conv.num_messages += 1`，并清 `conv.is_typing`。
8. **下一 tick B 感知到该回话了**：在 Agent B 的下一次 `agent.tick`
   里，B 看到
   `conversation.last_message["author"] == A_pid`（不是自己），并且距
   离该消息时间戳已过 `MESSAGE_COOLDOWN_MS = 2000`（2 秒）冷却
   （`engine/agent.py` 里 `cooldown = conversation.last_message["timestamp"]
   + MESSAGE_COOLDOWN_MS`），于是又提交一个 `agentGenerateMessage`
   操作，循环回到第 1 步。

### ASCII 时序图

```
 brain worker (A)        DialogueRouter          MessageBus/DB        Renderer(obs)        B endpoint (handler)        engine main thread
      |                        |                       |                    |                       |                          |
      | llm.chat_completion    |                       |                    |                       |                          |
      |-------------> (LLM)    |                       |                    |                       |                          |
      |<----------- text="Hi!" |                       |                    |                       |                          |
      |                        |                       |                    |                       |                          |
      | deliver(RoutedMessage) |                       |                    |                       |                          |
      |----------------------->|                       |                    |                       |                          |
      |                        | persist(msg)          |                    |                       |                          |
      |                        |---------------------->|                    |                       |                          |
      |                        |<--------- message_id |                    |                       |                          |
      |                        | (snapshot obs+B ep)  |                    |                       |                          |
      |                        |--- obs(msg) -------->|                    |                       |                          |
      |                        |                       |                    | on_routed_message(msg)|                          |
      |                        |                       |                    |  add_bubble(A,"Hi!")  |                          |
      |                        |                       |                    |<--------              |                          |
      |                        |--- handler(msg) ---->|                    |                       |                          |
      |                        |                       |                    |                       | print("[router] B <- A") |
      |                        |                       |                    |                       |                          |
      |<------- message_id ----|                       |                    |                       |                          |
      |                        |                       |                    |                       |                          |
      | _on_finish("agentFinishSendingMessage", {...}) |                   |                       |                          |
      |--- game.enqueue_input -------------------------------------------------------------->|                          |
      |                        |                       |                    |                       |                          |
      |                        |                       |                    |                       |  _agent_finish_sending_message
      |                        |                       |                    |                       |   _finish_sending_message
      |                        |                       |                    |                       |    conv.last_message.author=A
      |                        |                       |                    |                       |    conv.num_messages++
      |                        |                       |                    |                       |                          |
   (A 完成)                    |                       |                    |                       |                          |
                                                                                                                          |
   ... 2s 冷却后 ...                                                                                                      |
   B.tick: last_message.author==A 且 over MESSAGE_COOLDOWN_MS -> 提交 agentGenerateMessage -> 循环回到顶部 (B 充当 A)
```

---

## 9. 人类玩家接入

人类玩家既是消息的发送方，也是接收方，两端接入方式不同。

### 9.1 人类玩家注册为接收方

`main.py: register_human_to_router` 创建一个 `AgentEndpoint`，
`player_id` = 人类玩家的 playerId，`name` = 人类玩家名，
`handler` = no-op（什么都不做）。

为什么 handler 是空函数？因为人类玩家「看到 agent 的话」靠的是渲染器
观察者：`renderer.on_routed_message(msg)` 用 `msg.author`（说话的
agent）作为气泡锚点把气泡画到 agent 头顶。人类玩家是通过屏幕看到这个
气泡的，不需要 handler 再做任何事。

### 9.2 人类玩家作为发送方

`main.py: _send_human_message` 不直接调 `router.deliver`，而是把消息
塞进 engine 的 input 队列：

```python
game.enqueue_input("sendHumanMessage", {
    "playerId": player_id,
    "conversationId": conv.id,
    "text": text,
    "messageUuid": str(uuid.uuid4()),
})
```

engine 主线程处理该 input 时（`engine/game.py: _send_human_message`）：

1. 找到对话里「另一方」的 playerId `other_pid`；
2. 构造 `RoutedMessage(conversation_id=conv_id, author=player_id,
   recipient=other_pid, text=text, message_uuid=..., timestamp=...)`；
3. 调 `game.dialogue_router.deliver(msg)`；
4. 调 `_finish_sending_message` 更新 `conv.last_message` /
   `conv.num_messages`。

所以人类玩家发消息时，`deliver` 是在 **engine 主线程**里被调用的；而
agent 发消息时，`deliver` 在 brain worker 线程里被调用。两者通过
`DialogueRouter` 的锁安全共存。

### 9.3 气泡锚点

`renderer.on_routed_message` 用 `msg.author` 作为气泡锚点。所以：

- agent 给人类玩家发消息 → 气泡画在 **agent 头顶**（人类通过屏幕看到
  agent 在说话）；
- 人类玩家给 agent 发消息 → 气泡画在 **人类玩家角色头顶**（agent 端
  也会收到 handler 调用，但当前 handler 是 no-op）。

---

## 10. 扩展点

模块设计上预留了若干扩展点：

- **自定义 handler**：直接传一个闭包给 `AgentEndpoint(handler=...)`，
  或子类化 `AgentEndpoint` 覆盖行为。例如可以让 handler 在收到消息时
  立刻把消息塞进 agent 的短期记忆。
- **换持久化后端**：子类化 `MessageBus`，覆盖 `persist` /
  `list_history`，把消息写进 Postgres / Kafka / 文件，路由其余逻辑不
  变。
- **消息过滤 / 审核**：包一层 `deliver`（或在 `MessageBus.persist` 前
  插入 moderation），对违禁词拦截或改写。
- **metrics / tracing**：`add_observer` 一个回调，把每条消息的
  `author` / `recipient` / `timestamp` / `len(text)` 上报到 metrics
  系统，零侵入。
- **多房间路由**：给 `RoutedMessage` 加 `room_id` 字段，给
  `DialogueRouter` 的 `_endpoints` 改成 `room_id -> {player_id ->
  endpoint}` 二级映射，`register` / `deliver` 按 `room_id` 作用域；观
  察者也可按房间订阅。
- **异步派发**：当前 `deliver` 同步调 handler。若 handler 变重，可在
  `deliver` 里把 handler 调用丢进一个专用线程池，路由主流程立刻返回
  （需自行处理背压与顺序）。
- **生命周期回调填充**：当前 bootstrap 注册的 agent 端点没填
  `on_conversation_started` / `on_conversation_ended`，可以在这里挂上
  「进入对话时初始化短期记忆 / 退出对话时触发记忆总结」的逻辑，避免
  agent 轮询 `world.conversations`。

---

## 11. 与原项目对照

| 维度          | 原 AI Town (Convex)                                       | 本项目 (Python)                                              |
| ------------- | --------------------------------------------------------- | ------------------------------------------------------------ |
| 消息落地      | Convex `messages` 表，由 mutation 写入                    | SQLite `messages` 表，由 `MessageBus.persist` 写入          |
| A→B 转发      | 隐式：A 写表 + 触发 `agentFinishSendingMessage` input；B 下一 tick 拉 `previousMessages` | 显式：A 调 `router.deliver(msg)`，路由同步派发给 B 的 handler |
| 注册中心      | 无（agent 之间靠 playerId 在数据库里间接互寻）            | 有：`DialogueRouter._endpoints` 按 playerId 注册            |
| 观察者        | 无一等概念（前端靠订阅 Convex query）                     | 有：`add_observer`，渲染器用它画气泡                         |
| 对话生命周期  | 散落在 `startConversation` / `leaveConversation` mutation | 路由统一收口：`on_conversation_started` / `on_conversation_ended` |
| 触发回复      | Convex scheduler 调度 `agentFinishSendingMessage`         | brain worker 线程 + `Game.enqueue_input` 回写，下一 tick 触发 |
| 派发线程模型  | 云端 mutation 调度                                         | 本地 `ThreadPoolExecutor(max_workers=4)` + RLock             |
| 测试 / 扩展   | 难（流程分散在 mutation / scheduler / query）             | 易（`DialogueRouter` 可单测，handler / bus / observer 均可替换） |

**取舍**：本项目多写了一个模块（`dialogue/router.py`，约 180 行），换
来的是：

1. 转发流程集中、可读、可画时序图；
2. 注册 / 注销 / 观察 / 生命周期事件都有显式 API，扩展无需改 engine；
3. 单元测试可以直接 `DialogueRouter(Database(":memory:"), "w")` 起一个
   实例，mock handler 与 observer，断言落盘与派发顺序；
4. 严格满足用户提出的需求——「所有的 agent 都要在对话路由注册，由对话
   路由来讲一个 agent 的话转发给另一个」。

代价是：相比 Convex 隐式流程，多了一层显式抽象，新增 agent 类型时需要
记得调 `register`（已在 `bootstrap._register_agents_to_router` 与
`main.register_human_to_router` 统一处理）。
