# 配置参考

本页集中说明 Bale 的配置来源、全部环境变量、Provider 与 HTTP/MCP/检索开关，以及常见的配置错误。安装与可复制命令见[快速启动](quickstart.md)，排错见[问题排查](troubleshooting.md)。

## 1. 配置来源与优先级

Bale 使用 pydantic-settings 读取配置，环境变量统一加 `BALE_` 前缀：

```text
CLI 参数 > 进程环境变量 > 当前工作目录的 .env > 代码默认值
```

- `.env` 由 pydantic-settings 从**当前工作目录**读取；在仓库根目录执行 `bale ...` 或 `uvicorn ...` 时最常见。
- 进程环境变量优先于 `.env`，同名键以环境变量为准。
- CLI 参数只覆盖对应行为的单次运行（如 `--trace`、`--session-db`），不修改全局配置。
- 未识别的环境变量会被忽略（`extra="ignore"`），拼写错误不会导致启动失败，也不会生效。
- `.env` 已被 Git 忽略；不要提交真实密钥。

## 2. 环境变量总表

| 变量 | 类型与默认值 | 作用 |
|---|---|---|
| `BALE_PROVIDER` | `fake` 或 `openai`，默认 `fake` | 选择离线 Provider 或 OpenAI 兼容 Provider |
| `BALE_OPENAI_API_KEY` | 字符串，默认空 | 远程 OpenAI 兼容端点的 Bearer Key；使用 `SecretStr`，不会在 `repr` 中明文显示 |
| `BALE_OPENAI_BASE_URL` | 默认 `https://api.openai.com/v1` | OpenAI 兼容端点 |
| `BALE_OPENAI_MODEL` | 默认 `gpt-4o-mini` | 模型名 |
| `BALE_CONTEXT_WINDOW` | 正整数，默认 `128000` | 上下文窗口裁剪上限；必须大于 0 |
| `BALE_TRACE_PATH` | 路径，默认空 | 默认 JSONL Trace 文件；为空时不写 Trace |
| `BALE_INPUT_COST_PER_1K` | 非负浮点数，默认空 | 输入 token 每千价格；必须与下一项同时配置 |
| `BALE_OUTPUT_COST_PER_1K` | 非负浮点数，默认空 | 输出 token 每千价格；必须与上一项同时配置 |
| `BALE_HTTP_API_KEY` | 字符串，默认空 | 设置后所有 `/v1` 路由要求 Bearer 认证；`/health` 始终公开 |
| `BALE_HTTP_API_KEYS` | JSON 对象，默认空 | 多 Key 认证：`{"<token>": {"principal": "alice", "scope": "read"\|"write"}}` |
| `BALE_HTTP_SESSION_DB` | 路径，默认空 | 设置后 HTTP 运行持久化到 SQLite；请求可用 `session_id` 复用会话 |
| `BALE_HTTP_RATE_LIMIT_PER_MINUTE` | 非负整数，默认 `0` | 每个调用方每分钟的请求数；`0` 关闭；超限返回 429 与 `Retry-After` |
| `BALE_HTTP_APPROVALS` | 布尔，默认 `false` | 开启后写/执行调用等待 HTTP 审批，超时拒绝 |
| `BALE_HTTP_APPROVAL_TIMEOUT_SECONDS` | 正整数，默认 `120` | 单个审批的等待上限；超时按拒绝处理 |
| `BALE_HTTP_AUDIT_LOG` | 路径，默认空 | 追加 JSONL 审计记录：运行开始/结束、取消、审批请求与决定 |
| `BALE_MCP_CONFIG` | 路径，默认空 | HTTP 应用启动时加载的 MCP 服务器 JSON |
| `BALE_RETRIEVAL_ENABLED` | 布尔，默认 `true` | 是否在每个 Turn 自动注入检索片段 |
| `BALE_RETRIEVAL_TOP_K` | 整数，默认 `5`，范围 1–20 | 自动检索注入的片段上限 |
| `BALE_RETRIEVAL_HYBRID` | 布尔，默认 `true` | 配置 embedding 模型时启用向量召回；未配置模型时始终为纯 BM25 |
| `BALE_RETRIEVAL_ALPHA` | 浮点数，默认 `0.5`，范围 0–1 | 混合排序中 BM25 的权重，其余权重分配给向量相似度 |
| `BALE_RETRIEVAL_INDEX_PATH` | 路径，默认空 | 索引 JSON 快照路径；设置后片段与向量可跨进程复用并增量更新 |
| `BALE_EMBEDDING_MODEL` | 字符串，默认空 | OpenAI 兼容 embedding 模型名；为空则不做向量检索 |
| `BALE_EMBEDDING_BASE_URL` | 字符串，默认空 | embeddings 端点；为空时复用 `BALE_OPENAI_BASE_URL` |
| `BALE_EMBEDDING_API_KEY` | 字符串，默认空 | embedding 请求的 Bearer Key；为空时复用 `BALE_OPENAI_API_KEY`，本地回环可省略 |
| `BALE_SANDBOX_BACKEND` | `local` 或 `docker`，默认 `local` | 命令执行沙箱；`docker` 为每条命令启动独立容器 |
| `BALE_SANDBOX_IMAGE` | 字符串，默认 `python:3.11-slim` | Docker 沙箱镜像；必须包含工作区需要的运行时与测试依赖 |
| `BALE_SANDBOX_NETWORK` | 布尔，默认 `false` | 是否给沙箱容器启用网络；默认 `none` |
| `BALE_SANDBOX_MEMORY` | 字符串，默认 `1g` | 容器内存上限，透传给 `docker run --memory` |
| `BALE_SANDBOX_CPUS` | 正浮点数，默认 `1.0` | 容器 CPU 上限，透传给 `docker run --cpus` |
| `BALE_SANDBOX_PIDS_LIMIT` | 整数，默认 `256`，最小 16 | 容器进程数上限 |
| `BALE_SANDBOX_USER` | 字符串，默认 `10001:10001` | 容器内运行用户；Linux 宿主机需保证挂载目录对该 UID 可写 |

