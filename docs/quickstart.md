# Bale Coding Agent Harness 快速启动指南

本指南帮助你在本地安装并验证 B0 核心契约、CLI/HTTP Turn 创建入口、B1 只读 ReAct 运行时、B2a 写入运行时（`write_file`/`edit_file`/`shell`/`run_tests`/`git_diff`/`finish`、审批与 ShadowGit）、B3 上下文与会话运行时（Token 预算、历史裁剪、SQLite Session、取消恢复）、B4 追踪与成本运行时（JSONL Trace、耗时与可选成本估算）、B5 运行报告（Session/Trace 到 Markdown）、B6 本地闭环增强（持久化 Checkpoint 与 `undo`、`session`/`trace` 检查、`chat` 多轮、扩展写入工具、OpenAI 兼容 Provider）、B7 HTTP 执行/SSE 与生产化入口（`/v1/runs`、`/v1/runs/stream`、`/health`、Bearer 认证、按 `run_id` 取消）、B8 检索与评测（AST/行窗口 BM25、`search_code`/`bale search`、`bale eval`、自动上下文注入）、B9 MCP 工具接入（`bale run`/`bale chat`/HTTP）和 FastMCP 文件写入示例。Docker 容器隔离尚未实现；B2a 的本地执行只适用于受控开发场景。默认 Fake 配置不需要模型、数据库服务或容器服务。

## 1. 环境要求

| 依赖 | 要求 | 用途 |
|---|---|---|
| Python | 3.11 或更高版本，本地已验证 3.11.18 | 运行包、CLI/HTTP 和 MCP 示例 |
| pip | 建议使用当前稳定版本 | 安装锁定依赖 |
| MCP 客户端 | 支持 `stdio` 的客户端，或使用本文 Python 脚本 | 初始化和调用工具 |
| 文件系统权限 | 对启动目录具有写权限 | 创建 `output/` 和文本文件 |

默认 Fake 配置和 MCP 示例不需要 GPU、Docker、数据库或 API Key。选择 `openai` 时可连接 OpenAI、DeepSeek 或本地 Ollama；远程端点必须配置 Key。

## 2. 安装

### 2.1 获取项目并进入根目录

```bash
cd Bale_Coding_Agent_Harness
```

后续命令均默认在项目根目录执行。

### 2.2 创建虚拟环境

使用标准库 `venv`：

```bash
python -m venv .venv
source .venv/bin/activate
```

也可以使用 Conda：

```bash
conda create -n bale-harness python=3.11 -y
conda activate bale-harness
```

### 2.3 安装并检查依赖

```bash
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps .
python -m pip check
```

`requirements.lock` 是由 pyproject 生成、包含运行与开发依赖及哈希的完整锁文件。`requirements.txt` 仅为原 MCP 示例保留 `mcp==1.30.0`，完整安装应使用上述锁文件。不要直接升级到不兼容的 MCP 主版本后再沿用当前 FastMCP 示例。

## 3. 配置 Settings

默认配置为 `BALE_PROVIDER=fake`，无需创建 `.env` 即可使用 CLI 和 HTTP 的 Turn 创建入口。需要显式管理环境时，可以复制无密钥样例：

```bash
cp .env.example .env
```

配置项如下：

| 环境变量 | 当前行为 |
|---|---|
| `BALE_PROVIDER` | `fake` 或 `openai`，默认 `fake` |
| `BALE_OPENAI_API_KEY` | 远程 `openai` 端点必填，空白值视为缺失，使用 SecretStr 保护；本地回环端点可省略 |
| `BALE_OPENAI_BASE_URL` | OpenAI 兼容端点，默认 `https://api.openai.com/v1` |
| `BALE_OPENAI_MODEL` | 模型名，默认 `gpt-4o-mini` |
| `BALE_CONTEXT_WINDOW` | 上下文窗口 token 数，默认 `128000`；与 `turn.token_budget` 独立 |
| `BALE_TRACE_PATH` | 默认 JSONL 追踪文件；被 `--trace` 覆盖，留空则不写 |
| `BALE_INPUT_COST_PER_1K` | 输入 token 每千价格；需与下一项同时配置 |
| `BALE_OUTPUT_COST_PER_1K` | 输出 token 每千价格；未同时配置时不估算成本 |
| `BALE_HTTP_API_KEY` | 设置后所有 `/v1` 路由要求 `Authorization: Bearer <key>`；`/health` 始终公开 |
| `BALE_MCP_CONFIG` | HTTP 应用启动时加载的 MCP 服务器 JSON；损坏或缺失时拒绝启动 |
| `BALE_RETRIEVAL_ENABLED` | 是否在每次 Turn 自动注入 BM25 检索片段，默认 `true` |
| `BALE_RETRIEVAL_TOP_K` | 自动检索片段上限，默认 `5`，范围 1–20 |

