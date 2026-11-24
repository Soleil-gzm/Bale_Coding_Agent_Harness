# Bale Coding Agent Harness 架构设计

## 1. 文档目的与实现状态

本文同时记录两类内容：

- **当前实现**：B0 工程骨架、Turn/ToolResult 契约、Settings、ModelProvider/FakeProvider、共享 Turn 构造入口；B1 只读 ReAct 运行时（AgentLoop、ToolRegistry、只读内置工具、Workspace 路径防护与 `bale run` 入口）；B2a 写入运行时（`write_file`/`edit_file`/`shell`/`run_tests`/`git_diff`/`finish`、LocalSandbox、审批策略、ShadowGit 快照与调用幂等）；以及可独立运行的 FastMCP 文件写入示例。
- **目标架构**：Coding Agent Harness 的后续设计方向，用于约束模块边界和扩展方式。

除特别注明外，带有“规划”“目标”或“后续”标识的模块均未在当前仓库实现。文档中的目标接口是逻辑契约，不代表已有 Python 类或稳定 API。

## 2. 设计目标与边界

### 2.1 目标

目标运行时围绕以下闭环组织：

```text
理解仓库 → 构建上下文 → 模型决策 → 受控工具调用 → 修改与验证
→ 有预算地诊断重试 → 输出差异、测试和追踪证据 → 支持中断恢复
```

核心原则：

1. CLI 和 HTTP 等入口只创建统一任务，不分别实现 Agent 逻辑。
2. Agent Loop、工具治理、上下文和 Checkpoint 由项目自身维护。
3. 内置工具与 MCP 工具经过同一注册、校验、审批和超时流程。
4. 不可信代码、Shell 和测试只能在明确的执行边界中运行。
5. 检索只提供证据，不代替 Agent 完成推理与验证。
6. 每次执行都应有预算、状态、错误和可追踪结果。

### 2.2 当前非目标

当前实现不提供真实模型调用、Docker 容器隔离、多模型路由、多智能体调度、Web 前端、HTTP 审批流、Session/Resume 持久化或生产级隔离。B1 已提供只读 Turn 执行，B2a 已提供受审的本地写入与命令执行；后续实现也应避免在缺少证据时将上述未实现能力视为已经具备。`LocalSandbox` 只是受控开发环境的防护层，不是不可信代码的安全边界。

## 3. 当前实现架构

### 3.1 B0 核心契约与入口

```mermaid
flowchart LR
    CLI[bale turn] --> Request[TurnRequest]
    HTTP[POST /v1/turns] --> Request
    Request --> Factory[build_turn]
    Factory --> Turn[Created Turn]
    Settings[Settings] --> ProviderFactory[Provider Factory]
    ProviderFactory --> Fake[FakeProvider]
    ProviderFactory -->|openai| Error[Configuration or implementation error]
```

| 当前模块 | 职责 | 边界 |
|---|---|---|
| `contracts.py` | 定义 Turn、ToolResult、审批模式和状态类型 | 只校验数据，不调度执行 |
| `settings.py` | 读取 `BALE_` 配置、保护 Secret、校验 OpenAI Key | 不打印配置或密钥 |
| `providers.py` | 定义最小 ModelProvider、FakeProvider 和工厂 | 真实 OpenAIProvider 未实现 |
| `turns.py` | 校验入口输入并统一构造 `created` Turn | 不创建 workspace，不执行 Turn |
| `cli.py` | 将命令行参数转换为 TurnRequest 并输出 JSON | 初始化配置和 Provider，但不调用 `complete()` |
| `http.py` | 通过应用工厂暴露 `POST /v1/turns` | 无执行、Session、SSE、健康检查或持久化 |

CLI 和 HTTP 对相同输入生成相同的非 ID 字段；`turn_id` 和 `session_id` 分别随机生成。HTTP 环境在调用 `create_app()` 时加载，导入模块本身不读取环境。

### 3.2 B1 只读运行时与 B2a 写入运行时

