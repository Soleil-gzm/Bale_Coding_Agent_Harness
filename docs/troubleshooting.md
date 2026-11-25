# 问题排查

本文按「现象」组织。先做下面的三项自检，再跳到对应章节。MCP **服务端**的启动与协议问题在 [快速启动 §10](quickstart.md#10-常见问题排查) 有更细的步骤，这里不重复。

## 先做三项自检

```bash
python --version                 # 需要 3.11+
python -c "import bale_harness"  # 确认当前解释器装了项目
bale --help                      # 确认 console script 可用
```

```bash
# 查看当前生效配置（不会打印密钥内容）
python -c "from bale_harness.settings import Settings; s=Settings(); print(s.provider, s.openai_base_url, s.openai_model, bool(s.openai_api_key))"
```

若 `python -c "import bale_harness"` 失败，先修复安装；若配置与预期不符，检查 `.env`、shell 环境变量与 `BALE_` 前缀。

## 安装与启动

### `ModuleNotFoundError: No module named 'bale_harness'`

项目未以可编辑模式安装，或命令用了另一个解释器。

```bash
python -m pip install -e ".[dev]"
python -c "import bale_harness; print(bale_harness.__file__)"
```

### `BALE_OPENAI_API_KEY is required when BALE_PROVIDER=openai`

`BALE_PROVIDER=openai` 时必须提供 Key。二选一：

```bash
export BALE_OPENAI_API_KEY=sk-...        # PowerShell: $env:BALE_OPENAI_API_KEY="sk-..."
# 或改用本地兼容端点 / Fake
BALE_PROVIDER=fake bale run --workspace . --message "hello"
```

`http://localhost`、`http://127.0.0.1` 等本地回环端点可省略 Key；若端点要求非空 Bearer，填任意非空字符串即可。

### Windows 控制台中文或符号乱码

子进程与 MCP 输出统一按 UTF-8 解码；若终端仍乱码，先设置 `PYTHONIOENCODING=utf-8` 并确认终端代码页。

## CLI 运行

### 写/执行工具被拒绝（`denied`）

这是默认策略，不是故障。`approve_write` 模式下需要人工确认；非交互环境不会弹窗，因此直接拒绝。

```bash
bale run --workspace . --message "修复测试" --yes            # 信任场景，全部放行
bale run --workspace . --message "只读分析" --approval-mode auto_read
```

`--yes` 只应在受控目录使用。HTTP 对应字段是 `approve_writes=true`，同样表示调用方已确认风险。

### `context window exceeded`

固定前缀（系统提示 + 工具说明 + 检索片段 + 记忆）加上当前消息已经超过 `BALE_CONTEXT_WINDOW`。可尝试：

1. 提高 `BALE_CONTEXT_WINDOW`（需与模型的真实窗口一致）。
2. 降低 `BALE_RETRIEVAL_TOP_K` 或设置 `BALE_RETRIEVAL_ENABLED=false`，减少系统前缀。
3. 缩短当前消息；历史裁剪救不了「当前 Turn 本身就放不下」的情况。

### `git executable not found on PATH` 或 ShadowGit 失败

快照依赖本机 `git`。安装 Git，或关闭快照：

```bash
bale run --workspace . --message "..." --no-shadow-git
```

持久化 Checkpoint 目录必须可写，且建议放在工作区之外；目录在工作区内时，CLI 会退回到系统临时目录。

### 恢复相关报错

| 报错 | 原因 | 处理 |
|---|---|---|
| `--resume cannot be combined with --message` | 两个参数互斥 | 只保留 `--resume` |
| `--resume requires both --session-db and --session-id` | 缺少定位信息 | 同时传两个参数 |
| `--message is required unless --resume is used` | 普通运行缺少消息 | 补 `--message` |
| `no resumable turn for session ...` | 没有未完成的 Turn | 用新会话或新消息重新运行 |
| `workspace does not match the stored session` | 工作区与 Session 不一致 | 使用原工作区，或换 Session |
| `session belongs to workspace ...` | Session 指向别的工作区 | 换 Session 或改工作区 |

### `bale undo` 报错

`--checkpoint-dir or --session-db is required`：undo 需要知道快照位置；`no checkpoint at ...`：目录不存在或尚未产生快照。先运行一次带持久化参数的 `bale run`/`bale chat`。

### `unsupported sandbox ...; use 'local' or 'docker'`

`--sandbox` 只接受 `local` 或 `docker`。检查拼写，或删除该参数使用 `BALE_SANDBOX_BACKEND` 的默认值。

### `docker executable not found` 或 `Cannot connect to the Docker daemon`

`--sandbox docker` 需要当前进程能直接执行 `docker` 并访问 daemon：

```bash
docker version
docker info
```

- Windows/macOS：启动 Docker Desktop，等引擎显示 running。
- Linux：确认当前用户在 `docker` 组，或调整 socket 权限。
- Bale 自身运行在容器内时，部署镜像不含 Docker CLI，也未挂载 socket；应在宿主机运行 Bale 再启用 Docker 沙箱。

### Docker 沙箱里 `No module named pytest` / 命令不存在

`BALE_SANDBOX_IMAGE` 决定容器内可用工具。默认 `python:3.11-slim` 没有 pytest、git 或项目依赖：

```bash
docker build -t my-project/runner:latest .
export BALE_SANDBOX_IMAGE=my-project/runner:latest
```

镜像需要包含 Python、pytest 以及被测项目运行时。构建前先 `docker pull`，避免首次运行把镜像拉取时间算进命令超时。

### Docker 沙箱写工作区报 `Permission denied`

容器默认以 `10001:10001` 运行。Linux 上宿主目录需要对该 UID 可写：

```bash
sudo chown -R 10001:10001 ./workspace
```

或把 `BALE_SANDBOX_USER` 设为当前用户的 `uid:gid`。Windows/macOS 的 Docker Desktop 绑定挂载通常不受容器 UID 限制。

### Docker 沙箱内网络请求失败

默认 `--network none`。需要联网时设置 `BALE_SANDBOX_NETWORK=true`，并自行评估数据外泄风险。

### Docker 沙箱命令超时后仍有容器

正常情况下超时会执行 `docker rm -f`。若进程被强杀，可手动清理：

```bash
docker ps -a --filter label=bale.sandbox=1
docker rm -f <container>
```

## HTTP

### 401 + `WWW-Authenticate: Bearer`

设置了 `BALE_HTTP_API_KEY` 或 `BALE_HTTP_API_KEYS`，但请求未带或带错令牌：

```bash
curl http://127.0.0.1:8000/health          # 始终公开
curl -H "Authorization: Bearer $BALE_HTTP_API_KEY" \
  -X POST http://127.0.0.1:8000/v1/runs ...
```

若本地开发不需要认证，取消该环境变量即可；服务不要暴露到公网。

### 422 参数校验失败

常见原因：`message` 为空、`workspace` 不存在或不是目录、`run_id` 含非法字符（只允许字母、数字、`.`、`_`、`-`）、`max_iterations` 越界。响应体会指出具体字段。

### 取消返回 404 / 启动返回 409

- 404：`run_id` 不存在或该次运行已结束并从注册表移除。取消只能作用于**进行中**的运行。
- 409：同一个 `run_id` 已在运行。换一个 id，或等待结束。
- 取消是协作式的：在下一个迭代边界生效，不会中断正在进行的单次模型/工具调用。
- 409 `session ... is busy`：同一个 `session_id` 已有进行中的 Run；等待它结束或换一个会话。

### 403 / 429 与 Session 参数错误

- 403：`read` scope 的 Key 只能运行 `approval_mode=auto_read`，也不能调用审批接口。
- 429：超过 `BALE_HTTP_RATE_LIMIT_PER_MINUTE`，按 `Retry-After` 秒数退避；设为 `0` 可关闭限流。
- 400 `session_id requires BALE_HTTP_SESSION_DB`：先配置 Session 数据库，再传 `session_id`。

### 审批不出现或写入被拒绝

- 必须设置 `BALE_HTTP_APPROVALS=true`；否则 `approve_writes=false` 时写/执行调用被直接拒绝，不会产生审批。
- 用 `GET /v1/runs/{run_id}/approvals` 查询挂起项；SSE 客户端会收到 `approval_requested` 与 `approval_decided` Trace 事件。
- 超过 `BALE_HTTP_APPROVAL_TIMEOUT_SECONDS` 未决定、Run 结束或取消时，剩余审批按拒绝处理。
- 审批只在进程内保存；进程重启或多副本部署时，调用方需要重新发起 Run。

### SSE 长时间没有事件

- 用 `curl -N` 禁用客户端缓冲；反向代理需关闭响应缓冲（服务端已发送 `X-Accel-Buffering: no`）。
- 正常顺序是 `event: run` → 若干 `event: trace` → `event: result`；异常时是 `event: error`。
- 客户端断开时服务端会取消执行；若仍占用资源，检查是否使用了会一直缓冲的中间层。

### 应用启动即失败（MCP 配置）

`BALE_MCP_CONFIG` 指向的文件不存在、JSON 非法、服务器名重复或字段不合法时，应用会在 lifespan 启动阶段失败。这是预期行为。先用 CLI 验证配置：

```bash
bale run --workspace . --message "hello" --mcp-config servers.json --yes
```

## MCP

- 先按 [快速启动 §10](quickstart.md#10-常见问题排查) 检查解释器、`mcp` 版本与服务端 stderr。
- `mcp_error`：启动/初始化/协议层失败；`mcp_tool_error`：服务端返回 `isError=true`。后者通常是工具业务失败，不是连接断开。
- `read_only` 只是调用方声明：声明为只读的工具才会与其他只读工具并行执行；它**不会**隔离服务器权限。
- `bale chat` 与 HTTP 入口在各自生命周期内复用会话；`bale run` 每次重建。服务端日志写到 stderr，避免污染 stdout 的 JSON-RPC 通道。

## 检索与评测

### `bale search` 没有命中

1. 换用代码中真实出现的标识符或注释词；未启用 embedding 时纯自然语言描述可能没有词法重叠，混合检索可缓解但不保证召回。
2. `--path` 是否限定到了错误子目录。
3. 目标文件是否被跳过：隐藏目录、`__pycache__`/`node_modules`/`dist`/`build` 等、二进制、非文本后缀、超过 256 KiB 的文件，以及索引上限 2000 个文件。
4. 确认工作区路径正确：`--workspace` 指向的是包含代码的根目录。

### 首次检索很慢

索引是惰性构建的，第一次调用会扫描工作区并可能调用 embedding。用 `--path` 缩小范围，或设置 `BALE_RETRIEVAL_INDEX_PATH` 持久化快照：后续运行按文件签名复用未变化的片段与向量，只处理新增、修改和删除的文件。

### 向量检索没有生效

1. `bale search --json` 中 `vector_search` 为 `false`，说明没有可用的向量：检查 `BALE_EMBEDDING_MODEL` 是否设置，`BALE_EMBEDDING_BASE_URL`/`BALE_EMBEDDING_API_KEY`（或回退的 `BALE_OPENAI_*`）是否可用，以及 `BALE_RETRIEVAL_HYBRID` 是否被关闭。
2. embedding 端点失败时索引会静默退回 BM25，不阻断搜索；查看服务端日志确认请求是否到达。
3. `BALE_RETRIEVAL_ALPHA=1` 表示完全使用 BM25；想更依赖向量请调低该值。
4. 修改文件后，同一进程内的检索器不会自动重建索引；重启进程或重新调用 `bale search` 才会读取新快照。

### 自动检索让上下文变大或结果跑偏

```bash
BALE_RETRIEVAL_ENABLED=false bale run ...     # 完全关闭
BALE_RETRIEVAL_TOP_K=2 bale run ...           # 减少片段
```

检索只按当前用户消息召回，不感知历史；片段被标注为 untrusted，模型仍需用工具核实。

### `bale eval` 失败或退出码为 1

- `no tasks in ...`：任务文件为空或目录下没有 `tasks.json`。
- `fixture not found: ...`：`fixture` 路径相对于任务文件所在目录解析，检查目录名。
- FakeProvider 只会返回脚本化文本，无法真正修改文件，因此固定任务通常失败；评测真实能力需要配置真实 Provider。
- 退出码 1 表示有任务未通过，不等于 harness 崩溃；报告中的 `error` 才是执行异常。

## 收集诊断信息

提交 Issue 时请附上：

```bash
python --version
python -m pip show bale-harness mcp
bale --help
python -m pytest -q
```

以及：复现命令（隐去密钥）、`BALE_PROVIDER`/`BALE_OPENAI_BASE_URL`/`BALE_OPENAI_MODEL`、操作系统、完整错误输出。不要把 `.env`、API Key、Trace 中的业务内容直接贴出。
