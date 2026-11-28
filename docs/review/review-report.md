# Bale Coding Agent Harness — 代码审查与一致性校验报告

- 审查日期：2026-11-28
- 审查范围：`src/bale_harness/`、`tests/`、`examples/`、根目录配置与 `docs/` 全部文档
- 基线提交：`60038be`（审查起始）→ 修复提交：`892ce10`
- 执行环境：Windows 11、Python 3.13.9（文档声明 3.11.18 验证；3.13 同样通过全部测试）、Docker 29.9.3 可用（daemon 运行中）、Ollama 运行中（qwen3:8b 等模型已就位）
- 审查方法：文档基线建立 → 静态映射（CLI/HTTP/环境变量/工具/模块）→ 关键源文件核验 → 运行 `ruff`/`pytest`/`compileall` → CLI 端到端冒烟

---

## 0. 执行摘要

**总体结论：项目处于"早期建设阶段"的尾声，核心 MVP 已全部完成并可演示；代码质量高、测试覆盖充分（578 passed），但存在少量文档-实现漂移与一处已修复的 lint 门禁断裂。**

| 维度 | 结论 |
|---|---|
| 实现 vs 文档 | 功能实现普遍超前于部分文档表述；发现 **7 处漂移点（D1–D7），现已全部修复**（修复提交见 §1.2 与附录 B） |
| 开发进度 / MVP | **MVP 已完成**。B0–B10 全部落地；评测 DR-02 的 E1–E3 已实现，E4 仅缺"真实效果 baseline 产物" |
| 测试状态 | `pytest`：**578 passed / 7 skipped / 0 failed**（7 个为 Docker 集成测试，默认未设 `BALE_TEST_DOCKER=1` 时跳过）；Docker 实际可用，设 `BALE_TEST_DOCKER=1` 后 13 个 Docker 沙箱测试全过 |
| 静态检查 | `ruff check` 修复后 **通过**；`ruff format --check` **通过**；`compileall examples` **通过** |
| 可演示性 | **具备可演示条件**。离线路径（FakeProvider）的 CLI `turn`/`run`/`search` 端到端验证通过；演示前需处理 1 项环境注意事项（见 §4） |
| 已提交修复 | `892ce10`（lint UP038）、`05726c8`（design 状态）、`fb3accc`（死链）、`985b142`（E4 表述）、`7aa66ca`（quickstart）、`26ed5de`（sandbox docstring）、`.env` base_url 修正（本地，未入库）。完整清单见附录 B |

---

## 1. 实现与设计一致性校验（漂移点清单）

审查方式：以 `docs/` 文档为设计基线，逐项核对 `src/` 中的实际符号、CLI 命令、HTTP 路由、环境变量与工具注册。

### 1.1 核对通过项（无漂移）

| 核对项 | 结果 | 证据 |
|---|---|---|
| CLI 子命令 | ✅ 全部存在 | `cli.py`：`turn`/`search`/`eval`/`run`/`report`/`session`(list,show)/`trace`/`chat`/`undo`/`checkpoint`(list,clean) 共 12 个 `@app.command`，与文档完全一致 |
| HTTP 路由 | ✅ 全部存在 | `http.py`：`/health`、`POST /v1/turns`、`POST /v1/runs`、`GET /v1/runs/{run_id}`、`GET /v1/runs/{run_id}/report`、`POST /v1/runs/stream`、`GET /v1/runs/{run_id}/approvals`、`POST .../approvals/{approval_id}`、`POST .../cancel`，共 9 条，与文档一致 |
| 环境变量 | ✅ 32 项全部匹配 | `settings.py` 字段名、类型、默认值与 `configuration.md §2` 完全一致（含 `BALE_SANDBOX_*`、`BALE_RETRIEVAL_*`、`BALE_EMBEDDING_*`、`BALE_HTTP_*` 等），无遗漏、无默认值偏差 |
| 内置工具 | ✅ 全部存在 | `tools/builtin.py`(read_file/list_files/search_files)、`write.py`(write_file/edit_file/move_file/delete_file/apply_patch)、`exec.py`(shell/run_tests)、`git.py`(git_diff/finish)、`code_search.py`(search_code) |
| 模块完整性 | ✅ 全部存在 | `agent/` 下 loop/context/history/session/memory/trace/cost/report/approval/sandbox/checkpoint 与 `tools/`、`repo_index/`、`eval/`、`http_support.py` 均非平凡实现 |