远程 `openai` 端点缺少有效 Key 时会在初始化阶段失败；`http://localhost`、`http://127.0.0.1` 等本地端点可免 Key。`.env` 已被 Git 忽略，不要提交真实密钥或打印完整 Settings。

命令执行默认使用 `LocalSandbox`。需要容器隔离时设置 `BALE_SANDBOX_BACKEND=docker`，并按需配置 `BALE_SANDBOX_IMAGE`、`BALE_SANDBOX_MEMORY`、`BALE_SANDBOX_CPUS` 等变量；完整说明见[配置参考 §6](configuration.md)。配置优先级、CLI 参数映射与校验失败见[配置参考](configuration.md)。

常用端点示例：

| 服务 | `BALE_OPENAI_BASE_URL` | `BALE_OPENAI_MODEL` | Key |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | 必填 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | 必填 |
| Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5-coder:7b` | 可省略 |

Ollama 需先 `ollama pull qwen2.5-coder:7b`；7B 量化模型约 4.7GB，适合 8GB 显存级别显卡，更大模型会显著变慢或溢出到内存。

## 4. 使用 CLI 创建 Turn

```bash
bale turn \
  --message "Inspect the repository" \
  --workspace "$PWD" \
  --token-budget 4096
```

命令向标准输出写入一个 Turn JSON。默认生成随机 `turn_id` 和 `session_id`，状态为 `created`，`max_iterations` 为 20，`approval_mode` 为 `approve_write`。可通过 `--repo-revision`、`--max-iterations`、`--token-budget`、`--time-budget`、`--cost-budget` 和 `--approval-mode` 覆盖相应输入。

该命令只构造和校验 Turn，不创建 workspace，不调用 `FakeProvider.complete()`，也不执行任何工具。

### 4.1 使用 bale run 执行只读 ReAct

B1 提供 `bale run`，在指定工作区上执行一个只读 ReAct 循环并输出 `RunResult` JSON。默认 Fake Provider 会立即返回完成答案，无需网络或 API Key：

```bash
bale run --message "Inspect the repository" --workspace "$PWD"
```

输出示例：

```json
{
  "turn_id": "...",
  "status": "completed",
  "answer": "Task completed.",
  "iterations": 1,
  "tool_calls": 0,
  "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
  "error": null,
  "diff": "",
  "checkpoints": [],
  "session_id": null,
  "resumed": false,
  "duration_ms": 0,
  "estimated_cost": null
}
```

退出码：`completed` 为 0，`failed` 或 `cancelled` 为 1。可用 `--max-iterations` 限制循环次数，`--token-budget` 设置 token 预算，`--time-budget <秒>` 限制整个 Turn 的 wall-clock 时间（超时返回 `time budget exceeded`）。所有路径都受 `WorkspaceGuard` 防护，无法逃逸工作区。

### 4.2 修改代码并运行测试

B2a 加入了写入与执行工具：`write_file`、`edit_file`、`shell`、`run_tests`、`git_diff`、`finish`。写与执行属于副作用操作，默认需要审批；`--yes` 表示自动批准，适合脚本和演示：

```bash
bale run \
  --message "Add a docstring to app.py and run the tests" \
  --workspace "$PWD" \
  --yes
```

- `--approval-mode`：`auto_read`（写/执行一律拒绝）、`approve_write`（默认，写/执行逐次审批）、`approve_all`（全部审批）。
- 未传 `--yes` 且进程不是交互终端时，写/执行会被拒绝并回报给模型，不会阻塞等待输入。
- `--shadow-git/--no-shadow-git`（默认开启）：在系统临时目录建立独立 git-dir，为每次迭代创建快照，并把相对基线的 Diff 写入结果的 `diff` 字段；不会读写工作区自己的 `.git`。
- `shell` 不接受管道、重定向或串联等 shell 元字符，推荐直接传参数数组。`run_tests` 使用当前沙箱的 Python 解释器执行 pytest。
- `LocalSandbox` 是默认执行层，提供命令 deny-list、环境变量白名单、cwd 限定、超时和输出截断，**不是**不可信代码的安全边界。

需要容器隔离时改用 Docker 沙箱：

```bash
bale run \
  --message "Run the tests" \
  --workspace "$PWD" \
  --sandbox docker \
  --sandbox-image my-project/runner:latest \
  --yes