B1 实现了最小 ReAct 闭环：模型请求工具调用 → 注册表治理执行 → 结果回注上下文 → 无工具调用时结束。B2a 在同一注册表上加入写入与执行工具：调用先经审批，`shell`/`run_tests` 交由 `LocalSandbox` 执行，每次迭代结束由 `ShadowGit` 在独立 git-dir 中快照，最终把 Diff 写入结果。

```mermaid
flowchart LR
    CLI[bale run] --> Loop[AgentLoop]
    Loop --> Provider[ModelProvider]
    Loop --> Registry[ToolRegistry]
    Registry --> Approval[PolicyApprover]
    Registry --> Tools[Built-in tools]
    Tools --> Guard[WorkspaceGuard]
    Tools --> Sandbox[LocalSandbox]
    Tools --> Shadow[ShadowGit]
    Loop -->|per-iteration snapshot| Shadow
    Loop --> Result[RunResult JSON]
```

审批语义：`auto_read` 只读放行、写/执行拒绝；`approve_write` 只读放行、写/执行需审批；`approve_all` 全部需审批。没有审批人时写/执行一律 `denied`，拒绝结果会回注模型而不是终止循环。`call_id` 对写/执行工具做幂等缓存，重复调用不会产生第二次副作用。

| 当前模块 | 职责 | 边界 |
|---|---|---|
| `agent/messages.py` | 运行时消息、工具调用、模型响应、用量与 ToolSpec 数据契约 | 仅描述数据，不执行 |
| `agent/security.py` | `WorkspaceGuard`：路径规范化、`..` 逃逸与符号链接逃逸防护 | 只读，不创建工作区 |
| `agent/tools/registry.py` | `Tool` 协议与 `ToolRegistry`：注册、参数校验、审批、调用幂等、超时、错误归一化、只读并发 | 写/执行工具无审批人时一律拒绝 |
| `agent/tools/builtin.py` | `read_file`、`list_files`、`search_files`，输出有界 | 全部只读且并发安全 |
| `agent/approval.py` | `PolicyApprover`：按 `auto_read`/`approve_write`/`approve_all` 判定，缺省拒绝 | 只做决策，不负责交互或持久化 |
| `agent/sandbox.py` | `Sandbox` 端口与 `LocalSandbox`：argv 执行、命令 deny-list、环境变量白名单、cwd 限定、超时与输出截断 | 非安全边界，Docker 适配待 B2b |
| `agent/checkpoint.py` | `ShadowGit`：独立 git-dir 的基线快照、逐迭代快照、diff 与恢复 | 不读写用户 `.git` |
| `agent/tools/write.py` | `write_file`、`edit_file`：原子写与精确匹配前置校验 | 写副作用，需审批 |
| `agent/tools/exec.py` | `shell`、`run_tests`：拒绝 shell 元字符，交由 Sandbox 执行 | 执行副作用，需审批 |
| `agent/tools/git.py` | `git_diff` 读取 ShadowGit 变更；`finish` 作为完成信号 | 均为只读 |
| `agent/context.py` | 装配系统前缀与当前用户消息 | 历史裁剪与检索装配留待后续 |
| `agent/loop.py` | ReAct 循环、终止/取消/迭代预算、调用 ID、逐迭代快照、用量与 Diff 聚合 | 无 Session/Resume 或 Trace 持久化 |
| `cli.py` `run` | 构造 Turn 并执行循环，输出 `RunResult` JSON | 退出码：`completed` 为 0，否则为 1 |

`ModelProvider.complete` 已演进为接收消息列表与工具定义、返回结构化 `ModelResponse`；`FakeProvider` 支持脚本化响应队列，使循环测试完全离线且确定。真实 OpenAI Provider 仍未实现。

### 3.3 MCP 分层视图

```mermaid
flowchart LR
    Client[MCP Client] -->|JSON-RPC over stdio| Server[FastMCP WriteServer]
    Server --> Tool[write_file]
    Tool --> Name[Timestamp filename]
    Name --> FS[Local output directory]
    Tool -->|Text result| Server
    Server -->|MCP response| Client
```

