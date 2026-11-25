# Bale Coding Agent Harness

Bale Coding Agent Harness 是一个面向 Coding Agent 运行时的项目，目标是将模型交互、工具治理、上下文管理、隔离执行、状态恢复和执行证据组织为可扩展的薄内核。

**当前仓库处于早期建设阶段。** 已落地 B0 工程基础、B1 只读 ReAct 运行时、B2a 写入运行时、B3 上下文/会话运行时、B4 追踪/成本运行时、B5 运行报告、B6 本地闭环增强、B7 HTTP 执行/SSE、B8 检索/评测和 B9 MCP 工具接入：可安装 Python 包、核心数据契约、配置校验、离线 FakeProvider 与 OpenAI 兼容 Provider、只创建 Turn 的 CLI/HTTP 入口，带审批、ShadowGit 快照和本地 Sandbox 的 `bale run`，Token 预算、wall-clock 时间预算、历史裁剪、SQLite Session 与取消恢复，JSONL Trace 与可选成本估算，基于 Session/Trace 的 Markdown 报告，持久化 Checkpoint 与 `bale undo`，`bale chat` 多轮交互，`bale session`/`bale trace` 检查命令，`move_file`/`delete_file`/`apply_patch` 写入工具，执行 Turn 的 `POST /v1/runs` 与 SSE 流式 `POST /v1/runs/stream`，以及 BM25 代码检索（`search_code`、`bale search`，可选向量混合与增量快照）、固定任务评测（`bale eval`）和 stdio MCP 工具适配（`bale run --mcp-config`、`bale chat --mcp-config`、HTTP `BALE_MCP_CONFIG`）。HTTP 入口另提供 `/health`、可选 Bearer 认证和按 `run_id` 的协作式取消；Python 文件按顶层定义切块，Agent Loop 会按用户消息自动注入检索片段。Docker 执行沙箱（每次工具调用独立容器）已落地，可用 `--sandbox docker` 或 `BALE_SANDBOX_BACKEND=docker` 启用；HTTP 审批、限流、SQLite Session 与审计也已落地。HTTP 服务本身已支持 Docker 部署。

## 文档

- [文档索引](docs/README.md)：按目标选择阅读路径。
- [快速启动](docs/quickstart.md)：安装、配置、CLI/HTTP/MCP 命令。
- [配置参考](docs/configuration.md)：配置优先级、环境变量与运行参数。
- [Docker 部署](docs/deployment.md)：镜像构建、Compose 运行、卷与安全默认。
- [架构设计](docs/architecture.md)：当前实现、核心契约与演进边界。
- [问题排查](docs/troubleshooting.md)：安装/运行/HTTP/MCP/检索/评测的常见失败。
- [安全模型](docs/security.md)：工作区、审批、执行、MCP 与 HTTP 的信任边界。
- [评测指南](docs/evaluation/README.md)：任务格式、报告字段与证据边界。

## 当前功能

### 核心契约与入口

- `Turn` 和 `ToolResult` 使用 Pydantic 校验，支持严格字段检查和 JSON 往返。
- `Settings` 读取 `BALE_` 环境变量；`openai` 模式缺少有效 Key 时立即报错（本地 Ollama 端点除外），密钥不会在 `repr` 中明文显示。
- `ModelProvider` 定义最小异步契约，`FakeProvider` 提供确定性离线响应并记录收到的 Turn；`OpenAICompatibleProvider` 通过标准库 `urllib` 调用任意 OpenAI 兼容 `/chat/completions` 端点（OpenAI、DeepSeek、Ollama），429/5xx 指数退避，错误信息自动脱敏。
- `bale turn` 与 `POST /v1/turns` 复用同一 `build_turn()` 构造逻辑，返回字段一致的 `created` Turn。
- `POST /v1/runs` 执行一个 Turn 并返回 `RunResult`；`POST /v1/runs/stream` 执行同一 Turn，以 SSE 推送 Trace 事件，最后发送 `result` 事件。
- CLI 和 HTTP 只创建 Turn，不调用模型或工具。

### 只读与写入运行时