### 1.2 漂移点（实现与设计不一致）

| # | 严重度 | 类别 | 位置 | 漂移描述 | 状态 / 处理 |
|---|---|---|---|---|---|
| D1 | 高 | 文档状态滞后 | `docs/evaluation/design.md:4` | 页眉标注"**设计待实施**"，但 golden v2 suite（20 任务 5/6/3/3/3）、runner v2（F2P/P2P/hidden、integrity、scenarios、compare、metrics、preflight、baseline 冻结）与 CI `structural-regression` 门禁均已落地，文档状态与代码/CI 矛盾。 | ✅ 已解决（`05726c8`）：状态改为 E1–E3 已实现、E4 部分完成；§15 勾选已达标 8 项。 |
| D2 | 中 | 文档死链 | `docs/evaluation/design.md:6-7` | 引用 `../../../docs/eval.md` 与 `../../../docs/bale-harness-architecture.md`，二者均不存在，为断裂引用。 | ✅ 已解决（`fb3accc`）：方法论标注为外部参考，原始目标改指向本仓库 `architecture.md`。 |
| D3 | 中 | 文档表述夸大 | `docs/evaluation/README.md:5` | 称"**DR-02 E4 已落地**"，但 `baselines/` 仅含 `.gitkeep`，无冻结 baseline；E4 要求含真实 baseline 与 candidate 对比。 | ✅ 已解决（`985b142`）：改为 E1–E3 已落地、E4 部分完成。**剩余**：产出首份真实 baseline（见 §5）。 |
| D4 | 中 | 文档表述陈旧 | `docs/quickstart.md:3` | 仍写"**Docker 容器隔离尚未实现**"，与已实现的 `DockerSandbox` 矛盾。 | ✅ 已解决（`7aa66ca`）：改为"已实现"，并指向 §4.2。 |
| D5 | 低 | 内联文档陈旧 | `src/bale_harness/agent/sandbox.py:9` | 模块 docstring 称"the Docker adapter is a **later milestone**"，但 `DockerSandbox` 已实现。 | ✅ 已解决（`26ed5de`）：docstring 改为引用 `DockerSandbox`。 |
| D6 | 高 | 代码缺陷 | `repo_index/index.py:228`、`repo_index/store.py:88` | `ruff check src tests`（锁文件钉住 `ruff==0.17.6`）报 2 处 UP038，导致 **CI Lint 步骤为红**，文档声明的基础检查不通过。 | ✅ 已解决（`892ce10`）：改用 PEP 604 联合类型写法；`ruff check` 现通过。 |
| D7 | 低 | 环境配置 | 仓库根 `.env`（gitignored） | 本地 `.env` 是有意配置 Ollama（`BALE_PROVIDER=openai`、`qwen3:8b`），但 `BALE_OPENAI_BASE_URL` 误用 `host.docker.internal:11434`（该主机名仅容器内有效，实测宿主机不可达），导致宿主机直跑 `bale run` connection refused。 | ✅ 已解决：`base_url` 改为 `localhost:11434`，实测真实模型 `bale run` 跑通。注意：勿提交含密钥的 `.env`。 |

> 说明：D1–D5 已于后续提交修复（见 issue-checklist.md）；D6 为阻断性代码缺陷，已修复（892ce10）；D7 为本地环境配置问题，已修正 `.env` 的 `base_url` 并实测通过。

---

## 2. 开发进度评估（MVP 完成度）