无密钥样例见仓库根目录 [.env.example](../.env.example)。

## 3. Provider 配置

### 3.1 离线 Fake

不设置任何变量即可：

```bash
bale run --workspace "$PWD" --message "Inspect the repository"
```

`FakeProvider` 返回确定性文本，适合测试 CLI、HTTP、Session、Trace 和评测管道。

### 3.2 远程 OpenAI 兼容端点

```bash
export BALE_PROVIDER=openai
export BALE_OPENAI_API_KEY=sk-...
export BALE_OPENAI_BASE_URL=https://api.deepseek.com/v1
export BALE_OPENAI_MODEL=deepseek-chat
```

`provider=openai` 时，Key 为空白或缺失会在配置校验阶段立即报错，错误信息为 `BALE_OPENAI_API_KEY is required when BALE_PROVIDER=openai`。

### 3.3 本地 Ollama

```bash
export BALE_PROVIDER=openai
export BALE_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export BALE_OPENAI_MODEL=qwen2.5-coder:7b
```

`localhost`、`127.0.0.1`、`::1`、`0.0.0.0` 被视为本地回环端点，可以省略 Key。本地端点免 Key 不代表模型具备稳定的工具调用能力；7B 量化模型可能输出文本形式的伪工具调用。

Provider 只实现非流式 Chat Completions，`temperature=0`；不支持流式响应、多模型路由或结构化输出参数。

## 4. 上下文、预算与成本

| 配置 | 控制范围 | 说明 |
|---|---|---|
| `BALE_CONTEXT_WINDOW` | 单次模型调用的上下文 | 超限时由 `HistoryTrimmer` 按整 Turn 裁剪，并保持 assistant 工具调用与 tool 结果配对 |
| `--token-budget`（CLI） / `token_budget`（HTTP） | 整个 Turn 的累计 token | 与上下文窗口相互独立；达到预算后 Turn 以预算错误结束 |
| `--max-iterations` | ReAct 迭代次数 | CLI 与 HTTP 都限制在 1–500，默认 20 |
| `--cost-budget`（仅 CLI） | Turn 的成本预算字段 | 当前只随 Turn 记录，运行循环**不做**成本上限拦截；成本估算仍取决于下面的费率 |
| `BALE_INPUT_COST_PER_1K` + `BALE_OUTPUT_COST_PER_1K` | 成本估算 | 只填一项时 `estimated_cost` 保持 `null` |