```

- 每条 `shell`/`run_tests` 启动一次性容器，工作区挂载到 `/workspace`。
- 默认无网络、只读根文件系统、非 root、CPU/内存/PID 限额；超时或取消会清理容器。
- 容器只注入少量固定环境变量，不继承宿主 `PATH`、API Key 或代理配置。
- 镜像必须自带所需运行时与测试依赖；`python:3.11-slim` 不含 pytest，运行 `run_tests` 请换用项目自己的镜像。
- 也可以用环境变量 `BALE_SANDBOX_BACKEND=docker`、`BALE_SANDBOX_IMAGE=...` 设为默认。

完整配置见[配置参考 §6](configuration.md)，安全边界见[安全模型](security.md)。

### 4.3 会话持久化与恢复

B3 增加了基于标准库 `sqlite3` 的 Session。传入 `--session-db` 后，消息、Turn 状态、已执行调用和 `after_turn` 记忆都会写入该文件；`--session-id` 用于显式指定会话：

```bash
bale run \
  --message "Add a docstring to app.py" \
  --workspace "$PWD" \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --yes
```

如果进程在 Turn 完成前退出（例如 Ctrl+C），该 Turn 会保持 `running` 或写入为 `cancelled`，可以恢复最近一个未完成 Turn。`--resume` 会复用存储的原始 user 消息，不需要也不允许同时传 `--message`：

```bash
bale run \
  --workspace "$PWD" \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --resume \
  --yes
```

恢复语义：

- Session 的 workspace 必须与 `--workspace` 一致，否则拒绝恢复。
- 已提交的写/执行调用会从数据库播种到幂等缓存，不会重复执行。
- 执行前已记录 `pending`、但没有结果的调用不会被重放，而是合成 `outcome_unknown` 的 tool 消息回注模型。
- 每个 Turn 完成（`completed`）后写入一条有界记忆摘要；下个 Turn 的稳定系统前缀会包含最近 5 条。
- Token 预算分两层：`turn.token_budget`（累计 `total_tokens` 上限）与 `BALE_CONTEXT_WINDOW`（上下文窗口裁剪）。Token 数为确定性估算，不等于 Provider 计费值。
- `turn.time_budget` 是 wall-clock 上限（秒，必须大于 0）。Loop 在迭代边界检查，并用剩余时间限制单次模型调用；超时以 `failed` 和 `time budget exceeded` 结束，工具调用仍受各自 `timeout_ms` 约束。
- 传入 `--session-db` 或 `--checkpoint-dir` 时，ShadowGit git-dir 会持久化到 `<session_db 父目录>/checkpoints/<session_id>`；否则仍使用进程临时目录。

### 4.4 追踪与成本

传入 `--trace` 后，每次运行向同一文件追加 JSONL 事件，每行一条记录：

```bash
bale run \
  --message "Inspect the repository" \
  --workspace "$PWD" \
  --trace "$PWD/.bale/trace.jsonl" \
  --yes