- `ToolRegistry` 统一处理参数校验、审批、`call_id` 幂等、超时、错误归一化和只读并发。
- 只读工具：`read_file`、`list_files`、`search_files`、`git_diff`；写入工具：`write_file`、`edit_file`、`move_file`、`delete_file`、`apply_patch`；执行工具：`shell`、`run_tests`；`finish` 显式结束任务。
- `WorkspaceGuard` 阻止 `..` 与符号链接逃逸；写入采用临时文件加原子替换；`apply_patch` 要求每条旧文本恰好匹配一次，全部成功才写盘。
- `PolicyApprover` 支持 `auto_read`、`approve_write`、`approve_all`，无审批人时写/执行一律拒绝。
- `LocalSandbox` 提供命令 deny-list、环境变量白名单、cwd 限定、超时和输出截断；它不是不可信代码的安全边界。
- `DockerSandbox` 为每条命令启动独立容器：工作区挂载到 `/workspace`，默认无网络、只读根文件系统、非 root、CPU/内存/PID 限额，超时或取消时强制清理；通过 `--sandbox docker` 或 `BALE_SANDBOX_BACKEND=docker` 启用。
- `ShadowGit` 使用独立 git-dir 做基线与逐迭代快照，输出相对基线的 Diff，不读写工作区自己的 `.git`。
- `bale run` 驱动上述闭环，输出包含 `diff` 与 `checkpoints` 的 `RunResult` JSON。

### 上下文、会话与恢复

- `ContextBuilder` 生成 Turn 内稳定的系统前缀，并可注入上一条 completed Turn 的记忆摘要。
- `TokenBudget` 与 `HistoryTrimmer` 按整 Turn 裁剪历史，压缩超长工具输出，同时保持 assistant 工具调用与 tool 结果配对。
- `SessionStore` 使用标准库 SQLite 追加保存消息、Turn 状态、已执行调用和 `after_turn` 记忆；`bale run --session-db ... --session-id ...` 启用持久化。
- 取消或中断后，`bale run --resume` 复用原始 user 消息继续执行；已提交的写操作不会重复执行，结果未知的调用会以 `outcome_unknown` 回注模型。
- `BALE_CONTEXT_WINDOW` 控制上下文窗口（默认 128000）；`turn.token_budget` 控制本 Turn 累计 token 上限，`turn.time_budget` 控制 wall-clock 秒数上限（`--time-budget` / HTTP `time_budget`）。

### 追踪与成本

- `JsonlTraceExporter` 以追加方式记录 `turn_started`、`model_call`、`tool_call`、`checkpoint`、`turn_finished`，每条事件带序号、时间戳与耗时。
- 追踪默认脱敏：不写完整 Prompt、回答或工具参数，只写 token 数、消息数、工具名、状态和错误码。
- Trace 写入失败不会改变 Turn 结果；`bale run --trace path.jsonl` 启用，`BALE_TRACE_PATH` 可提供默认路径。
- 同时配置 `BALE_INPUT_COST_PER_1K` 与 `BALE_OUTPUT_COST_PER_1K` 后，`RunResult.estimated_cost` 给出估算成本；未配置时为 `null`。

### HTTP 执行与 SSE

- `POST /v1/runs` 在服务端执行一个 Turn，返回与 CLI 一致的 `RunResult`；`approve_writes` 默认 `false`，未开启审批时写/执行工具被拒绝。
- `POST /v1/runs/stream` 返回 `text/event-stream`：先发送 `event: run`（`run_id`），再逐条发送 `event: trace`，结束时发送 `event: result`；执行异常发送 `event: error`。
- `GET /health` 始终公开；设置 `BALE_HTTP_API_KEY` 或 `BALE_HTTP_API_KEYS` 后所有 `/v1` 路由要求 `Authorization: Bearer`，认证结果写入 `X-Bale-Principal`。
- 两个执行接口都返回 `X-Run-Id`；请求可自带 `run_id`，并用 `POST /v1/runs/{run_id}/cancel` 在下一个迭代边界协作取消，未知或已结束的 `run_id` 返回 404。
- `BALE_HTTP_SESSION_DB` 启用 SQLite Session：响应返回 `X-Session-Id`，请求可传 `session_id` 复用；同一会话的并发 Run 返回 409。
- `BALE_HTTP_APPROVALS=true` 时写/执行调用挂起，`GET/POST /v1/runs/{run_id}/approvals[/{approval_id}]` 用于查询与决定；超时、结束或取消按拒绝处理。
- `BALE_HTTP_RATE_LIMIT_PER_MINUTE` 按调用方限流（429 + `Retry-After`）；`BALE_HTTP_AUDIT_LOG` 追加运行与审批的 JSONL 审计。审批与限流状态在进程内，多副本不共享。

### 代码检索与评测