Token 数与成本都是确定性估算，不等于 Provider 账单。默认 Fake Provider 的用量为 0。

## 5. HTTP 配置

| 配置 | 行为 |
|---|---|
| `BALE_HTTP_API_KEY` | 设置后 `/v1` 全部要求 `Authorization: Bearer <key>`；缺失或错误返回 401 与 `WWW-Authenticate: Bearer` |
| `BALE_HTTP_API_KEYS` | 多 Key：每个 Key 映射 `principal` 与 `scope`；`read` 只能运行 `approval_mode=auto_read` 的 Turn，且不能审批 |
| `BALE_HTTP_SESSION_DB` | 设置后每个 Run 记录到 SQLite；响应头与 `RunResult` 都带 `session_id`；同一会话的并发 Run 返回 409 |
| `BALE_HTTP_RATE_LIMIT_PER_MINUTE` | 按 `principal`（无认证时按客户端 IP）使用内存令牌桶；`/health` 不受限 |
| `BALE_HTTP_APPROVALS` | `true` 时写/执行调用挂起并广播 `approval_requested` Trace 事件，等待 HTTP 决定；超时拒绝 |
| `BALE_HTTP_APPROVAL_TIMEOUT_SECONDS` | 审批等待上限；到期未决定按拒绝处理，写入不会发生 |
| `BALE_HTTP_AUDIT_LOG` | 追加 JSONL：`run_started`、`run_finished`、`run_cancelled`、`run_failed`、`approval_requested`、`approval_decided`、`approval_result`；不记录提示词与工具参数 |
| `BALE_MCP_CONFIG` | 应用 lifespan 内加载并复用 MCP 会话；文件缺失或 JSON/字段非法时拒绝启动 |
| `BALE_TRACE_PATH` | HTTP 执行同样写入默认 Trace；SSE 还会把事件推送到响应流 |
| `BALE_SANDBOX_BACKEND` | HTTP 执行同样使用该沙箱；`docker` 需要服务进程能访问 Docker daemon |

启动示例：

```bash
export BALE_HTTP_API_KEY=local-dev-key
export BALE_HTTP_SESSION_DB=.bale/sessions.db
export BALE_HTTP_RATE_LIMIT_PER_MINUTE=60
export BALE_HTTP_AUDIT_LOG=.bale/audit.jsonl
uvicorn bale_harness.http:create_app --factory --host 127.0.0.1 --port 8000
```

审批与审批接口：

```bash
# 1. 发起需要写入的 Run；approve_writes 保持 false
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Fix the failing test", "workspace": "/srv/workspace", "run_id": "fix-1"}'

# 2. 查询挂起的审批（包含工具名与参数）
curl http://127.0.0.1:8000/v1/runs/fix-1/approvals \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY"

# 3. 批准或拒绝
curl -X POST http://127.0.0.1:8000/v1/runs/fix-1/approvals/<approval_id> \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve"}'
```

`GET /v1/runs/{run_id}/approvals` 返回 `approval_id`、`tool`、`side_effect` 与 `arguments`，仅对已认证调用方可见。SSE 消费方无需轮询：`approval_requested` 与 `approval_decided` Trace 事件会随流推送，事件 `data.approval_id` 可用于提交决定。Run 结束或取消时未决定的审批一律拒绝。

