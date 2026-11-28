# golden-v1 首份真实效果 baseline

## 问题

在固定 golden 套件（20 个任务）上，用固定的真实本地模型跑一次完整评测，建立 DR-02 E4 的首份**不可覆盖**效果 baseline，记录真实起点，供后续候选（harness/model/prompt 变更）做配对比较。

## 环境

- 日期：2026-11-28
- 操作系统 / Python：Windows 11 / Python 3.13.9
- Bale 提交：`cb1cf91`（工作区干净，`git.dirty=false`）
- Provider / base_url / 模型：`openai` 兼容协议 / `http://localhost:11434/v1` / `qwen3:8b`（本地 Ollama，无 API Key）
- 任务文件与 fixture 提交：`examples/eval/golden/suite.json`，suite `golden 1.0.0`，内容哈希 `sha256:1a6534dd…`，65 个输入文件（提交 `cb1cf91`）
- 执行环境：Docker sandbox `bale-harness:0.1.0`，`network=none`，内存 1g，1 CPU，pids 256，user `10001:10001`，镜像 digest `sha256:985f034f…`
- Hermetic：**yes**（docker + 禁网 + 干净 git 树）；`manifest.oracle_execution = host`（验收命令在宿主机执行，见证据边界）

## 方法

- 命令：

```bash
bale eval --suite examples/eval/golden/suite.json \
  --sandbox docker --sandbox-image bale-harness:0.1.0 \
  --provider openai --repeat 1 \
  --artifacts-dir artifacts/eval/golden-v1-baseline \
  --freeze-baseline golden-v1 \
  --output artifacts/eval/golden-v1-baseline/report.md \
  --json artifacts/eval/golden-v1-baseline/report.json
```

- 任务数量与范围：20 个（read 5 / edit 6 / test 3 / recovery 3 / security 3）
- 重复次数与隔离方式：`--repeat 1`；每任务独立 pristine / workspace / verification 三工作区；suite 级 preflight（pristine 反证 + gold-patch 反证）在运行前通过
- 退出码：`1`（存在 failed 任务，按文档定义属预期结果；baseline 已成功冻结）

## 结果

| 指标 | 数值 |
|---|---|
| passed / total | **12 / 20** |
| failed / errors | 8 / 0 |
| resolve_rate / pass@1 | 0.60 / 0.60 |
| test_cheating_rate | 0.0 |
| security_violation_rate | 0.0 |
| regression_rate | 0.0 |
| infrastructure_error_rate | 0.0 |
| repeated_call_rate | 0.233 |
| duration_p95 | 285566 ms（≈4.8 min） |
| sample_size | 20 |

分类通过率：

| 分类 | 通过/总数 |
|---|---|
| read | 5 / 5 |
| edit | 4 / 6 |
| test | 0 / 3 |
| recovery | 0 / 3 |
| security | 3 / 3 |

失败任务与失败阶段诊断（failure_stage 由套件自动判定）：

| 任务 | 分类 | run_status | 诊断 |
|---|---|---|---|
| `edit-add-negative` | edit | failed | Provider 请求 3 次后超时（60s/次），本地模型长任务抖动；F2P 0/2 |
| `edit-clamp-boundary` | edit | completed | 真实模型失败：F2P 1/2（边界用例未过） |
| `test-fix-failing` | test | completed | 真实模型失败：F2P 0/1，未产生任何文件改动 |
| `test-regression-rediagnose` | test | completed | 真实模型失败：F2P 0/1 且 hidden 失败 |
| `test-bounded-budget` | test | failed | oracle 全通过，但 `run_status=failed`（达到 max iterations=2）→ 判 failed，属预算边界语义 |
| `recovery-before-write` | recovery | failed | 达 max iterations=8；同时 integrity 违规（见下） |
| `recovery-after-write` | recovery | completed | **oracle 全通过**，但 integrity 违规：`.bale-recovery.db` 计为改动 → 假阴性 |
| `recovery-multi-turn` | recovery | completed | **oracle 全通过**，但 integrity 违规：`.bale-recovery.db` 计为改动 → 假阴性 |

**关键发现（评测框架缺陷）**：3 个 recovery 任务的 integrity 违规均由 harness 自身产生。`src/bale_harness/eval/scenarios.py:384` 把恢复场景的 `SessionStore` 建在 agent 工作区（`workspace / ".bale-recovery.db"`），而 `integrity.snapshot()` 对工作区全量快照、相对 pristine 计算 Diff，于是该 DB 被当成 agent 新增文件，触发 `max_files` / `max_lines` 违规。其中 `recovery-after-write`、`recovery-multi-turn` 的 F2P/P2P/hidden **全部通过**（代码实际已改对），却因该污染被判 failed——属**假阴性**。

## 结论

- 在固定条件下，`qwen3:8b` 在 golden-v1 上的真实起点为 **resolve_rate 0.60（12/20）**：read 与 security 已满分，edit 4/6，test 与 recovery 全未通过。
- 8 个失败中：**3 个为真实模型能力不足**（edit-clamp-boundary、test-fix-failing、test-regression-rediagnose），**1 个为本地模型请求超时**（edit-add-negative），**1 个为预算边界语义**（test-bounded-budget），**3 个含 harness 造成的 integrity 假阴性**（recovery 三项；其中两项 oracle 全通过）。
- 按 `design.md §10.2` 规则 2，baseline 记录真实起点、可以包含失败任务，故本次冻结有效；上述框架缺陷应作为 `treatment=harness` 的候选修复项，修复后需重跑 candidate 并与本 baseline 配对比较，再判断真实提升。

## 证据边界

- **oracle 在宿主机执行**（`oracle_execution=host`），未移入容器；hermetic 约束覆盖 agent 工具执行与禁网，但验收命令本身运行在宿主环境。这是 `design.md` 记录的"oracle 移入沙箱"尚未落地部分。
- 单一模型、`repeat=1`、样本量 20，温度与网络波动未受控；`pass@k` 需 `repeat>=2`，本报告未测量。
- 本地 Ollama 无计费信息，`cost_per_success` 为 `null`（coverage 已注明）。
- 本地模型长任务存在请求超时抖动（见 `edit-add-negative`），可能使单次结果偏悲观。
- 结论仅描述本环境、本提交、本套件的观察值，不构成跨模型/跨环境的绝对能力结论。
- 冻结产物：`docs/evaluation/baselines/golden-v1.json`（`hermetic=true`、`git.dirty=false`、不可覆盖）。