| 层 | 当前组件 | 职责 |
|---|---|---|
| 协议入口 | `FastMCP("WriteServer")` | 注册工具并通过 `stdio` 接收 MCP 请求 |
| 工具层 | `write_file` | 接收字符串、生成文件名、执行写入并返回文本 |
| 文件适配 | `os`、`open` | 创建输出目录并以 UTF-8 写入 |
| 运行环境 | Python 进程 | 提供当前工作目录、系统时间和文件系统权限 |

核心契约位于 `src/bale_harness/`，MCP 示例保持在 `examples/` 且不与运行时模块共享全局配置。当前没有应用执行层或持久化层。

### 3.4 MCP 模块职责

实现位于 `examples/mcp/write_server.py`：

| 组件 | 行为 | 边界 |
|---|---|---|
| `mcp` | FastMCP 服务实例 | 当前只注册一个工具 |
| `OUTPUT_DIR` | 固定为 `./output` | 相对于启动进程的工作目录 |
| `write_file(content)` | 异步 MCP 工具 | 不支持调用方指定路径 |
| 时间戳命名 | `note_YYYYMMDD_HHMMSS.txt` | 秒级精度，可能发生同秒覆盖 |
| 错误处理 | 捕获文件操作异常并返回文本 | 尚未归一化为结构化错误 |

### 3.5 MCP 数据流

```mermaid
sequenceDiagram
    participant C as MCP Client
    participant S as WriteServer
    participant T as write_file
    participant F as File System

    C->>S: 启动子进程并初始化 stdio session
    C->>S: list_tools
    S-->>C: write_file 与输入 Schema
    C->>S: call_tool("write_file", {content})
    S->>T: 调用异步工具
    T->>T: 生成秒级时间戳文件名
    T->>F: 以 UTF-8 写入 ./output
    F-->>T: 成功或异常
    T-->>S: 结果文本
    S-->>C: MCP CallToolResult
    C->>S: 关闭 session 与子进程
```

`stdio` 同时承载协议消息，因此服务代码不应向标准输出写入普通日志。客户端应负责关闭会话并回收子进程。

## 4. 目标系统分层架构

当前已实现 CLI/HTTP 到 Turn 的构造路径。下图中 Turn 之后的 Agent Runtime 及其内部模块均为后续设计，不是当前目录结构或已有能力。

```mermaid
flowchart TB
    CLI[CLI Adapter] --> Turn[Turn Factory]
    HTTP[HTTP Adapter] --> Turn
    Turn --> Runtime[Agent Runtime]

    subgraph RuntimeCore[Runtime Core]
        Loop[AgentLoop]
        Context[ContextBuilder]
        History[HistoryTrimmer]
        Registry[ToolRegistry]
        Approval[Approval Service]
        Session[Session Service]
        Checkpoint[Checkpoint Service]
        Trace[Trace and Cost Ledger]
    end

    Runtime --> Loop
    Context --> Loop
    History --> Context
    Loop --> Registry
    Registry --> Approval
    Approval --> Builtin[Builtin Tools]
    Approval --> MCP[MCP Tool Adapter]
    Builtin --> Sandbox[Sandbox Port]
    MCP --> Sandbox
    Repo[RepoIndex Retriever] --> Context
    Knowledge[Knowledge Retriever] --> Context
    Provider[ModelProvider Port] --> Loop
    Loop --> Checkpoint
    Loop --> Session
    Loop --> Trace
```

| 层 | 目标职责 | 关键约束 |
|---|---|---|
| 入口适配层 | CLI、HTTP、取消和审批请求 | 入口极薄，共享同一 Turn 契约 |
| Runtime 层 | Loop、预算、状态、恢复 | 不依赖具体模型、工具或 Web 框架 |
| 上下文层 | 历史、检索、工具输出和 Token 预算 | 当前用户消息最后，数据与指令分离 |
| 工具治理层 | 注册、Schema、权限、审批、超时、错误归一化 | 所有副作用工具必须经过 Registry |
| 执行适配层 | Local、Docker 或远程 Sandbox | 隔离失败时不得静默降级到宿主执行 |
| 检索适配层 | RepoIndex 与外部知识库 | 统一返回 SearchHit，受上下文预算控制 |
| 状态与观测层 | Session、Checkpoint、Trace、Usage | 支持恢复、审计和成本核算 |

