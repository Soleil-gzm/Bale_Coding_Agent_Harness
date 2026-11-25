# 安全模型与信任边界

Bale 的定位是**本地单用户开发工具**，不是多租户服务。默认的 `LocalSandbox` 不是不可信代码的安全边界；可选的 `DockerSandbox` 提供容器级隔离，但宿主 Docker daemon 仍是信任边界。本文说明各层实际提供的保护、明确的非保证，以及部署时的最低要求。

## 信任模型

- 受信任：工作区中的代码与配置、启动 Bale 的用户、用户显式提供的 MCP 服务器、CLI 中的人工审批。
- 不受信任：模型输出、仓库文件内容、工具返回值、MCP 工具结果、Session 记忆、自动检索片段、HTTP 调用方传入的 `workspace` 与消息。
- 结论：审批与工作区边界是最后防线；模型可能被提示注入诱导，不能仅依赖系统提示中的「untrusted」标签。

## 边界一览

| 层 | 提供什么 | 不提供什么 |
|---|---|---|
| `WorkspaceGuard` | 规范化并拒绝 `..`、绝对路径越界与符号链接逃逸 | 不是操作系统级沙箱；子进程仍可访问工作区外文件 |
| 审批策略 | 读工具自动放行；写/执行按 `ApprovalMode` 逐次审批，无审批器即拒绝 | 不判断命令内容是否安全；`--yes`/`auto_approve` 会全部放行 |
| `LocalSandbox` | 不经 shell、命令拒绝列表、环境变量白名单、工作目录限制、超时、输出截断、进程树清理 | 明确**不是**安全边界；通过解释器仍可执行任意代码 |
| `DockerSandbox` | 每命令一次性容器、无网络、只读根文件系统、非 root、cap-drop、CPU/内存/PID 限额、超时清理 | 依赖宿主 Docker daemon；镜像内容与内核共享仍是信任假设，未配置自定义 seccomp/AppArmor |
| `ShadowGit` | 独立 git-dir 快照、Diff 与恢复 | 不加密、不防篡改；恢复会覆盖当前工作区 |
| MCP 适配 | stdio 子进程、工具级审批与超时、错误归一化 | 不隔离服务器权限；`read_only` 只是声明 |
| HTTP 入口 | 可选 Bearer 认证（多 Key + principal/scope）、内存限流、SQLite Session、HTTP 审批、JSONL 审计、运行取消、默认拒绝写入 | 无 TLS；限流为单进程内存；`workspace` 仍由调用方指定 |
| 上下文注入 | 记忆与检索片段标注为 untrusted | 不能阻止模型执行其中的恶意指令 |

## 工作区边界

所有内置文件工具都通过 [`WorkspaceGuard`](../src/bale_harness/agent/security.py) 解析路径：根目录先 `resolve()`，候选路径也 `resolve()`，再用 `relative_to(root)` 检查包含关系，因此 `..`、工作区外的绝对路径和指向外部的符号链接都会在访问前被拒绝，错误文本为 `path escapes workspace: ...`。

注意：

- `search` 的 `--path` 与 `exec` 的 `cwd` 也走同一守卫。
- `exec` 只约束**工作目录**，不约束命令参数；`LocalSandbox` 下 `python -c "...open('/etc/passwd')..."` 仍可越界。需要强隔离时启用 `DockerSandbox`（`--sandbox docker` / `BALE_SANDBOX_BACKEND=docker`）。
- 工作区根本身若是指向外部的符号链接，会被解析为真实路径；请确保启动时传入的是预期目录。

## 审批与副作用

审批由 [`PolicyApprover`](../src/bale_harness/agent/approval.py) 按 `ToolSpec.side_effect` 执行：

- `auto_read`：读工具自动放行，写/执行一律拒绝。
- `approve_write`（默认）：读自动放行，写/执行逐次询问；没有 decider 时拒绝。
- `approve_all`：所有调用都询问。

CLI 的 `--yes` 使用 `auto_approve`，HTTP 的 `approve_writes=true` 同理。二者都等于**放弃逐次确认**，只应在受控目录与可信调用方使用。MCP 工具默认按写副作用处理，只有配置 `read_only=true` 才作为只读工具并行执行；这是声明，不是权限隔离。

## 本地执行

[`LocalSandbox`](../src/bale_harness/agent/sandbox.py) 的保护是分层的尽力而为：