项目以里程碑 B0–B10 组织（见 `docs/architecture.md`），未显式使用"MVP"一词。审查据此映射当前阶段：

### 2.1 里程碑状态

| 里程碑 | 内容 | 状态 | 证据 |
|---|---|---|---|
| B0 | 核心契约与入口（Turn/ToolResult/Settings/Provider/CLI+HTTP Turn 构造） | ✅ 完成 | `contracts.py`/`settings.py`/`providers*.py`/`turns.py`/`cli.py`/`http.py` + 测试 |
| B1 | 只读 ReAct 运行时 | ✅ 完成 | `agent/loop.py`、`tools/registry.py`/`builtin.py`、`security.py` |
| B2a | 写入运行时（写/执行工具、审批、ShadowGit） | ✅ 完成 | `tools/write.py`/`exec.py`/`git.py`、`approval.py`、`checkpoint.py` |
| B3 | 上下文与会话（预算/裁剪/Session/恢复） | ✅ 完成 | `context.py`/`history.py`/`session.py`/`memory.py` |
| B4 | 追踪与成本 | ✅ 完成 | `trace.py`/`cost.py` |
| B5 | 运行报告 | ✅ 完成 | `report.py` + `bale report` |
| B6 | 本地闭环增强（持久化 Checkpoint/undo/chat/扩展写工具/OpenAI Provider） | ✅ 完成 | `cli.py` undo/checkpoint/chat、`providers_openai.py` |
| B7 | HTTP 执行与 SSE | ✅ 完成 | `http.py` runs/stream/health/取消 |
| B8 | 检索与评测 | ✅ 完成 | `repo_index/`、`eval/`、`bale search`/`bale eval` |
| B9 | MCP 工具接入 | ✅ 完成 | `tools/mcp.py`、`examples/mcp/`、CLI/HTTP 接入 |
| B10 | HTTP 生产化 + Docker 沙箱 | ✅ 完成 | `http_support.py`（认证/限流/Session/审批/审计）、`DockerSandbox` |
| DR-02 E1 | 评测正确性与安全底座（schema v2/非空 oracle/fixture 约束/独立 verification/负例） | ✅ 完成 | `eval/task.py`/`integrity.py`/`oracle.py`/`runner.py` + 测试 |
| DR-02 E2 | 场景执行与 20 个 Golden Tasks | ✅ 完成 | `examples/eval/golden/suite.json` 20 任务 5/6/3/3/3、`eval/scenarios.py` |
| DR-02 E3 | 版本化报告、证据与 baseline 对比 | ✅ 完成 | `eval/metrics.py`/`compare.py`/`manifest.py`/`report.py` |
| DR-02 E4 | 首份效果 baseline 与 CI 门禁 | ⚠️ 部分 | CI 结构回归门禁 ✅（`.github/workflows/ci.yml`）；**真实模型效果 baseline 未产出**（`baselines/` 为空） |

### 2.2 已完成 / 进行中 / 未开始

- ✅ **已完成**：B0–B10 全部运行时能力；评测结构回归（DR-02 E1–E3 + E4 的 CI 门禁）；全部文档化 CLI/HTTP/工具/环境变量；578 项自动化测试；静态检查门禁（修复后）。
- 🟡 **进行中 / 收尾**：DR-02 E4 —— 产出首份真实模型 baseline 并完成一次 candidate 配对比较（代码能力已具备，缺真实模型运行产物）。
- ⬜ **未开始（明确为非 MVP 目标，见 `architecture.md §2.2/§4`）**：Knowledge Retriever、多副本共享状态、流式 Provider 输出、多模型路由、Web 前端、生产级多副本隔离、自定义 seccomp/AppArmor、镜像签名校验。

### 2.3 MVP 判定

**MVP 已完成。** 理由：一个可本地运行的 Coding Agent 运行时闭环（CLI/HTTP 入口 → ReAct 循环 → 工具治理 → 受控执行 → 状态恢复 → 追踪/报告 → 检索 → 评测 → MCP 接入）已全部具备并通过 578 项测试与端到端冒烟。剩余项均为文档明示的"后续/非目标"扩展，不构成 MVP 缺口。