## 5. 目标模块划分

| 规划模块 | 核心职责 | 主要依赖方向 |
|---|---|---|
| `agent/turn.py` | 执行期恢复信息与后续状态扩展 | 当前 B0 Turn 契约 |
| `agent/loop.py` | ReAct、终止、取消、重试预算、调用 ID | Provider、Registry、Checkpoint |
| `agent/context.py` | 稳定前缀、检索装配、TokenBudget | Retriever、History |
| `agent/history.py` | 按完整 Turn 裁剪并维持消息配对 | 领域消息模型 |
| `agent/tools/registry.py` | 注册、校验、并发、超时、错误归一化 | Tool 端口、Approval、Trace |
| `agent/tools/builtin.py` | 读写、检索、测试、Diff 等内置工具 | Sandbox、Workspace |
| `agent/tools/mcp.py` | MCP list/call、命名空间和生命周期 | MCP SDK、Registry |
| `agent/security.py` | 路径、能力、环境变量和不可信数据边界 | Workspace、Approval |
| `agent/checkpoint.py` | 独立快照、撤销、恢复和幂等 | Git、Session |
| `sandbox/` | 本地、容器和远程执行适配 | 统一 Sandbox 端口 |
| `repo_index/` | AST 切块、词法索引和行号引用 | Retriever 端口 |
| `providers/` | 真实模型调用、结构化输出和用量 | 当前 ModelProvider 端口 |
| `server/` | HTTP 执行、Session、审批和健康检查 | Runtime、当前 Turn 构造入口 |
| `tracing/` | Span、错误、Token、延迟和成本 | 无 Runtime 反向依赖 |
| `eval/` | 固定任务、确定性验证和报告 | CLI、Workspace、Verifier |

这些路径用于说明职责边界。B1 已实现 `agent/loop.py`、`agent/context.py`（最小前缀）、`agent/tools/registry.py`、`agent/tools/builtin.py`（只读工具）和 `agent/security.py`；B2a 已实现 `agent/approval.py`、`agent/sandbox.py`（LocalSandbox）、`agent/checkpoint.py`（ShadowGit）、`agent/tools/write.py`、`agent/tools/exec.py` 与 `agent/tools/git.py`；其余模块仍为规划，不能当作已有能力。后续实现时应以实际代码和测试为准更新本文。

## 6. 核心逻辑契约

### 6.1 Turn

| 字段组 | 当前实现 |
|---|---|
| 身份 | `turn_id`、`session_id` 为 UUID，默认分别使用 `uuid4` |
| 输入 | 非空且去除首尾空白的 `message`、`workspace: Path`、可选 `repo_revision` |
| 预算 | `max_iterations=20` 且至少为 1、`token_budget > 0`、`cost_budget=None` 且设置时非负 |
| 权限 | `auto_read`、`approve_write`、`approve_all`，默认 `approve_write` |
| 状态 | `created`、`running`、`waiting_approval`、`completed`、`failed`、`cancelled`，默认 `created` |

Turn 禁止未知字段并支持 JSON 序列化/反序列化。构造 Turn 不会创建 workspace；时间预算、Checkpoint、已执行调用 ID 和恢复位置仍是后续扩展。

CLI 与 HTTP 通过唯一的 `build_turn()` 构造 Turn，避免入口行为不一致。审批模式和后续状态当前只是数据契约，不表示已有审批或状态机执行逻辑。

### 6.2 Tool 与 ToolResult

当前 `ToolResult` 数据契约包含：

- `call_id`、工具名称和 `success | error | timeout | denied | cancelled` 状态；
- 文本内容、可选结构化错误码和非负 `latency_ms`；
- `truncated=False` 和默认独立字典 `metadata`；
- 未知字段拒绝与 JSON 序列化/反序列化。

B1 已实现 `ToolSpec`（名称、描述、parameters、副作用类型、并发安全标志、超时）与 `ToolRegistry`，提供参数校验、超时、错误归一化和只读并发调度；当前仅注册只读工具。写入和执行工具、审批与幂等检查仍为后续规划。调度规则是：只有标记为只读且并发安全的工具可以并行执行；写入和执行工具默认串行。