- 不经过 shell，始终 `create_subprocess_exec`。
- 拒绝常见破坏性/网络命令（`rm`、`format`、`curl`、`ssh`、`powershell` 等）。
- 只透传环境变量白名单（`PATH`、`HOME`、`TEMP`、`VIRTUAL_ENV` 等），默认丢弃 API Key 与代理凭证。
- 工作目录必须在工作区内；默认超时 120 秒；stdout/stderr 各截断到 30,000 字符；超时后尝试结束进程树。

这些措施降低误操作风险，但**不能**阻止解释器或编译器执行任意代码。不要把 `LocalSandbox` 当作安全边界。

[`DockerSandbox`](../src/bale_harness/agent/sandbox.py) 为每条命令启动独立容器：

- 工作区以 bind mount 挂载到 `/workspace`，容器内 `cwd` 不能逃逸；
- 默认 `--network none`、`--read-only`、`--tmpfs /tmp`、`--cap-drop ALL`、`no-new-privileges`；
- 默认非 root 用户（`10001:10001`），资源限额由 `BALE_SANDBOX_MEMORY`、`BALE_SANDBOX_CPUS`、`BALE_SANDBOX_PIDS_LIMIT` 控制；
- 只注入固定环境变量与工具显式参数，不继承宿主环境；
- 超时或取消时执行 `docker rm -f`，避免遗留容器。

仍需注意：Docker 容器不是虚拟机，宿主内核、Docker daemon 与镜像供应链都是信任边界；工作区可写时，容器内命令仍能修改挂载内容。不要运行来源不明的镜像，也不要在容器内挂载额外敏感目录。

## 快照与恢复

`ShadowGit` 使用独立 `--git-dir`，避免污染用户仓库；快照目录可能位于工作区之外，并包含工作区文件副本。请像保护源码一样保护它：

- 不要提交到版本库或公开分享。
- `bale undo`/恢复是破坏性操作，会覆盖当前工作区改动；执行前先自行备份。
- 同一工作区的并发运行可能互相覆盖快照；当前没有锁或并发协调。

## MCP 服务器

- MCP 服务器以当前用户权限运行；只连接你信任的实现。
- 本项目使用 stdio transport；服务器 stdout 必须是 JSON-RPC，日志应写 stderr。
- MCP Python SDK 默认只向子进程传递少量基础环境变量（`PATH`、`HOME`、`TEMP` 等）；配置中的 `env` 会在此基础上补充。不要把密钥写进 MCP 配置或仓库。
- `mcp_error` 表示连接/协议失败，`mcp_tool_error` 表示服务端返回 `isError=true`；两者都会作为工具错误回给模型，不会自动中断 Turn。
- `bale chat` 与 HTTP 在各自生命周期内复用会话；`bale run` 每次重建。服务器崩溃会影响当前进程中的后续调用。

## HTTP 入口

- 设置 `BALE_HTTP_API_KEY` 或 `BALE_HTTP_API_KEYS` 后，所有 `/v1` 路由要求 `Authorization: Bearer <key>`，使用 `hmac.compare_digest` 逐个比较；`/health` 始终公开。
- 多 Key 时每个 Key 映射 `principal` 与 `scope`。`read` 只能运行 `approval_mode=auto_read` 且 `approve_writes=false` 的 Turn，也不能调用审批接口；认证结果通过 `X-Bale-Principal` 回传并写入审计。
- 限流按 `principal`（未配置 Key 时按客户端 IP）使用进程内令牌桶；超过 `BALE_HTTP_RATE_LIMIT_PER_MINUTE` 返回 429 与 `Retry-After`。`/health` 不消耗配额。令牌桶不跨进程共享，重启即清空。
- `BALE_HTTP_SESSION_DB` 把 Run 记录写入本地 SQLite；同一 `session_id` 的并发 Run 返回 409。Session 记录包含用户消息、工具状态与答案，应按源码同等级别保护该文件。
- `BALE_HTTP_APPROVALS=true` 时，写/执行调用会挂起等待 `POST /v1/runs/{run_id}/approvals/{approval_id}` 的决定；超时（`BALE_HTTP_APPROVAL_TIMEOUT_SECONDS`）或 Run 结束/取消一律按拒绝处理。审批参数来自模型输出，可能包含提示注入内容，批准前必须核对工具名与参数，而不是只看模型摘要。
- `GET /v1/runs/{run_id}/approvals` 暴露 `arguments`；只应允许可信调用方访问，且不要在日志中留存完整参数。
- `BALE_HTTP_AUDIT_LOG` 追加 `principal`、`run_id`、`session_id`、状态与审批结果；不记录提示词、答案和工具参数，但可能包含工作区路径。
- `workspace` 由调用方指定，服务端不会限制到你预期的目录；**只应在本机或可信内网监听**，公网部署需自行加 TLS、认证网关与限流。
- 默认 `approve_writes=false` 且未开启审批时，写/执行工具直接拒绝；把它设为 `true` 等于信任所有调用方。
- `run_id` 取消是协作式的，不保证立即停止正在进行的模型/工具调用；取消会同时拒绝该 Run 挂起的审批。
- SSE 会回传 Trace 事件；Trace 不包含提示词、答案与工具参数，但仍可能包含路径与状态，注意日志访问控制。

