# Bale Coding Agent Harness — 问题清单与改进跟踪

- 生成日期：2026-11-28
- 配套报告：[review-report.md](review-report.md)
- 严重度定义：**阻断**（破坏 CI/演示/安全边界，必须立即处理）/ **高**（明显误导或功能不符）/ **中**（一致性或健壮性问题）/ **低**（优化或文档措辞）
- 状态：✅ 已解决 / ⚠️ 待处理 / 📝 仅记录（已知限制，非缺陷）

---

## 一、阻断性问题（演示前必须处理）

| ID | 严重度 | 标题 | 位置 | 描述 | 状态 | 处理 |
|---|---|---|---|---|---|---|
| B1 | 阻断 | `ruff check` 门禁断裂（UP038） | `repo_index/index.py:228`、`repo_index/store.py:88` | 锁文件钉住 `ruff==0.17.6`，`ruff check src tests` 报 2 处 UP038，CI Lint 步骤为红，文档声明的基础检查不通过。 | ✅ 已解决 | 提交 `892ce10` 修复，`ruff check` 现通过。 |
| B2 | 阻断（环境） | 本地 `.env` 的 base_url 不可达 | 仓库根 `.env`（gitignored，未跟踪） | `.env` 有意配置 Ollama（qwen3:8b），但 `base_url` 误用 `host.docker.internal:11434`（仅容器内有效，实测宿主机不可达），导致宿主机直跑 `bale run` connection refused。 | ✅ 已解决 | 已将 `base_url` 改为 `localhost:11434`，真实模型 `bale run` 实测通过（14.5s）。Docker 沙箱亦实测可用（13 测试通过）。勿提交含密钥的 `.env`。 |

---

## 二、实现-文档漂移

| ID | 严重度 | 标题 | 位置 | 描述 | 状态 | 建议动作 |
|---|---|---|---|---|---|---|
| D1 | 高 | 评测设计文档状态滞后 | `docs/evaluation/design.md:4` | 页眉标注"设计待实施"，但 golden v2 suite、runner v2 与 CI 结构回归门禁均已实现，文档与代码/CI 矛盾。 | ✅ 已解决 | 提交 `05726c8`：状态改为 E1–E3 已实现、E4 部分完成，§15 勾选已达标项。 |
| D2 | 中 | 文档断裂引用 | `docs/evaluation/design.md:6-7` | 引用的 `../../../docs/eval.md`、`../../../docs/bale-harness-architecture.md` 在仓库中不存在。 | ✅ 已解决 | 提交 `fb3accc`：方法论标注为外部参考，原始目标改指向本仓库 `architecture.md`。 |
| D3 | 中 | E4 完成度表述夸大 | `docs/evaluation/README.md:5` | 称"DR-02 E4 已落地"，但 `docs/evaluation/baselines/` 仅含 `.gitkeep`，无冻结 baseline；`design.md §13.4/§15` 要求 E4 含真实 baseline 与 candidate 对比。 | ✅ 已解决（表述） | 提交 `985b142`：改为 E1–E3 已落地、E4 部分完成。**剩余**：产出首份真实效果 baseline（E4 收尾，见优先级汇总）。 |
| D4 | 中 | quickstart 陈旧表述 | `docs/quickstart.md:3` | 仍写"Docker 容器隔离尚未实现"，与已实现的 `DockerSandbox` 矛盾。 | ✅ 已解决 | 提交 `7aa66ca`：改为"已实现"，并指向 §4.2。 |
| D5 | 低 | sandbox 模块 docstring 陈旧 | `src/bale_harness/agent/sandbox.py:9` | 称 Docker 适配器是"later milestone"，但 `DockerSandbox` 已实现。 | ✅ 已解决 | 提交 `26ed5de`：docstring 改为引用 `DockerSandbox`。 |
| D7 | 低 | 环境配置 | 仓库根 `.env` | 同 B2，本地未跟踪文件的 `base_url` 配置问题。 | ✅ 已解决 | 同 B2，已改 `localhost` 并实测通过。 |

> D1–D5 已于 2026-11-28 全部修复（提交见各行）；B1 为代码缺陷，已修复（892ce10）；B2/D7 为本地环境配置问题，已修正 `.env` 的 `base_url` 并实测通过（真实模型 + Docker 沙箱均验证）。

---

## 三、风险登记（含已知限制）

| ID | 类别 | 严重度 | 位置 | 描述 | 性质 | 状态 |
|---|---|---|---|---|---|---|
| R1 | 稳定性 | 中 | `providers_openai.py:59` | `asyncio.to_thread` 执行阻塞 urllib，取消不中断进行中的线程（受 `timeout_s` 约束），协作式取消对模型调用不即时。 | 已知限制 | 📝 已记录（文档已声明） |
| R2 | 安全 | 中 | `agent/security.py` | `WorkspaceGuard` 检查与打开之间存在 TOCTOU 窗口。 | 已知限制 | 📝 已记录（文档已声明非 OS 沙箱） |
| R3 | 稳定性 | 中 | `http.py`/`http_support.py` | HTTP 状态/审批/限流为进程内内存，重启丢失、多副本不共享。 | 已知限制 | 📝 已记录（非目标） |
| R4 | 安全 | 中 | `agent/checkpoint.py` | 同工作区并发 Run 无锁，可互相覆盖快照/文件。 | 已知限制 | 📝 已记录 |
| R5 | 安全 | 低 | 部署 | HTTP 无内置 TLS，`workspace` 由调用方任意指定。 | 部署责任 | 📝 已记录（文档已要求外层加固） |
| R6 | 安全 | 低 | `agent/sandbox.py` LocalSandbox | 非安全边界，解释器可执行任意代码。 | by-design | 📝 已记录（文档已充分警示） |
| R7 | 性能 | 低 | `repo_index/` | 索引构建在调用线程懒加载，大仓库首次检索阻塞；已设文件/片段上限。 | 可接受 | 📝 已记录（快照可缓解） |
| R8 | 稳定性 | 低 | 环境 | 本机 `pip check` 报 `pyopenssl`/`streamlit` 冲突，非项目依赖，属共享环境污染。 | 环境产物 | 📝 已记录（干净 venv 不出现） |

---

## 四、改进建议优先级汇总

1. **高**：~~修正 `design.md` 状态（D1）与死链（D2）~~ ✅；**待做**：产出首份真实效果 baseline 完成 E4 收尾（D3 剩余工作）。
2. **中**：~~修正 `quickstart.md`（D4）与 `sandbox.py` docstring（D5）、统一 E4 表述（D3）~~ ✅ 已完成。
3. **低**：同工作区并发 Run 加文件锁（R4）；评估异步 HTTP 客户端改善取消及时性（R1）；缩小 TOCTOU 窗口（R2）。

---

## 五、核对通过项（无问题，作为基线记录）

- CLI 12 个子命令全部存在且与文档一致 ✅
- HTTP 9 条路由全部存在且与文档一致 ✅
- `settings.py` 32 个 `BALE_*` 环境变量与 `configuration.md` 完全一致 ✅
- 全部文档化内置工具已注册 ✅
- `src/` 无 `TODO/FIXME/NotImplementedError` 遗留 ✅
- 认证使用 `hmac.compare_digest`（时序安全）✅
- 重试有界、无死循环 ✅
- 错误信息密钥脱敏 ✅
- HTTP 运行历史上限 200（防内存膨胀）✅
- SSE 路径 `finally` 释放门禁 ✅
- 测试：578 passed / 7 skipped（Docker）/ 0 failed ✅
