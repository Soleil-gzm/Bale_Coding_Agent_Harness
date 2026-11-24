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
| `BALE_MCP_CONFIG` | 路径，默认空 | HTTP 应用启动时加载的 MCP 服务器 JSON |
| `BALE_RETRIEVAL_ENABLED` | 布尔，默认 `true` | 是否在每个 Turn 自动注入 BM25 检索片段 |
| `BALE_RETRIEVAL_TOP_K` | 整数，默认 `5`，范围 1–20 | 自动检索注入的片段上限 |

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
| `--cost-budget`（仅 CLI） | 估算成本 | 需要同时配置输入/输出费率才有意义 |
| `BALE_INPUT_COST_PER_1K` + `BALE_OUTPUT_COST_PER_1K` | 成本估算 | 只填一项时 `estimated_cost` 保持 `null` |

Token 数与成本都是确定性估算，不等于 Provider 账单。默认 Fake Provider 的用量为 0。

## 5. HTTP 配置

| 配置 | 行为 |
|---|---|
| `BALE_HTTP_API_KEY` | 设置后 `/v1` 全部要求 `Authorization: Bearer <key>`；缺失或错误返回 401 与 `WWW-Authenticate: Bearer` |
| `BALE_MCP_CONFIG` | 应用 lifespan 内加载并复用 MCP 会话；文件缺失或 JSON/字段非法时拒绝启动 |
| `BALE_TRACE_PATH` | HTTP 执行同样写入默认 Trace；SSE 还会把事件推送到响应流 |

启动示例：

```bash
export BALE_HTTP_API_KEY=local-dev-key
uvicorn bale_harness.http:create_app --factory --host 127.0.0.1 --port 8000
```

HTTP 当前没有限流、Session 持久化或交互式审批流；`workspace` 由调用方指定，服务应只监听本机或可信内网。写入默认被拒绝，只有可信调用方才应传 `approve_writes=true`。

## 6. MCP 配置

单次运行：

```bash
bale run --workspace "$PWD" --message "写一条 MCP 笔记" \
  --mcp-config examples/mcp/servers.json --yes
```

- `--mcp-config` 优先于 `BALE_MCP_CONFIG`（CLI 只读取显式参数）。
- `bale chat` 在整个 REPL 期间复用同一 MCP 会话。
- 服务器配置支持 `name`、`command`、`args`、`env`、`cwd`、`read_only`、`timeout_ms`；相对 `cwd` 以配置文件所在目录为基准。
- 只支持 stdio transport；`read_only` 是调用方声明，不是隔离保证。

配置结构与示例见[快速启动 §4.11、§8](quickstart.md)和 [examples/mcp/servers.json](../examples/mcp/servers.json)。

## 7. 检索配置

自动检索在每个 Turn 开始时按用户消息检索工作区，把有限片段作为独立的 untrusted 段落写入系统前缀：

```bash
export BALE_RETRIEVAL_ENABLED=false   # 关闭自动注入
export BALE_RETRIEVAL_TOP_K=3         # 仅保留前 3 条
```

- `BALE_RETRIEVAL_TOP_K` 超出 1–20 会在配置校验阶段失败。
- 自动检索不感知对话历史，也不保证召回完整；检索异常会静默跳过，不阻断 Turn。
- 索引按工作区惰性构建，不写磁盘；关闭自动注入后仍可使用 `search_code` 或 `bale search`。

## 8. CLI 运行参数与配置的关系

| 参数 | 覆盖/补充的配置 | 说明 |
|---|---|---|
| `--trace` | `BALE_TRACE_PATH` | 单次运行指定 JSONL 路径 |
| `--session-db` | 无环境变量 | 启用 SQLite Session；Checkpoint 目录由它派生 |
| `--checkpoint-dir` | 无环境变量 | 显式指定 ShadowGit 持久化目录 |
| `--mcp-config` | `BALE_MCP_CONFIG`（仅 HTTP 使用） | 挂载 stdio MCP 服务器 |
| `--approval-mode` / `--yes` | 无环境变量 | 控制写与执行审批 |
| `--shadow-git` / `--no-shadow-git` | 无环境变量 | 默认开启快照 |
| `--max-iterations`、`--token-budget`、`--cost-budget` | 无环境变量 | 单次运行预算 |

## 9. 校验失败与安全处理

- **启动失败**：`provider=openai` 且远程端点缺少有效 Key；`BALE_RETRIEVAL_TOP_K` 越界；`BALE_MCP_CONFIG` 损坏。修复配置后再启动，不要通过关闭校验绕过。
- **HTTP 401**：检查 `Authorization: Bearer` 是否与 `BALE_HTTP_API_KEY` 完全一致；`/health` 不需要认证。
- **HTTP 422**：请求字段类型或范围不合法，例如 `token_budget <= 0`、未知字段、空白 `message`。
- **密钥处理**：Settings 使用 `SecretStr`，日志与 `repr` 不输出明文；不要把 `.env` 内容粘贴到 Issue 或终端分享中。
- **验证配置**：`bale --help` 检查 CLI 是否可用；`GET /health` 检查 HTTP 进程；一次 `bale run` 检查 Provider、Key 与工作区权限。

配置项与路由发生变化时，应同步更新本页、[快速启动 §3](quickstart.md)、[README](../README.md)和[文档索引](README.md)。