```

单次完成运行的典型事件序列：

```json
{"seq":1,"ts":"2026-01-01T00:00:00+00:00","event":"turn_started","turn_id":"...","session_id":null,"data":{"resumed":false}}
{"seq":2,"ts":"2026-01-01T00:00:00+00:00","event":"model_call","turn_id":"...","iteration":1,"duration_ms":3,"data":{"prompt_tokens":42,"prompt_messages":2,"usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0},"has_tool_calls":false}}
{"seq":3,"ts":"2026-01-01T00:00:00+00:00","event":"turn_finished","turn_id":"...","iteration":1,"status":"completed","duration_ms":5,"data":{"error":null,"iterations":1,"tool_calls":0,"usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0},"estimated_cost":null}}
```

- 事件词表：`turn_started`、`model_call`、`tool_call`、`checkpoint`、`turn_finished`。
- 不记录完整 Prompt、回答或工具参数，只记录规模、名称、状态与错误码。
- `--resume` 会向同一文件追加事件，`seq` 从新运行重新计数。
- 同时配置 `BALE_INPUT_COST_PER_1K` 与 `BALE_OUTPUT_COST_PER_1K` 后，`turn_finished.data.estimated_cost` 与 `RunResult.estimated_cost` 才有数值；它是估算，不是 Provider 账单。
- 追踪写入失败（目录不可写、目标为目录等）只会跳过 trace，不影响 Turn 结果。
- `BALE_TRACE_MAX_BYTES`（默认 0，不轮转）大于 0 时，写入前检查大小并轮转为 `trace.jsonl.1`…`.N`；`BALE_TRACE_BACKUPS` 控制保留的备份数（默认 3）。`bale trace` 只读取当前文件，历史事件在备份文件中。

### 4.5 生成运行报告

`bale report` 读取 Session 数据库，可选叠加 Trace，输出 Markdown（`--format html` 时输出独立 HTML 页面）：

```bash
bale report \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --trace "$PWD/.bale/trace.jsonl" \
  --output "$PWD/.bale/report.md"

bale report \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --format html \
  --output "$PWD/.bale/report.html"
```

报告包含：

- 会话概要：Session、Workspace、生成时间、Turn 数与状态计数、累计 token、可选估算成本。
- 每个 Turn：状态、迭代次数、工具调用数、用量、错误、Task 与 Answer；超长文本会截断。
- 工具调用表：工具名、状态、错误码与延迟。
- Trace 概要：事件计数、模型耗时与工具耗时；未提供 Trace 时显示占位说明。
- 记忆：写入的 `after_turn` 摘要。

省略 `--output` 时输出到标准输出。Session 不存在、没有 Turn、Trace 路径无效或 `--format` 不是 `markdown`/`html` 时命令以非零状态退出；报告不调用模型，也不修改数据库。HTML 由报告子集就地转换，所有文本先做 HTML 转义，不依赖第三方渲染库，也不生成 PDF。

### 4.6 多轮交互

`bale chat` 在同一个 Session 上循环执行 Turn；输入 `/exit` 或 `/quit` 退出：

```bash
bale chat \
  --workspace "$PWD" \
  --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --yes
```

- 每行输入生成一个独立 Turn；跨 Turn 连续性来自 Session 的 `after_turn` 记忆摘要，不携带完整历史。
- `--no-shadow-git` 可关闭快照；传 `--session-db` 时 Checkpoint 会持久化，可直接配合 `bale undo`。
- 写/执行工具需要审批；非交互环境请使用 `--yes`，否则会被拒绝并回报给模型。
- `--mcp-config` 可挂载 stdio/HTTP/SSE MCP 工具，会话在整个 REPL 期间保持；自动检索默认开启，可用 `BALE_RETRIEVAL_ENABLED=false` 关闭。

### 4.7 检查 Session/Trace 与撤销改动

```bash
bale session list --session-db "$PWD/.bale/sessions.db"
bale session list --session-db "$PWD/.bale/sessions.db" --json
bale session show --session-db "$PWD/.bale/sessions.db" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11
bale trace --trace "$PWD/.bale/trace.jsonl" --limit 5
```

`bale undo` 从持久化 Checkpoint 恢复工作区，默认回到基线：

```bash
bale undo \
  --workspace "$PWD" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --session-db "$PWD/.bale/sessions.db" \
  --yes
```

- 必须提供 `--session-db` 或 `--checkpoint-dir`，且目录中已有 ShadowGit `HEAD`。
- 未传 `--yes` 时会要求交互确认；`--snapshot <commit>` 可恢复到指定快照。
- 撤销会覆盖工作区当前内容，包括基线之后新建的文件。

浏览与清理持久化快照：

```bash
bale checkpoint list \
  --workspace "$PWD" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --session-db "$PWD/.bale/sessions.db"
bale checkpoint clean \
  --workspace "$PWD" \
  --session-id 6f1c1f6e-2e0e-4d0e-9d1e-2f7f1a9b8c11 \
  --session-db "$PWD/.bale/sessions.db" \
  --yes
```

- `list` 按时间倒序输出 commit、时间与快照消息，`--json` 便于脚本处理；目录不存在时输出 `no checkpoints` 并以 0 退出。
- `clean` 删除整个 Checkpoint 目录（含基线），不修改工作区；不带 `--yes` 时需要确认，拒绝删除会以 1 退出。
- 两个命令都不需要在运行 Turn 时使用，也不会重新扫描工作区；`--checkpoint-dir` 同样适用。

### 4.8 写入工具

除 `write_file`、`edit_file` 外，B6 增加了：

| 工具 | 行为 |
|---|---|
| `move_file(source, destination)` | 移动文件；目标已存在时拒绝，自动创建父目录 |
| `delete_file(path)` | 仅删除普通文件，目录和不存在路径返回错误 |
| `apply_patch(path, replacements)` | 按顺序应用 `{old, new}` 精确替换；每条必须恰好匹配一次，全部成功才原子写回 |

三者均为写副作用，需要审批。

### 4.9 代码检索

只读工具 `search_code` 与 CLI `bale search` 共用 `repo_index.HybridIndex`：默认是 BM25，配置 embedding 后自动加入向量召回并融合排序：

```bash
bale search --workspace "$PWD" --query "tool registry" --limit 5
bale search --workspace "$PWD" --query "session resume" --json
```

- 每次 CLI 调用都会重新扫描工作区；设置 `BALE_RETRIEVAL_INDEX_PATH` 时会读取并写回快照，未变化文件直接复用片段与向量。`search_code` 在单个工具实例内缓存索引。
- Python 文件按顶层函数/类切块（装饰器归入所属定义），模块头、定义间语句与尾部代码各自成块；语法错误或非 Python 文件回退到行窗口。
- 按 40 行窗口（8 行重叠）回退切块，标识符按 snake_case/camelCase 拆分后小写化，使用 BM25 排序。
- 默认跳过隐藏目录、`__pycache__`/`node_modules`/`dist`/`build` 等目录、二进制文件、非文本后缀和超过 256 KiB 的文件；最多索引 2000 个文件。
- `--limit` 取值为 1–50，`--path` 可限定子目录且不能逃逸工作区。混合检索时 `score` 是归一化融合分，`--json` 还会给出 `vector_search` 表示向量是否实际启用。
- 默认开启自动检索：`bale run`/`bale chat`/HTTP 在 Turn 开始按用户消息检索，把 `path:start-end` 片段作为独立 untrusted 段落写入系统前缀；同 Turn 内前缀稳定，检索失败时跳过。环境变量 `BALE_RETRIEVAL_ENABLED=false` 可关闭，`BALE_RETRIEVAL_TOP_K` 控制条数。

启用混合检索（OpenAI 兼容 embeddings 端点）：

```bash
export BALE_EMBEDDING_MODEL=text-embedding-3-small
export BALE_EMBEDDING_BASE_URL=https://api.openai.com/v1
export BALE_EMBEDDING_API_KEY=sk-...
export BALE_RETRIEVAL_ALPHA=0.5
export BALE_RETRIEVAL_INDEX_PATH=.bale/repo-index.json
bale search --workspace "$PWD" --query "how are sessions resumed" --json
```

也可复用已有的 `BALE_OPENAI_*` 配置（含本地 Ollama）并只额外设置模型名。embedding 失败时索引自动退回 BM25，`vector_search` 为 `false`；`BALE_EMBEDDING_API_KEY` 与 `BALE_EMBEDDING_BASE_URL` 分别回退到 `BALE_OPENAI_API_KEY` 与 `BALE_OPENAI_BASE_URL`。

### 4.10 固定任务评测

`bale eval` 读取 JSON 任务文件，把每个 `fixture` 复制到系统临时目录后执行一个完整 Turn，再验证文件断言：

```bash
bale eval \
  --tasks examples/eval/tasks.json \
  --output "$PWD/.bale/eval.md" \
  --json "$PWD/.bale/eval.json"
```

任务格式：

```json
{
  "tasks": [
    {
      "id": "greeting-message",
      "description": "Change the greeting text.",
      "fixture": "fixtures/greeting",
      "message": "Update greet() to return a message starting with hello.",
      "max_iterations": 6,
      "checks": [
        {"type": "file_exists", "path": "greetings.py"},
        {"type": "file_contains", "path": "greetings.py", "value": "hello"}
      ]
    }
  ]
}
```

- `fixture` 相对任务文件所在目录解析；任务文件也可直接传目录（读取其中的 `tasks.json`）。
- 支持 `file_exists`、`file_not_exists`、`file_contains`、`file_not_contains`；内容检查的 `value` 必须非空，存在性检查不接受 `value`。
- 评测自动批准写与执行，fixture 必须视为可信输入；原始 fixture 不会被修改。
- 每个任务使用独立的 Provider 实例；Markdown 默认写到标准输出，`--json` 额外写机器可读报告。
- 任一任务 `failed` 或 `error` 时命令返回非零退出码。默认 `BALE_PROVIDER=fake` 只返回固定答案，通常无法满足断言；真实完成率需要配置 Ollama/DeepSeek 等 Provider。
- `--sandbox docker`（或 `BALE_SANDBOX_BACKEND=docker`）让任务中的 `shell`/`run_tests` 在一次性容器内执行，镜像由 `--sandbox-image` / `BALE_SANDBOX_IMAGE` 指定；缺少 `docker` 可执行文件时命令直接失败，不会静默退回本机。文件写入工具仍在宿主进程内运行，临时工作区的挂载写权限要求与 `bale run` 相同。
- 当前只做文件级确定性检查，不执行 fixture 内测试、不比较 Diff，也不做模型评分。

### 4.11 MCP 工具接入

`bale run` 可通过 JSON 配置连接 MCP 服务并注册其工具（默认 `stdio`）：

```bash
bale run --workspace "$PWD" --message "写一条 MCP 笔记" \
  --mcp-config examples/mcp/servers.json --yes
```

配置格式：

```json
{
  "servers": [
    {
      "name": "notes",
      "command": "python",
      "args": ["write_server.py"],
      "cwd": ".",
      "read_only": false,
      "timeout_ms": 60000
    }
  ]
}
```

远程服务器使用 `transport`、`url` 与可选 `headers`：

```json
{
  "servers": [
    {
      "name": "remote",
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer <token>"},
      "read_only": true
    }
  ]
}
```

- `command`/`args`/`env` 用于启动子进程，相对 `cwd` 以配置文件所在目录解析。
- `transport` 取 `stdio`（默认）、`http`（streamable HTTP）或 `sse`；远程传输只接受 `url`/`headers`，混用 `command/args/env/cwd` 或 `url` 不是 `http(s)` 时配置校验失败。
- 每个工具对模型暴露为 `mcp_<server>_<tool>`；参数 schema 来自 MCP `inputSchema`，必填参数在本地先校验。
- 工具默认按写副作用处理，必须审批或加 `--yes`；`read_only=true` 适合只读服务器，并允许并行批次。
- MCP 协议错误返回 `mcp_error`，工具自身 `isError` 返回 `mcp_tool_error`；两者都会进入 `ToolResult`，不会中断循环。
- 每次 `bale run` 重新建立会话，运行结束后关闭连接；`bale chat --mcp-config` 在整个交互期间复用同一会话，HTTP 入口通过 `BALE_MCP_CONFIG` 在应用 lifespan 内启动并复用，配置损坏时启动失败。
- HTTP/SSE 不做重连或服务发现；认证与 TLS 由 `url`/`headers` 和远端服务负责。
- 手动验证最小服务端仍可使用第 6、7 节。

## 5. 使用 HTTP 创建与执行 Turn

启动应用工厂（默认读取项目根目录 `.env`）：

```bash
uvicorn bale_harness.http:create_app --factory --host 127.0.0.1 --port 8000
```

仅创建 Turn：

```bash
curl -X POST http://127.0.0.1:8000/v1/turns \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Inspect the repository",
    "workspace": "/absolute/path/workspace",
    "token_budget": 4096
  }'
```

成功时返回 HTTP 201 和与 CLI 相同的 Turn 结构。相同输入下，除分别生成的随机 ID 外字段一致。非法预算、空白消息和未知字段返回 HTTP 422。

执行 Turn（非流式）：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Inspect the repository",
    "workspace": "/absolute/path/workspace",
    "token_budget": 4096,
    "max_iterations": 4,
    "time_budget": 120,
    "approve_writes": false,
    "shadow_git": false
  }'
```

返回 HTTP 200 与 `RunResult`。`approve_writes` 默认 `false`，未开启审批时写/执行工具会被直接拒绝；把它设为 `true` 等于信任所有调用方，只应在可信环境使用。

设置 `BALE_HTTP_API_KEY` 后，所有 `/v1` 请求都需要 `Authorization: Bearer <key>`，缺失或错误返回 401 与 `WWW-Authenticate: Bearer`；`/health` 始终公开。多调用方时用 `BALE_HTTP_API_KEYS` 给每个 Key 配置 `principal` 与 `scope`：

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Inspect the repository", "workspace": "/absolute/path/workspace", "shadow_git": false}'
```

`/v1/runs` 与 `/v1/runs/stream` 都返回 `X-Run-Id`。请求可自带 `run_id`（仅字母、数字、`.`、`_`、`-`）以便查询或取消。运行状态与保留结果通过 `GET /v1/runs/{run_id}` 查询：

```bash
curl http://127.0.0.1:8000/v1/runs/inspect-1 \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY"
```

运行中返回 `status: "running"` 与 `result: null`；完成后 `status` 为终态，`result` 为完整 `RunResult`。响应带 `X-Run-Id` 与 `X-Session-Id`，仅同一 `principal` 可读，同一主体的 read-scope Key 也可查询；其他主体返回 403。未知或超出最近 200 条进程内记录的 Run 返回 404；状态与结果在重启后丢失，也不跨副本共享。此接口不要求启用 `BALE_HTTP_SESSION_DB`。

取消运行中的 Run：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs/inspect-1/cancel
```

取消是协作式的，在下一个迭代边界生效；未知或已结束的 `run_id` 返回 404，运行中复用相同 `run_id` 返回 409。设置 `BALE_MCP_CONFIG` 可让 HTTP 应用启动时挂载 MCP 工具；配置损坏时应用启动失败。

运行结束后可按 `run_id` 获取 Markdown 报告（需要 `BALE_HTTP_SESSION_DB`）：

```bash
curl http://127.0.0.1:8000/v1/runs/inspect-1/report \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY"
```

响应为 `text/markdown`，带 `X-Run-Id` 与 `X-Session-Id`；只包含该 Run 所属 Session，且仅同一 `principal` 可读。未启用 Session 返回 400，未知 `run_id` 返回 404。

设置 `BALE_HTTP_SESSION_DB` 后，Run 会写入 SQLite，并返回 `X-Session-Id`；把该值作为下一次请求的 `session_id` 即可复用会话。同一 `session_id` 的并发 Run 返回 409，未配置数据库时携带 `session_id` 返回 400：

```bash
export BALE_HTTP_SESSION_DB=.bale/sessions.db
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Continue", "workspace": "/srv/workspace", "session_id": "<uuid>"}'
```

设置 `BALE_HTTP_APPROVALS=true` 后，写/执行调用会挂起等待 HTTP 审批；`approval_mode=auto_read` 仍然直接拒绝写入：

```bash
curl http://127.0.0.1:8000/v1/runs/inspect-1/approvals \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY"

curl -X POST http://127.0.0.1:8000/v1/runs/inspect-1/approvals/<approval_id> \
  -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve"}'
```

审批列表包含工具名、`side_effect` 与 `arguments`；`read` scope 的 Key 不能审批。SSE 客户端可直接消费 `approval_requested` 与 `approval_decided` Trace 事件，不必轮询。超时（`BALE_HTTP_APPROVAL_TIMEOUT_SECONDS`，默认 120 秒）或 Run 结束/取消时，剩余审批按拒绝处理。`BALE_HTTP_RATE_LIMIT_PER_MINUTE` 启用按调用方的内存限流，超限返回 429 与 `Retry-After`；`BALE_HTTP_AUDIT_LOG` 追加运行与审批的 JSONL 审计。

需要容器化运行时，见[Docker 部署指南](deployment.md)。

以 SSE 流式观察同一 Turn：

```bash
curl -N -X POST http://127.0.0.1:8000/v1/runs/stream \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Inspect the repository",
    "workspace": "/absolute/path/workspace",
    "shadow_git": false
  }'
```

事件顺序为 `event: run`（携带 `run_id`）、若干 `event: trace`（`turn_started`、`model_call`、`tool_call`、`turn_finished` 等），最后是 `event: result`；执行异常会发送 `event: error`。客户端断开时服务端会取消执行。

HTTP 的 Session、限流、审批与审计都在单进程内维护，多副本之间不共享，也不提供 TLS；`workspace` 由调用方指定，服务应只监听本机或可信内网。自动检索默认开启，每次请求按 `workspace` 惰性构建索引。

## 6. 启动 MCP 服务端

```bash
python examples/mcp/write_server.py
```

正常情况下，进程会持续等待标准输入中的 MCP 消息，不会显示交互式提示符，也不应通过浏览器访问。

直接启动适合检查导入和进程是否可用。实际调用工具时，建议让 MCP 客户端启动并管理服务进程，而不是在一个终端中手动输入协议消息。

## 7. 使用 Python 客户端验证

如果第 6 节的手动服务进程仍在运行，可以先停止它；以下脚本会启动一个独立的服务端子进程。激活同一虚拟环境，然后从项目根目录执行：

```bash
python - <<'PY'
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    server = Path.cwd() / "examples" / "mcp" / "write_server.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(server)],
        cwd=str(Path.cwd()),
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [tool.name for tool in tools.tools])

            result = await session.call_tool(
                "write_file",
                {"content": "Hello from Bale Harness MCP.\n"},
            )
            print("result:", result.content[0].text)


asyncio.run(main())
PY
```

该脚本会：

1. 使用当前 Python 解释器启动服务端子进程；
2. 初始化 MCP session；
3. 列出工具，预期包含 `write_file`；
4. 调用工具并打印返回文本；
5. 关闭 session 并清理子进程。

成功后可在项目根目录检查生成文件：

```bash
ls output/
```

文件内容为：

```text
Hello from Bale Harness MCP.
```

`output/` 是运行时产物，已被 Git 忽略。

## 8. 配置常见 MCP 客户端

对于采用 `mcpServers` JSON 配置的客户端，可按其配置格式添加以下条目。将三个绝对路径替换为本机实际路径：

```json
{
  "mcpServers": {
    "bale-write-server": {
      "command": "/absolute/path/Bale_Coding_Agent_Harness/.venv/bin/python",
      "args": [
        "/absolute/path/Bale_Coding_Agent_Harness/examples/mcp/write_server.py"
      ],
      "cwd": "/absolute/path/Bale_Coding_Agent_Harness"
    }
  }
}
```

配置要点：

- `command` 必须指向安装了 `mcp==1.30.0` 的解释器。
- `args` 必须指向 `write_server.py` 的绝对路径。
- `cwd` 决定 `output/` 的位置；省略时将继承客户端的工作目录。
- 修改配置后按客户端要求重新连接或重启 MCP 服务。

不同客户端的配置入口可能不同，但服务端启动参数保持不变。

## 9. 基础使用约束

- 工具只接受 `content` 字符串，不接受目标文件路径。
- 文件统一命名为 `note_YYYYMMDD_HHMMSS.txt`。
- 同一秒内的多次写入可能使用相同文件名并覆盖内容。
- 文件操作异常会作为文本返回，调用方应检查返回内容和实际文件。
- 当前示例不具备工作区隔离或权限审批，只应在受控目录中运行。

## 10. 常见问题排查

### `ModuleNotFoundError: No module named 'mcp'`

客户端启动的解释器与安装依赖的解释器不一致。执行：

```bash
which python
python -m pip show mcp
```

在 MCP 客户端配置中使用同一个解释器的绝对路径。

### 从 `mcp.server.fastmcp` 导入失败

检查是否安装了不兼容版本：

```bash
python -m pip show mcp
python -m pip install --force-reinstall "mcp==1.30.0"
python -m pip check
```

### 服务端启动后没有输出

这是 `stdio` 服务的正常行为。使用第 7 节客户端脚本验证，不要把该进程当作交互式 REPL。

### 工具调用成功但找不到文件

`OUTPUT_DIR` 是相对路径。文件位于**服务端进程工作目录**的 `output/` 下，不一定是客户端目录。为客户端配置显式 `cwd`，或在返回文本中查看相对路径。

### 无法创建 `output/` 或写入文件

检查启动目录权限、磁盘空间和只读文件系统。建议选择普通可写目录启动服务，不要通过提升系统权限绕过路径管理。

### 文件内容被覆盖

当前文件名只有秒级精度。避免同秒并发写入；如需可靠文件标识，应将命名改为 UUID 等唯一值并同步更新测试与文档。

### 协议连接中断或出现普通文本解析错误

确保服务代码没有向标准输出打印日志。`stdio` 必须保留给 MCP 消息；诊断信息应使用标准错误或结构化日志方案。

## 11. 修改后的验证

完整基础检查：

```bash
python -m pip check
ruff check src tests
ruff format --check src tests
pytest -q
python -m compileall examples
```

测试覆盖契约、配置、FakeProvider、CLI/HTTP 同构、HTTP 输入校验、B1 只读运行时（消息契约、路径防护、工具注册器、只读工具、ReAct 循环、CLI run）、B2a 写入运行时（审批策略、Sandbox 防护、ShadowGit、写入/编辑、执行工具、幂等与端到端改码闭环）、B3 上下文与会话（预算非负、整 Turn 裁剪与配对、稳定前缀、SQLite 往返与事务回滚、记忆、取消恢复与不重复写入）、B4 追踪与成本（事件顺序、追加写入、脱敏、失败不阻断、成本估算）、B5 运行报告（Session 顺序、Trace 回读、报告内容、`--output`）、B6 本地闭环（Session 汇总、Checkpoint 跨进程基线、`session`/`trace`/`chat`/`undo`、扩展写入工具、OpenAI 兼容 Provider 映射/重试/脱敏、本地端点免 Key）和 B7 HTTP 执行/SSE（默认拒绝写入、显式审批后写入、SSE 事件顺序与 `result` 结束）、B8 检索与评测（分词、BM25 排序、隐藏/二进制过滤、`search_code`/`bale search`、任务校验、fixture 隔离、文件断言与 Markdown/JSON 报告）、B9 MCP 工具接入（配置校验、工具名与 schema 映射、必填参数校验、写/只读治理、错误归一化、stdio 往返、`bale run --mcp-config`）以及 MCP 示例语法，不需要网络或 API Key。部分测试会在临时目录启动真实 pytest 子进程。仓库已配置 GitHub Actions，但远程结果应以 Actions 页面为准。

修改 MCP 示例后，重新执行第 7 节脚本，确认：

- `write_file` 出现在工具列表中；
- 工具调用无异常；
- 返回文本包含生成路径；
- 文件内容与传入字符串完全一致；
- 客户端退出后没有残留服务进程。

## 12. 下一步

- 按目标选择阅读路径：[文档索引](README.md)。
- 调整环境变量、Provider、执行沙箱或 HTTP/MCP 开关：[配置参考](configuration.md)。
- 用 Docker 构建并运行 HTTP 服务：[Docker 部署](deployment.md)。
- 了解当前实现与目标模块的边界：[架构设计](architecture.md)。
- 部署或评审前确认信任边界：[安全模型](security.md)。
- 遇到失败先查：[问题排查](troubleshooting.md)。
- 记录评测结果：[评测指南](evaluation/README.md)。
- 返回项目功能、结构和贡献说明：[README](../README.md)。