- `search_code` 在首次调用时扫描工作区（跳过隐藏目录、构建/缓存目录、二进制与超大文件），Python 文件按顶层定义切块、其余按 40 行窗口切块；默认用标准库 BM25 排序，配置 `BALE_EMBEDDING_MODEL` 后叠加 OpenAI 兼容向量召回并按 `BALE_RETRIEVAL_ALPHA` 融合，embedding 失败时自动退回 BM25。索引在工具实例内缓存，并可按文件签名增量重建。
- `bale search --workspace ... --query ...` 提供同一检索的 CLI 入口，`--limit` 限制命中数，`--json` 输出机器可读结果（含 `vector_search` 字段）。
- `bale run`/`bale chat`/HTTP 默认在每次 Turn 按用户消息检索，并把有限片段作为独立的 untrusted 段落注入系统前缀；`BALE_RETRIEVAL_*` 与 `BALE_EMBEDDING_*` 控制开关、条数、混合权重与索引快照。
- `bale eval --tasks examples/eval/tasks.json` 读取固定 JSON 任务，把每个 fixture 复制到临时工作区后运行完整 ReAct 闭环，再用 `file_exists`、`file_not_exists`、`file_contains`、`file_not_contains` 做确定性验证。
- 评测默认自动批准写与执行（fixture 视为可信）；`--output` 写 Markdown，`--json` 写 JSON，存在失败或错误时命令以非零状态退出，可用于 CI。

### 运行报告

- `bale report` 读取 SQLite Session 与可选 JSONL Trace，生成只读 Markdown 报告：会话概要、每个 Turn 的状态/用量/任务与答案、工具调用表、Trace 事件计数和记忆摘要。
- 报告不调用模型、不修改 Session；默认输出到标准输出，`--output` 可直接写入文件。

### Checkpoint 持久化与撤销

- 传入 `--session-db` 或显式 `--checkpoint-dir` 时，ShadowGit 的 git-dir 会保留在 `<session_db 父目录>/checkpoints/<session_id>`（若该路径位于 workspace 内则回退到系统临时目录）；不再随进程退出删除。
- 复用已有 git-dir 时以原 `HEAD` 作为基线，因此恢复后的 Diff 仍覆盖恢复之前的全部改动。
- `bale undo` 从持久化 Checkpoint 恢复工作区；默认回到基线，可用 `--snapshot` 指定某个快照，必须 `--yes` 或交互确认。

### 会话、追踪检查与多轮交互

- `bale session list` 按更新时间列出 Session 与 Turn 数；`bale session show` 展示某个 Session 的 Turn 明细；两者支持 `--json`。
- `bale trace` 汇总 JSONL Trace 的事件计数、模型耗时与工具耗时，`--limit N` 可追加最近 N 条事件原文。
- `bale chat` 在同一个 Session 上循环执行 Turn，输入 `/exit` 或 `/quit` 退出；跨 Turn 连续性依赖 Session 记忆，不携带完整对话历史。

### MCP 工具接入

- `bale run --mcp-config examples/mcp/servers.json` 读取 JSON 配置，启动 stdio MCP 服务、初始化会话并分页拉取工具。
- `bale chat --mcp-config ...` 在整个交互期间复用同一 MCP 会话；HTTP 通过 `BALE_MCP_CONFIG` 在应用 lifespan 内挂载，配置损坏时启动失败。
- 每个 MCP 工具被适配为 `mcp_<server>_<tool>`，与内置工具共享参数校验、审批、超时和错误归一化。
- MCP 工具默认按写副作用处理，需要审批或 `--yes`；服务器声明 `read_only=true` 后才作为只读工具参与并行执行。
- 配置项包括 `name`、`command`、`args`、`env`、`cwd`、`read_only` 和 `timeout_ms`；相对 `cwd` 以配置文件所在目录为基准。

### MCP 文件写入示例

- 使用 `FastMCP` 创建名为 `WriteServer` 的 MCP 服务，通过标准输入输出通信。
- 注册异步 `write_file(content: str)` 工具，将内容以 UTF-8 写入 `output/note_<时间戳>.txt`。
- 返回写入结果或错误文本，便于验证 MCP list/call 流程。

默认 Fake 配置不需要模型 API、数据库服务、Docker 或 GPU；`openai` 模式可连接 DeepSeek 或本地 Ollama。Session 使用标准库 `sqlite3`，ShadowGit 快照需要本机可用的 `git`。

## 功能边界

当前实现提供契约、Turn 构造入口和受控的本地执行闭环，不是完整的 Coding Agent：