### 6.3 SearchHit

检索结果统一携带内容、相似度或排序分数、来源和元数据。代码检索还应提供路径、起止行号和 revision；知识库检索可提供 chunk/document 标识。分数不能直接解释为答案置信度。

## 7. 目标核心流程

### 7.1 Turn 主流程

1. 加载 Session、工作区、能力范围和预算。
2. 装配稳定系统前缀、历史、当前任务和检索证据。
3. ModelProvider 返回助手消息或工具调用。
4. Registry 完成参数校验、权限检查、审批、幂等检查和超时控制。
5. 工具结果按不可信数据包装后进入下一轮上下文。
6. 每次迭代保存状态、追踪信息和必要 Checkpoint。
7. 无工具调用时执行任务完成判定和确定性验证。
8. 返回答案、Diff、测试结果、状态和 Trace 标识。

最大迭代、预算耗尽、取消、审批超时或不可恢复错误都不是任务成功。

### 7.2 工具治理流程

```text
查找工具 → 参数转换与 Schema 校验 → Workspace/capability 检查
→ 调用 ID 幂等检查 → 按策略审批 → 选择执行适配器
→ 超时与输出限制 → 错误归一化 → Trace → ToolResult
```

任何失败都不能退化为绕过 Registry 的宿主执行。

### 7.3 上下文与恢复

目标预算公式：

```text
available_history = context_window
                  - reserved_output
                  - reserved_tools
                  - reserved_system
                  - reserved_current
```

所有扣减后预算最小为零。超预算时先压缩检索和工具输出，再裁剪历史，最后处理长期记忆；裁剪必须维持工具调用与结果配对。恢复时只注入必要恢复块，已完成的副作用调用不得重复执行。

## 8. 技术栈选型说明

### 8.1 当前实际技术栈

| 技术 | 选择原因 | 限制 |
|---|---|---|
| Python 3.11+ | 与当前包元数据和异步契约对齐 | 本地仅验证 3.11.18 |
| Pydantic v2、pydantic-settings | 严格数据校验、环境配置和 Secret 保护 | 配置加载不代表真实 Provider 可用 |
| Typer、FastAPI、Uvicorn | 共享 TurnRequest 与 build_turn | 只创建 Turn，不执行 |
| `asyncio`、Protocol | 统一最小异步 Provider 契约 | FakeProvider 不访问网络 |
| FastMCP、`mcp==1.30.0` | 用少量代码声明示例工具和 Schema | 不等同于完整 Agent 工具治理 |
| MCP `stdio` | 本地进程集成简单、无需开放端口 | 客户端必须管理进程生命周期 |
| pytest、Ruff、pip-tools | 离线测试、静态检查和可复现安装 | 本地通过不等于远程 CI 已运行 |
| 本地文本文件 | MCP 示例无需外部服务 | 不提供并发、权限或持久化保证 |

### 8.2 目标技术栈

| 领域 | 建议选型 | 选择理由 |
|---|---|---|
| 语言与契约 | Python 3.11+、Pydantic v2 | 类型化配置、契约和异步运行时 |
| CLI / HTTP | Typer、FastAPI | 共享 Turn，分别提供本地与服务入口 |
| 模型 | OpenAI-compatible Provider、FakeProvider | 统一接口并支持离线测试 |
| 状态 | SQLite | 轻量、可恢复、低部署成本 |
| 工具协议 | 官方 MCP Python SDK | 与当前示例保持同一生态 |
| 代码检索 | Python AST、BM25 | 结构定位和确定性对照实验 |
| 隔离执行 | DockerSandbox | 非 root、无网、资源限额和清理 |
| 观测 | JSONL Trace、Usage/Cost Ledger | 无外部依赖即可审计 |
| 测试 | pytest、pytest-asyncio、ruff | 单元、契约、集成和静态检查 |

目标选型只有在相应模块和验证落地后才成为项目实际依赖。

## 9. 扩展点设计