Session 复用：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Continue", "workspace": "/srv/workspace", "session_id": "<uuid>"}'
```

不传 `session_id` 时服务端生成一个并在 `X-Session-Id` 与 `RunResult.session_id` 返回。未配置 `BALE_HTTP_SESSION_DB` 时携带 `session_id` 返回 400。

限流与认证边界：

- 认证成功时响应带 `X-Bale-Principal`，审计使用同一 `principal`。
- 超过限流返回 429 与 `Retry-After` 秒数；令牌桶在进程内，重启即清空，多副本之间不共享。
- `read` scope 的 Key 发起非 `auto_read` 运行返回 403，审批接口也返回 403。
- `workspace` 仍由调用方指定，服务应只监听本机或可信内网；生产部署仍需在外层加 TLS。

## 6. 执行沙箱配置

默认 `local` 使用 `LocalSandbox`，在宿主进程内执行命令。选择 `docker` 后，每次 `shell`/`run_tests` 都会启动一个一次性容器：

```bash
export BALE_SANDBOX_BACKEND=docker
export BALE_SANDBOX_IMAGE=my-project/runner:latest
bale run --workspace "$PWD" --message "Run the test suite" --yes
```

CLI 也可以单次覆盖：

```bash
bale run --sandbox docker --sandbox-image my-project/runner:latest ...
```

容器默认配置：

- 工作区绑定挂载到 `/workspace`，`cwd` 映射到其中的相对目录；
- `--network none`，除非 `BALE_SANDBOX_NETWORK=true`；
- 只读根文件系统，`/tmp` 为 64 MiB tmpfs；
- `--cap-drop ALL`、`no-new-privileges`、非 root 用户；
- `--memory`、`--cpus`、`--pids-limit` 限额；
- 命令超时或运行取消时执行 `docker rm -f` 清理；
- 只注入 `HOME`、`BALE_SANDBOX`、`PYTHONUNBUFFERED` 与工具显式传入的环境变量，不继承宿主环境。

使用要求：

- 宿主机必须能直接执行 `docker`；Bale 自身运行在容器内时，需要额外安装 Docker CLI 并挂载 socket，部署镜像默认不包含；
- 镜像里必须有命令需要的运行时。`run_tests` 通过镜像内的 `python -m pytest` 执行，因此默认 `python:3.11-slim` 只能运行 `shell` 类命令，不能直接跑 pytest；
- `/workspace` 的写权限由 `BALE_SANDBOX_USER` 与宿主挂载权限共同决定。Linux 上可执行 `sudo chown -R 10001:10001 <workspace>`，或把 `BALE_SANDBOX_USER` 设为当前 UID/GID；
- 只读根文件系统会阻止容器内 `pip install`、写 `/app` 等操作；需要构建依赖时请预构建镜像。

## 7. MCP 配置

单次运行：

```bash
bale run --workspace "$PWD" --message "写一条 MCP 笔记" \
  --mcp-config examples/mcp/servers.json --yes
```

- CLI 只读取显式的 `--mcp-config`；`BALE_MCP_CONFIG` 仅用于 HTTP 应用启动时加载。
- `bale chat` 在整个 REPL 期间复用同一 MCP 会话。
- 服务器配置支持 `name`、`command`、`args`、`env`、`cwd`、`read_only`、`timeout_ms`；相对 `cwd` 以配置文件所在目录为基准。
- 只支持 stdio transport；`read_only` 是调用方声明，不是隔离保证。

配置结构与示例见[快速启动 §4.11、§8](quickstart.md)和 [examples/mcp/servers.json](../examples/mcp/servers.json)。

## 8. 检索配置

自动检索在每个 Turn 开始时按用户消息检索工作区，把有限片段作为独立的 untrusted 段落写入系统前缀。默认是纯 BM25；配置 embedding 模型后变为向量召回与 BM25 的混合排序：

```bash
export BALE_RETRIEVAL_ENABLED=false      # 关闭自动注入
export BALE_RETRIEVAL_TOP_K=3            # 仅保留前 3 条
export BALE_RETRIEVAL_HYBRID=true        # 有 embedding 模型时启用向量召回
export BALE_RETRIEVAL_ALPHA=0.5          # BM25 权重；0 为纯向量，1 为纯 BM25
export BALE_RETRIEVAL_INDEX_PATH=.bale/repo-index.json