- OpenAI 兼容 Provider 只实现非流式 Chat Completions（`temperature=0`），不支持流式输出、多模型路由或结构化输出参数。
- 默认 `LocalSandbox` 仅适用于受控开发场景；`DockerSandbox` 提供容器级隔离，但仍共享宿主内核与 Docker daemon。
- Session 持久化基于本地 SQLite 文件，不提供多进程协调或远程存储；HTTP 侧只支持单进程内复用。
- Checkpoint 目录已可持久化并支持 `bale undo`，但没有快照浏览/清理命令，也依赖本机 `git`。
- Trace 为本地追加 JSONL，无轮转、采样或远程导出；`estimated_cost` 只是按配置费率的估算，不代表 Provider 账单。
- 运行报告是只读 Markdown 投影，只提供 CLI 入口，不包含 HTML/PDF 导出或 HTTP 报告接口。
- 检索默认是无第三方依赖的 BM25 词法排序（Python 按顶层定义切块，其余按行窗口），可选叠加 OpenAI 兼容 embedding 的向量召回与按文件签名的增量索引；向量端点不可用时会退回 BM25。评测只执行 JSON 固定任务与文件级断言，不包含 Docker 隔离、自动生成任务或模型评分。
- MCP 只支持 stdio transport；`bale run` 每次重建会话，`bale chat` 与 HTTP 入口在各自生命周期内复用一个会话，不包含 SSE/HTTP transport、工具缓存、工具级审批界面或跨进程会话复用。`read_only` 是调用方声明，不代表服务器已被隔离。
- HTTP 接口提供 Bearer 认证（多 Key + `read`/`write` scope）、内存限流、SQLite Session、HTTP 审批、JSONL 审计、`run_id` 指定与协作式取消；没有 TLS、多副本共享状态或按工具/路径的 ACL，`approve_writes=true` 只应由可信调用方使用。
- 自动检索按用户消息在每次 Turn 注入有限片段，不感知对话历史，也不保证召回完整；检索异常时静默跳过，不阻断 Turn。
- `bale chat` 的跨 Turn 连续性依赖 Session 记忆摘要，不携带完整历史，也不会自动压缩超长对话。
- 审批在 CLI 中提供交互确认与 `--yes`，在 HTTP 中通过 `BALE_HTTP_APPROVALS` 与审批接口提供；两者都不判断命令内容是否安全。
- MCP 示例的文件名精度为秒，同一秒内的重复调用可能覆盖同名文件。
- MCP 输出目录相对于进程当前工作目录，不应将其误认为安全沙箱。

完整能力的分层设计与扩展方向见[架构设计](docs/architecture.md)。

## 技术栈

| 组成 | 当前选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 本地已在 Python 3.11.18 验证 |
| 数据与配置 | Pydantic v2、pydantic-settings | 严格契约、环境变量与 `.env` |
| CLI / HTTP | Typer、FastAPI、Uvicorn | 共享 Turn 构造逻辑 |
| Provider | Protocol、FakeProvider、OpenAI 兼容实现 | 最小异步接口、离线测试与真实端点 |
| 网络客户端 | 标准库 `urllib` | 无新增运行时依赖，支持 DeepSeek/Ollama |
| 工具治理 | ToolRegistry、PolicyApprover | 参数校验、审批、幂等、超时与错误归一化 |
| 本地执行 | LocalSandbox、ShadowGit | 子进程防护层与独立 git-dir 快照 |
| 上下文与会话 | ContextBuilder、HistoryTrimmer、SessionStore | Token 预算、整 Turn 裁剪与 SQLite 持久化 |
| 观测 | JsonlTraceExporter、CostRates | 追加式 JSONL 事件与可选成本估算 |
| 报告 | build_report、bale report | 从 Session/Trace 生成只读 Markdown |
| 检索与评测 | Bm25Index、HybridIndex、Bm25Retriever、EvalTask/EvalReport | 标准库 BM25、可选 embedding 混合排序、增量 JSON 快照、按 Turn 注入与确定性文件断言 |
| 工具协议 | Model Context Protocol | MCP 客户端与服务端均使用 `stdio` transport |
| SDK | `mcp==1.30.0` | 服务端使用 `FastMCP`，运行时使用 `ClientSession` 适配工具 |
| 测试与检查 | pytest、Ruff、pip-tools | 离线测试、静态检查与哈希锁文件 |
| 存储 | SQLite、本地文件系统 | Session 使用标准库 `sqlite3`；MCP 示例写入 `output/` |

## 项目结构