## 上下文注入与提示注入

- Session 记忆、自动检索片段与工具输出都可能包含攻击者可控文本。系统前缀会把记忆和检索片段标注为 untrusted，但这只是降低风险，不是强制隔离。
- 检索片段来自工作区文件，可能包含「忽略之前的指令」之类内容；不要在没有审批的情况下让 Agent 对不可信仓库执行写入或命令。
- 启用向量检索后，片段文本会发送到 `BALE_EMBEDDING_BASE_URL` 指向的端点（或回退的 `BALE_OPENAI_BASE_URL`）进行 embedding；不要把不可外发的代码索引到远程服务。索引快照包含片段文本与向量，应按代码同等敏感度保护。
- 模型可能把文件内容当作指令；审批弹窗应展示真实的工具名与参数，而不是只看模型摘要。
- 外部输入（HTTP 消息、Issue 文本、网页抓取结果）与仓库文件同等对待。

## 密钥与敏感数据

- `BALE_OPENAI_API_KEY` 使用 `SecretStr`，不会在 `repr`/日志中直接展开；`.env` 不应提交。
- `LocalSandbox` 默认丢弃密钥类环境变量；`DockerSandbox` 不继承宿主环境，只注入固定变量与工具参数；MCP 子进程按 SDK 白名单继承，显式 `env` 可能带入密钥，需自行审查。
- Trace 只记录事件、耗时与状态，不记录提示词、答案和工具参数；不要把密钥写进路径、工具名或自定义 metadata。
- 运行报告与 Trace 文件可能包含工作区路径、命令与退出码；分享前先脱敏。

## 部署最低要求

1. 在专用工作区运行，不指向家目录、系统目录或包含生产凭证的仓库。
2. 保持默认 `approve_write`；仅在隔离环境使用 `--yes` / `approve_writes=true`。
3. HTTP 仅监听 `127.0.0.1` 或内网，并设置 `BALE_HTTP_API_KEY`；公网场景自行加 TLS 与限流。
4. 只连接可信 MCP 服务器；审查其 `env` 与可执行文件。
5. 定期清理 Checkpoint、Trace、Session 与 MCP `output/`，不要把敏感数据留在临时目录。

## 已知限制

- 默认无 Docker/VM 隔离；启用 `DockerSandbox` 后有容器级文件系统、网络与资源限制，但仍是共享宿主内核，不是 VM 级隔离。
- `DockerSandbox` 依赖宿主可用的 Docker CLI/daemon；Bale 自身运行在容器内时，需要额外安装 Docker CLI 并挂载 socket，部署镜像默认不包含。
- 未提供自定义 seccomp/AppArmor 配置、镜像签名校验或每命令网络白名单。
- 无细粒度权限（按工具/路径/命令的 ACL）；HTTP 只有 `read`/`write` 两种 scope。
- 限流与审批状态保存在进程内存，多副本部署不共享；重启后挂起的审批与令牌桶都会失效。
- 审批超时只按拒绝处理，不会自动通知调用方；需要可靠通知时应消费 `approval_requested` Trace 事件。
- `read_only` 与 `untrusted` 都是约定/标注，不是强制机制。
- 同一 Session 的并发 Run 会被拒绝，但不同 Session 共用同一工作区时仍可能互相覆盖 Snapshot 与文件；Session、审批与 MCP 会话均按单进程假设设计。