当前 `ModelProvider` 定义为 `async complete(messages, tools) -> ModelResponse`，FakeProvider 是支持脚本化响应队列的离线实现。以下接口方向用于降低后续实现耦合，其中 Tool 与 ModelProvider 已落地，其余不代表已经存在：

| 扩展点 | 调用方 | 实现方应提供的能力 |
|---|---|---|
| ModelProvider | AgentLoop | 消息与工具定义输入、结构化输出、Token 用量、取消 |
| Tool | ToolRegistry | Schema、副作用元数据、超时执行、结构化结果 |
| Sandbox | 内置工具与执行策略 | Workspace 隔离、进程控制、资源限制、清理 |
| Retriever | ContextBuilder | 按命名空间、过滤条件和 Top-K 返回 SearchHit |
| SessionStore | Session Service | append-only 消息、Turn 状态、恢复点 |
| CheckpointStore | AgentLoop | 快照、基线、撤销、调用 ID 幂等记录 |
| TraceExporter | Runtime | Span、错误、用量和成本导出 |
| Verifier | 完成任务流程 | 测试、文件、Diff 和禁止副作用检查 |

新增实现不应让 Runtime 直接依赖具体 SDK。外部 SDK 应停留在适配器内，并通过契约测试保证不同实现行为一致。

## 10. 安全与可靠性设计

### 10.1 当前示例注意事项

- 在受控目录中启动服务，并限制进程文件系统权限。
- 输出路径虽然固定，但同秒调用可能覆盖文件；重要数据不应使用该示例保存。
- MCP 客户端和服务端版本必须兼容，避免混用不同主版本的 API。
- 返回错误文本不等于结构化错误，调用方仍需检查文件是否实际生成。

### 10.2 目标运行时控制

| 风险 | 设计控制 |
|---|---|
| 路径逃逸 | canonical path、Workspace allowlist、符号链接检查 |
| 不可信命令 | Sandbox、非 root、无外部网络、资源限额和超时 |
| 密钥泄漏 | 环境变量白名单、日志脱敏、最小挂载 |
| 不可信工具内容 | 数据与指令分离、长度限制、写前审批 |
| 重复副作用 | 调用 ID、原子写、前置内容校验、Checkpoint |
| MCP 服务故障 | 服务隔离、命名空间、超时和资源清理 |

本地执行只能用于受控开发场景，不能作为不可信代码的安全边界。

## 11. 测试与验证设计

当前 pytest 覆盖契约、配置、FakeProvider、CLI/HTTP 同构、输入校验，以及 B1 的消息契约、路径防护、工具注册器、只读内置工具、ReAct 循环和 CLI run 端到端，和 B2a 的审批策略、Sandbox 防护、ShadowGit 快照、写入/编辑与执行工具、幂等与「写代码→跑测试→finish」闭环；MCP 示例可通过客户端执行初始化、`list_tools` 和 `call_tool`，并检查输出文件内容。基础检查命令为：

```bash
ruff check src tests
ruff format --check src tests
pytest -q
python -m compileall examples
```

仓库已配置 Python 3.11 GitHub Actions 工作流；远程运行状态应以 Actions 实际结果为准。后续完整运行时应分层验证：

1. **单元测试**：预算、裁剪、注册、路径、错误和状态机。
2. **契约测试**：Provider、Tool、Retriever、Sandbox、Store 的不同实现。
3. **集成测试**：MCP 生命周期、Sandbox 隔离、Checkpoint 和 Session 恢复。
4. **端到端测试**：只读理解、代码修改、失败重试、取消恢复和安全拒绝。
5. **确定性验证**：pytest 退出码、文件断言、Diff 和副作用检查。

真实外部服务不可用时，应明确标记对应测试跳过，不能以 Fake 结果代替真实集成结论。

## 12. 演进约束

- 先建立契约、Fake 和测试，再接入真实模型或执行环境。
- 优先完成最小只读闭环，再增加写入、隔离、恢复和观测。
- 每次扩展保持入口、Runtime 和 Adapter 的依赖方向不变。
- 新模块落地后同步更新实现状态、快速启动和公开接口文档。