```text
Bale_Coding_Agent_Harness/
├── src/bale_harness/
│   ├── contracts.py          # Turn、ToolResult 与状态类型
│   ├── settings.py           # BALE_ 配置与密钥校验
│   ├── providers.py          # ModelProvider、FakeProvider 与工厂
│   ├── providers_openai.py   # OpenAI 兼容 Provider（DeepSeek/Ollama）
│   ├── py.typed              # PEP 561 类型标记
│   ├── turns.py              # TurnRequest 与共享 build_turn()
│   ├── cli.py                # bale turn/search/eval/run/chat/report/session/trace/undo
│   ├── http.py               # /v1/turns、/v1/runs(/stream)、审批、取消、/health
│   ├── http_support.py       # API Key/principal、限流、审批 Broker、审计
│   ├── repo_index/           # BM25/向量混合检索、AST 切块、增量快照与注入
│   ├── eval/                 # 固定任务、运行器与 Markdown/JSON 报告
│   └── agent/                # ReAct 运行时
│       ├── messages.py       # 运行时消息与工具契约
│       ├── security.py       # WorkspaceGuard 路径防护
│       ├── history.py        # Token 估算与整 Turn 裁剪
│       ├── context.py        # 稳定前缀、预算与记忆装配
│       ├── session.py        # SQLite Session 与恢复状态
│       ├── memory.py         # after_turn 记忆摘要
│       ├── trace.py          # JSONL Trace 事件与导出器
│       ├── cost.py           # 成本费率与估算
│       ├── report.py         # Session/Trace 到 Markdown 报告
│       ├── approval.py       # 审批策略
│       ├── sandbox.py        # Sandbox 端口与 LocalSandbox
│       ├── checkpoint.py     # ShadowGit 快照与持久化恢复
│       ├── loop.py           # AgentLoop 与 RunResult
│       └── tools/            # registry、内置/写入/执行/git/MCP 工具
├── tests/                    # 契约、配置、入口与运行时测试
├── examples/eval/
│   ├── tasks.json            # 示例评测任务
│   └── fixtures/             # 任务工作区模板
├── examples/mcp/
│   ├── servers.json          # stdio MCP 服务配置样例
│   └── write_server.py       # 独立 FastMCP 服务端示例
├── docs/
│   ├── README.md             # 文档索引与阅读路径
│   ├── quickstart.md         # 安装、运行、验证与故障排查
│   ├── configuration.md      # 配置优先级、环境变量与运行参数
│   ├── deployment.md         # 镜像构建、Compose 与容器部署
│   ├── architecture.md       # 当前实现与目标运行时架构
│   ├── security.md           # 信任模型与安全边界
│   ├── troubleshooting.md    # 按现象组织的排错指南
│   └── evaluation/           # 评测用法、任务格式与证据边界
├── Dockerfile                # HTTP 服务运行镜像（非 root、含 git）
├── compose.yaml              # 本地 Compose 部署与数据卷
├── .dockerignore             # 构建上下文排除项
├── .github/workflows/ci.yml  # Python 3.11 基础检查
├── .env.example              # 无密钥配置样例
├── pyproject.toml            # 包元数据与工具配置
├── requirements.lock         # 带哈希的完整依赖锁文件
└── requirements.txt          # 原 MCP 示例的固定依赖
```

`output/` 在 MCP 工具成功调用后生成，已由 `.gitignore` 排除，不属于源码。

## 快速开始

推荐使用虚拟环境安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps .
```

创建一个 Turn，默认使用离线 Fake 配置：

```bash
bale turn --message "Inspect the repository" --workspace "$PWD" --token-budget 4096
```

执行一个 ReAct 运行（默认 Fake Provider 立即完成；`--yes` 自动批准写入与命令）：

```bash
bale run --message "Inspect the repository" --workspace "$PWD" --yes
```

启用 SQLite Session 并记录会话 ID：

```bash
bale run \
  --message "Add a docstring to app.py" \
  --workspace "$PWD" \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --yes
```

恢复该 Session 中最近的未完成 Turn（不需要再传 `--message`）：

```bash
bale run \
  --workspace "$PWD" \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --resume \
  --yes
```

记录本次运行的 JSONL 追踪（追加写入，可与 `--resume` 组合）：

```bash
bale run \
  --message "Inspect the repository" \
  --workspace "$PWD" \
  --trace "$PWD/.bale/trace.jsonl" \
  --yes
```

把该 Session 与追踪汇总成 Markdown 报告：

```bash
bale report \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --trace "$PWD/.bale/trace.jsonl" \
  --output "$PWD/.bale/report.md"