---

## 3. 潜在风险排查

按稳定性、安全、可维护性、性能四类，区分**真实缺陷**与**文档已知限制**。

### 3.1 已确认的良好实践（降低风险）

- **认证时序安全**：`http_support.py` Bearer 比对使用 `hmac.compare_digest`（`L67-L78`）。
- **重试有界**：`providers_openai.py` 重试上限 `max_retries+1`、指数退避，4xx(非 429) 立即失败，无死循环（`L57-L82`）。
- **密钥脱敏**：错误信息经 `_redact()` 替换 key（`providers_openai.py:210-213`），且 4xx 错误不回显响应体。
- **运行历史有界**：HTTP Run 状态内存保留上限 `MAX_RUN_HISTORY = 200`，LRU 淘汰（`http.py:147-181`）。
- **SSE 清理**：流式路径在 `finally` 中释放 Session 门禁（`http.py:384-440`）。
- **无遗留标记**：`src/` 全量 grep 无 `TODO/FIXME/XXX/HACK/NotImplementedError`。
- **沙箱清理**：`DockerSandbox` 在超时/取消/异常路径均调用容器清理（`sandbox.py:425-489`）。
- **提交规范**：Git 历史为英文小步提交（feat/fix/docs/chore/test/ci），符合本报告要求的提交约定。

### 3.2 风险登记

| ID | 类别 | 严重度 | 位置 | 风险描述 | 性质 | 建议 |
|---|---|---|---|---|---|---|
| R1 | 稳定性 | 中 | `providers_openai.py:59` | 模型调用通过 `asyncio.to_thread` 执行阻塞的 `urllib`，取消 asyncio 任务**不会中断进行中的线程**，线程会持续到自身 `timeout_s`（默认 60s）才结束。协作式取消对正在进行的模型调用不即时生效。 | 已知限制（文档已声明取消为协作式） | 可接受；如需更及时取消，可改为可取消的 `httpcore`/`httpx` 异步传输，但会引入新依赖。 |
| R2 | 安全 | 中 | `agent/security.py` | `WorkspaceGuard` 在 `resolve()` 检查与工具实际打开文件之间存在 **TOCTOU** 窗口：检查后、打开前路径可被替换为逃逸符号链接。 | 已知限制（文档声明 Guard 非 OS 级沙箱） | 强隔离场景应启用 `DockerSandbox`；如需更严格，可在打开时用 `os.open` + 目录 fd 校验。当前为可接受的本地工具定位。 |
| R3 | 稳定性 | 中 | `http.py` / `http_support.py` | HTTP 的 Run 状态、审批 Broker、限流令牌桶均为**进程内内存**，重启即丢失，多副本不共享。文档已声明。 | 已知限制（非目标） | 单进程部署可接受；多副本需外部存储，属后续规划。 |
| R4 | 安全 | 中 | `agent/checkpoint.py` / 文档 | 同一工作区的并发运行**无锁协调**，可能互相覆盖 ShadowGit 快照与文件。 | 已知限制 | 避免同工作区并发 Run；后续可加文件锁。 |
| R5 | 安全 | 低 | `Dockerfile` / 部署 | HTTP 服务**无内置 TLS**；`workspace` 由调用方任意指定，未限制到预期目录。 | 已知限制（部署责任） | 文档已要求仅监听本机/内网并在外层加 TLS + 认证网关；生产部署必须遵守。 |
| R6 | 安全 | 低 | `agent/sandbox.py` LocalSandbox | `LocalSandbox` 明确不是安全边界，解释器仍可执行任意代码；deny-list 为尽力而为。 | 已知限制（by-design） | 不可信代码必须用 `DockerSandbox`；文档已充分警示。 |
| R7 | 性能 | 低 | `repo_index/` | 索引构建发生在调用线程，懒加载；大仓库首次检索可能阻塞。已设上限（2000 文件 / 10000 片段）。 | 可接受 | 已通过 `BALE_RETRIEVAL_INDEX_PATH` 快照缓解。 |
| R8 | 稳定性 | 低 | 环境 | 本机 `pip check` 报 `pyopenssl`/`streamlit` 冲突，但二者**非项目依赖**，是共享全局环境的污染；干净 venv 不会出现。 | 环境产物，非项目缺陷 | 按文档使用独立 venv 安装即可。 |