# OpenAI 兼容 embeddings；本地端点（如 Ollama）可省略 Key。
export BALE_EMBEDDING_MODEL=text-embedding-3-small
export BALE_EMBEDDING_BASE_URL=https://api.openai.com/v1
export BALE_EMBEDDING_API_KEY=sk-...
```

- 未设置 `BALE_EMBEDDING_MODEL` 时只使用 BM25。`BALE_EMBEDDING_BASE_URL` 为空会复用 `BALE_OPENAI_BASE_URL`，`BALE_EMBEDDING_API_KEY` 为空会复用 `BALE_OPENAI_API_KEY`；远程端点没有可用 Key 时不启用向量，而不是启动失败。
- embedding 请求失败、返回空向量或维度不一致时，索引清空向量并退回 BM25，搜索与 Turn 继续执行。`search_code` 的 `vector_search` 元数据和 `bale search --json` 的 `vector_search` 字段表示实际是否启用向量。
- `BALE_RETRIEVAL_TOP_K` 超出 1–20、`BALE_RETRIEVAL_ALPHA` 超出 0–1 会在配置校验阶段失败。
- 索引按文件 `mtime_ns:size` 签名增量重建：未变化文件复用片段与向量，新增或修改文件重新切块并只补齐缺失向量，删除文件被移除。设置 `BALE_RETRIEVAL_INDEX_PATH` 后快照以 JSON 原子写入磁盘；快照损坏或版本不符时整体忽略并重建。
- 索引在每个检索器或工具实例内只构建一次，同一次 Turn 中不会自动感知文件变化；跨进程复用或减少重复 embedding 时请配置快照路径。
- 自动检索不感知对话历史，也不保证召回完整；检索异常会静默跳过，不阻断 Turn。关闭自动注入后仍可使用 `search_code` 或 `bale search`。
- 向量召回会把片段文本发送给配置的 embedding 端点；不要将不可外发的代码索引到远程服务。

## 9. CLI 运行参数与配置的关系

| 参数 | 覆盖/补充的配置 | 说明 |
|---|---|---|
| `--trace` | `BALE_TRACE_PATH` | 单次运行指定 JSONL 路径 |
| `--session-db` | 无环境变量 | 启用 SQLite Session；Checkpoint 目录由它派生 |
| `--checkpoint-dir` | 无环境变量 | 显式指定 ShadowGit 持久化目录 |
| `--mcp-config` | `BALE_MCP_CONFIG`（仅 HTTP 使用） | 挂载 stdio MCP 服务器 |
| `--approval-mode` / `--yes` | 无环境变量 | 控制写与执行审批 |
| `--shadow-git` / `--no-shadow-git` | 无环境变量 | 默认开启快照 |
| `--max-iterations`、`--token-budget`、`--cost-budget` | 无环境变量 | 单次运行预算 |

## 10. 校验失败与安全处理

- **启动失败**：`provider=openai` 且远程端点缺少有效 Key；`BALE_RETRIEVAL_TOP_K` 或 `BALE_RETRIEVAL_ALPHA` 越界；`BALE_MCP_CONFIG` 损坏。修复配置后再启动，不要通过关闭校验绕过。
- **检索静默退回 BM25**：`bale search --json` 的 `vector_search=false` 表示未配置 embedding 或调用失败；检查 `BALE_EMBEDDING_MODEL`、端点与 Key。embedding 故障不会阻断搜索或 Turn。
- **HTTP 401**：检查 `Authorization: Bearer` 是否与 `BALE_HTTP_API_KEY` 完全一致；`/health` 不需要认证。
- **HTTP 422**：请求字段类型或范围不合法，例如 `token_budget <= 0`、未知字段、空白 `message`。
- **密钥处理**：Settings 使用 `SecretStr`，日志与 `repr` 不输出明文；不要把 `.env` 内容粘贴到 Issue 或终端分享中。
- **验证配置**：`bale --help` 检查 CLI 是否可用；`GET /health` 检查 HTTP 进程；一次 `bale run` 检查 Provider、Key 与工作区权限。

配置项与路由发生变化时，应同步更新本页、[快速启动 §3](quickstart.md)、[README](../README.md)和[文档索引](README.md)。