```

在同一 Session 上开始多轮交互：

```bash
bale chat \
  --workspace "$PWD" \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --yes
```

检查 Session 与 Trace，并撤销未提交的改动：

```bash
bale session list --session-db "$PWD/.bale/sessions.db"
bale session show --session-db "$PWD/.bale/sessions.db" --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11
bale trace --trace "$PWD/.bale/trace.jsonl" --limit 5
bale undo --workspace "$PWD" --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --session-db "$PWD/.bale/sessions.db" --yes
```

检索工作区代码（默认 BM25；配置 `BALE_EMBEDDING_MODEL` 后启用混合排序，设置 `BALE_RETRIEVAL_INDEX_PATH` 可持久化增量索引）：

```bash
bale search --workspace "$PWD" --query "tool registry" --limit 5
```

运行示例评测任务（FakeProvider 下会因未修改文件而失败，连接真实模型才有参考价值）：

```bash
bale eval \
  --tasks examples/eval/tasks.json \
  --output "$PWD/.bale/eval.md" \
  --json "$PWD/.bale/eval.json"
```

使用 DeepSeek 或本地 Ollama 作为真实 Provider：

```bash
# DeepSeek
export BALE_PROVIDER=openai
export BALE_OPENAI_API_KEY=sk-...
export BALE_OPENAI_BASE_URL=https://api.deepseek.com/v1
export BALE_OPENAI_MODEL=deepseek-chat

# Ollama（无需 Key，本地模型需先 ollama pull qwen2.5-coder:7b）
export BALE_PROVIDER=openai
export BALE_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export BALE_OPENAI_MODEL=qwen2.5-coder:7b

bale run --message "Inspect the repository" --workspace "$PWD" --yes
```

启动 HTTP 应用（`BALE_HTTP_API_KEY`/`BALE_HTTP_API_KEYS` 启用认证，`BALE_HTTP_SESSION_DB`/`BALE_HTTP_APPROVALS`/`BALE_HTTP_RATE_LIMIT_PER_MINUTE`/`BALE_HTTP_AUDIT_LOG` 开启生产化能力，`BALE_MCP_CONFIG` 可挂载 MCP 工具）：

```bash
uvicorn bale_harness.http:create_app --factory
```

从项目根目录启动原 MCP 服务：

```bash
python examples/mcp/write_server.py
```

MCP 命令通过 `stdio` 等待客户端请求，不是交互式命令行程序。运行时客户端可直接复用示例配置：

```bash
bale run --workspace "$PWD" --message "写一条 MCP 笔记" \
  --mcp-config examples/mcp/servers.json --yes
```

HTTP 请求示例、完整客户端验证脚本、配置说明和常见问题见[快速启动指南](docs/quickstart.md)。

## 工具概览

### `write_file`

| 项目 | 内容 |
|---|---|
| 输入 | `content: str`，要写入文件的完整文本 |
| 输出 | 包含相对文件路径的成功文本，或包含错误信息的失败文本 |
| 写入位置 | `<进程工作目录>/output/note_YYYYMMDD_HHMMSS.txt` |
| 编码 | UTF-8 |
| 副作用 | 创建输出目录并写入或覆盖同名文件 |

调用方应根据返回文本检查执行结果，并妥善控制 MCP 服务的启动目录和文件系统权限。

## 开发与贡献

欢迎围绕示例可靠性、协议兼容性和后续运行时模块参与开发。

1. 提交 Issue 前先说明问题现象、复现步骤、Python/MCP SDK 版本和预期行为。
2. 从最新主分支创建主题分支，保持每次修改聚焦一个目标。
3. 新功能应包含必要注释、使用说明和可复现验证；新增运行时模块时同步补充自动化测试。
4. 提交前执行 `ruff check src tests`、`ruff format --check src tests`、`pytest -q` 和 `python -m compileall examples`。
5. 修改 MCP 示例后，按快速启动指南验证 `list_tools` 和 `call_tool`。
6. 不提交 `.venv/`、`output/`、日志、密钥或本地客户端配置。
7. 修改公开契约、工具签名、目录结构或运行方式时，同步更新 README 和相关文档。
8. 提交 Pull Request 时说明修改动机、行为变化、验证命令和已知限制。

## 许可证

当前仓库尚未指定开源许可证，也未包含 `LICENSE` 文件。在维护者明确选择许可证之前，本仓库不授予额外的复制、修改或再分发许可。添加许可证后，以根目录 `LICENSE` 文件为准。