> 审查未发现新的、未被文档覆盖的高危安全缺陷。核心信任边界（路径防护、审批、沙箱隔离、密钥脱敏、认证）均已实现并有测试覆盖。

---

## 4. 可演示性验证

### 4.1 已验证可运行（端到端冒烟）

| 演示路径 | 结果 | 证据 |
|---|---|---|
| `bale --help` | ✅ 列出全部命令 | 冒烟通过 |
| `bale turn`（构造 Turn） | ✅ 输出合法 Turn JSON，exit 0 | 冒烟通过 |
| `bale run`（FakeProvider，`--yes`） | ✅ `status: completed`，exit 0 | 显式 `BALE_PROVIDER=fake` 下通过 |
| `bale search`（BM25） | ✅ 返回命中片段，exit 0 | 冒烟通过 |
| 测试套件 | ✅ 578 passed / 7 skipped / 0 failed | `pytest -q`，187s |
| 静态检查 | ✅ `ruff check` / `ruff format --check` / `compileall examples` 全通过 | 修复后验证 |

### 4.2 演示前必须处理的阻断项

| # | 阻断项 | 严重度 | 处理方式 | 状态 |
|---|---|---|---|---|
| B1 | `ruff check src tests` 失败（UP038）导致 CI Lint 为红 | 阻断 | 已修复并提交 `892ce10` | ✅ 已解决 |
| B2 | 本地 `.env` 的 `base_url` 误用 `host.docker.internal`（仅容器内有效），宿主机直跑 `bale run` 联网失败 | 演示阻断（环境） | 已改为 `localhost:11434`；真实 Ollama 模型 `bale run` 实测通过（`status: completed`，14.5s） | ✅ 已解决 |

> 除上述外，未发现其他阻断核心功能运行的问题。Docker 实际可用（29.9.3），设 `BALE_TEST_DOCKER=1` 后 13 个 Docker 沙箱集成测试全部通过；本地已有 `bale-harness:0.1.0` 与 `python:3.11-slim` 镜像，可直接演示 `--sandbox docker`。注意：`--sandbox docker` 只沙箱化工具命令执行，模型调用仍由宿主机 bale 进程发起，故 base_url 用 `localhost`；仅当 bale 整体运行于容器内时才需 `host.docker.internal`。

### 4.3 演示建议前置检查清单

1. `.env` 已配置为 Ollama（`localhost:11434` + `qwen3:8b`），真实模型可用；如需离线演示可显式 `BALE_PROVIDER=fake`。
2. 运行 `ruff check src tests`、`ruff format --check src tests`、`pytest -q` 确认全绿（当前已全绿）。
3. 若演示真实模型：配置 `BALE_PROVIDER=openai` + 有效端点/Key；本地 Ollama 可免 Key。
4. 若演示 Docker 沙箱：确认 `docker` 可用，镜像含运行时依赖。
5. 使用独立 venv 按 `docs/quickstart.md §2` 安装，避免全局环境冲突（R8）。

---

## 5. 改进建议（按优先级）

### 高优先级
1. ~~更新 `design.md` 状态（D1）~~ ✅ `05726c8`；~~修正 `design.md` 死链（D2）~~ ✅ `fb3accc`。
2. ~~产出首份真实效果 baseline~~ ✅ 已完成（`baselines/golden-v1.json`，12/20，hermetic；记录见 `docs/evaluation/20261128-golden-v1-baseline.md`）。**剩余**：做一次 candidate 配对比较以收尾 E4；建议优先修复 recovery 场景的 integrity 假阴性（E4-1）后再跑 candidate（`treatment=harness`）。

### 中优先级（文档一致性）——已全部完成
3. ~~修正 `quickstart.md`（D4）与 `sandbox.py` docstring（D5）~~ ✅ `7aa66ca`、`26ed5de`。
4. ~~统一 `evaluation/README.md` 对 E4 的表述（D3）~~ ✅ `985b142`。

### 低优先级（健壮性增强，非必须）
6. 考虑为同一工作区的并发 Run 增加文件锁（R4）。
7. 如需更及时的模型调用取消，评估引入异步 HTTP 客户端替代 `urllib`+`to_thread`（R1）——权衡新增依赖。
8. （可选）`WorkspaceGuard` 打开时二次校验以缩小 TOCTOU 窗口（R2）。

---

## 附录 A：验证命令与结果记录

审查期间在干净安装（`requirements.lock` + `pip install --no-deps .`，Python 3.13.9）下执行：

| 命令 | 结果 |
|---|---|
| `python -m ruff check src tests` | 修复前：2 errors（UP038）；修复后：`All checks passed!` |
| `python -m ruff format --check src tests` | `107 files already formatted` ✅ |
| `python -m pytest -q` | `578 passed, 7 skipped in 187.10s` ✅（7 skipped = Docker 集成测试，默认未设 `BALE_TEST_DOCKER=1`；设上后 Docker 沙箱测试 13 passed） |
| `BALE_TEST_DOCKER=1 pytest tests/test_docker_sandbox.py -q` | `13 passed in 3.05s` ✅（Docker 沙箱实测可用） |
| `bale run`（真实 Ollama qwen3:8b，local 沙箱） | `status: completed`，1 次工具调用，2580 输入/350 输出 tokens，exit 0 ✅ |
| `python -m compileall examples` | exit 0 ✅ |
| `python -m pip check` | exit 1（仅全局环境无关包 `pyopenssl`/`streamlit` 冲突，非项目依赖） |
| `bale turn --message ... --workspace ...` | exit 0，合法 JSON ✅ |
| `BALE_PROVIDER=fake bale run ... --yes` | `status: completed`，exit 0 ✅ |
| `bale search --workspace ... --query ...` | exit 0，返回命中 ✅ |
| `git log --oneline` | 英文小步提交，工作区审查起始时干净 ✅ |

## 附录 B：本报告对应的全部修复提交

| 提交 | 类型 | 说明 |
|---|---|---|
| `892ce10` | fix(lint) | 修复 `repo_index/index.py`、`store.py` 各 1 处 UP038 `isinstance` 写法，恢复 `ruff check` 门禁（D6/B1）。2 文件，+2/-2，行为不变。 |
| `56db0ed` | docs(review) | 新增本审查报告与问题清单（`docs/review/`）。 |
| `05726c8` | docs(eval) | 更新 `design.md` 实现状态 + §15 勾选（D1）。 |
| `fb3accc` | docs(eval) | 修复 `design.md` 两处外部死链（D2）。 |
| `7aa66ca` | docs(quickstart) | 删除"Docker 未实现"陈旧表述（D4）。 |
| `26ed5de` | docs(sandbox) | 更新 `sandbox.py` docstring（D5）。 |
| `985b142` | docs(eval) | 修正 `evaluation/README.md` 的 E4 完成度表述（D3）。 |
| `038420c` | docs(review) | 问题清单标记 D1–D5 已解决。 |
| `32616fc` | docs(review) | 更正 B2/D7 根因与 Docker 可用性描述。 |

另：本地 `.env`（gitignored，未入库）的 `BALE_OPENAI_BASE_URL` 由 `host.docker.internal` 改为 `localhost`（B2/D7），已实测真实模型跑通。

**当前唯一待办**：产出首份真实效果 baseline（DR-02 E4 收尾，见 §5）。
