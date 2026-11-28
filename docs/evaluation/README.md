# 评测指南

本目录记录 Bale 的固定任务评测方式与结果记录规范。它是一个**确定性回归 harness**：用固定 fixture、固定消息和文件级断言检查 Agent 是否真的完成了修改，而不是模型排行榜。

> **本文档描述当前已实现的 `bale eval` 行为。** DR-02 E1–E3 已落地：20 个 Golden Tasks、F2P/P2P 防作弊、五元组版本指纹、hermetic 标记、preflight 数据质量、baseline 冻结与配对比较。E4 部分完成：CI 结构回归门禁已落地，首份真实效果 baseline 尚未产出（`baselines/` 待补充）。目标架构与尚未落地的部分（如 oracle 移入沙箱）见 [Eval 重设计方案](design.md)。

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 把 fixture 复制到临时工作区，隔离原始文件 | 默认在本机执行任务命令，除非显式传 `--sandbox docker` |
| 运行一个真实 Turn（含工具调用与审批） | 不自动生成任务、不做 LLM-as-judge |
| 用 `file_exists` / `file_contains` 等确定性断言判定 | 不比较模型之间的绝对能力 |
| 输出 Markdown/JSON 报告与退出码 | 不承诺跨环境可复现（除非用 hermetic docker 运行） |
| `--sandbox docker` 时禁用网络、标记 `hermetic: true` | |
| preflight 校验 gold_patch 与 pristine P2P/F2P 数据质量 | |
| 冻结不可变 baseline 并做配对比较 | |
| CI 结构回归门禁（fake provider + docker） | |

评测内部固定使用 `auto_approve`，因为工作区是临时副本；这**不代表**生产运行可以关闭审批。

## 快速运行

```bash
# 运行示例任务并打印 Markdown
bale eval --tasks examples/eval/tasks.json

# 同时保存 Markdown 与 JSON
bale eval --tasks examples/eval/tasks.json \
  --output reports/eval.md \
  --json reports/eval.json

# 官方 golden 套件（20 个任务，read/edit/test/recovery/security = 5/6/3/3/3）
bale eval --suite examples/eval/golden/suite.json

# 只做数据质量自检（preflight + 分类校验），不执行任务
bale eval --suite examples/eval/golden/suite.json --validate-only

# 结构回归：脚本化 fake provider + hermetic docker
bale eval --suite examples/eval/golden/suite.json --provider fake --sandbox docker

# 跑完冻结为不可变 baseline（同名不可覆盖，除非 --supersede-baseline）
bale eval --suite examples/eval/golden/suite.json --provider fake --sandbox docker \
  --freeze-baseline golden-v1

# 与 baseline 配对比较
bale eval --suite examples/eval/golden/suite.json \
  --compare-to docs/evaluation/baselines/golden-v1.json
```

`--suite` 与 `--tasks` 互斥。suite 运行前会做分类数量校验，并对声明了 `gold_patch` 的任务执行数据质量 preflight（`--skip-preflight` 可跳过）。

JSON 报告包含 `report_schema_version`、五元组指纹 `manifest`（model/prompt/tools/sandbox/dataset + git + flags）与 `metrics`（不可测指标为 `null` 并附 `coverage`）。常用参数：

```bash
# 重复 3 次，统计 pass@k
bale eval --suite examples/eval/golden/suite.json --repeat 3

# 持久化 manifest、报告与逐任务证据
bale eval --suite examples/eval/golden/suite.json --artifacts-dir artifacts/eval/run-1

# 与 baseline 配对比较；未声明维度不同时判为 incomparable 并退出 1
bale eval --suite examples/eval/golden/suite.json \
  --compare-to artifacts/eval/run-1/report.json
```

预期输出末尾：

```text
passed 2/2 (failed 0, errors 0)
```

退出码：全部通过为 `0`；存在 failed 或 error 为 `1`；任务文件无法加载时为参数错误。`--tasks` 也可以指向包含 `tasks.json` 的目录。

> FakeProvider 只会回放脚本化文本，无法真正改文件，因此用它跑示例任务通常全部失败。要看真实效果，先按 [快速启动 §3](../quickstart.md#3-配置-settings) 配置 DeepSeek/Ollama/OpenAI。

## 任务格式

任务文件是纯 JSON，顶层为 `{"tasks": [...]}`：

```json
{
  "tasks": [
    {
      "id": "greeting-message",
      "description": "Change the greeting text while keeping the signature.",
      "fixture": "fixtures/greeting",
      "message": "In greetings.py, update greet() so it returns a message starting with 'hello'.",
      "max_iterations": 6,
      "token_budget": 128000,
      "time_budget": 120,
      "checks": [
        {"type": "file_exists", "path": "greetings.py"},
        {"type": "file_contains", "path": "greetings.py", "value": "hello"},
        {"type": "file_not_contains", "path": "greetings.py", "value": "hi,"}
      ]
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 任务唯一标识，报告中展示 |
| `description` | 否 | 给人看的目标说明 |
| `fixture` | 是 | 相对任务文件所在目录解析；必须存在且为目录 |
| `message` | 是 | 传给 Agent 的用户消息 |
| `max_iterations` | 否 | 默认 8，必须 ≥1 |
| `token_budget` | 否 | 默认 128000 |
| `time_budget` | 否 | wall-clock 秒数，必须 >0；超时任务记为 `failed`，`error` 为 `time budget exceeded` |
| `checks` | 否 | 未通过任一检查即 `failed`；没有检查则只看运行状态 |

检查类型：

| `type` | `value` | 判定 |
|---|---|---|
| `file_exists` | 不允许 | 文件存在 |
| `file_not_exists` | 不允许 | 文件不存在 |
| `file_contains` | 必填 | 文件文本包含该子串 |
| `file_not_contains` | 必填 | 文件文本不包含该子串 |

注意：`file_contains` 是字面子串匹配，不做语法解析；`file_not_contains` 适合防止回归，但可能被注释或字符串绕过。需要验证行为时，优先检查更精确的文本或增加 fixture 内测试。

## 报告字段

| 字段 | 含义 |
|---|---|
| `status` | `passed` / `failed` / `error` |
| `run_status` | Turn 的实际状态（如 `completed`、`cancelled`） |
| `iterations` / `tool_calls` | 本次运行的迭代与工具调用次数 |
| `total_tokens` | 累计 token（来自 Provider 的 usage） |
| `duration_ms` | 单任务墙钟耗时 |
| `check_results` | 每条断言的结果与说明 |
| `error` | 失败/异常摘要；`failed` 也可能是「检查未通过」 |

`EvalReport` 汇总 `passed`、`failed`、`errors`、`total` 与逐任务结果；Markdown 由 `render_markdown` 生成。报告还携带：

- `manifest.hermetic`：是否为 hermetic 运行（docker + 禁网 + 干净 git 树），Markdown 头部渲染为 `Hermetic: yes/no`。
- `preflight`：每个声明 `gold_patch` 的任务的数据质量校验结果（pristine F2P/P2P、gold 全通过），Markdown 渲染为 `## Preflight` 表。
- `metrics`：含 `cost_per_success`、`regression_rate`、`repeated_call_rate` 等；无法计算的指标为 `null` 并在 `coverage` 注明原因。

## Hermetic 与可复现

- `--sandbox docker`（默认）在容器内以 `network=none` 运行，报告 `manifest.hermetic` 标记 `true`；这是发布官方结论的前提。
- `--sandbox local` 需显式 `--allow-local`，报告标记 `hermetic: false`，仅用于本地调试，不能冻结为官方 baseline。
- 官方 baseline 还要求 git 工作区干净（`manifest.git.dirty == false`），由 `--freeze-baseline` 强制校验。
- suite 运行前的 preflight 校验每个 `gold_patch` 可应用、pristine P2P/F2P 与 gold 全通过；失败则终止（`--skip-preflight` 可跳过，但不建议）。

## Baseline 冻结与配对比较

- `bale eval ... --freeze-baseline <name>` 把本次报告冻结到 `docs/evaluation/baselines/<name>.json`，同名不可覆盖。
- 需替换时用 `--supersede-baseline --baseline-reason "<原因>"`，旧报告归档为 `<name>.superseded.json` 并在 manifest 记录原因（design.md §10.2 规则 6）。
- `bale eval ... --compare-to <baseline.json>` 做配对比较：五元组指纹不一致判为 `incomparable` 并退出 1；一致则给出 verdict、newly resolved / newly failing 与分类 delta。

## CI 结构回归

每个 PR 跑 `structural-regression` job：用 `--provider fake --sandbox docker` 跑 golden suite 两次，断言基础设施错误率为 0、manifest hermetic、且两次结果确定。它验证 Runtime/工具治理/恢复/安全/eval 流水线的契约行为，而非模型通过率（fake provider 不会修代码）。

## 证据边界

发布任何评测结论时，至少标注：

- 日期、操作系统、Python 版本、Bale 提交。
- Provider、`BALE_OPENAI_BASE_URL`、模型名与是否本地。
- 任务文件路径与 fixture 版本（提交哈希）。
- 完整命令、`passed/failed/errors`、退出码。
- 已知限制：`--sandbox local` 无容器隔离（非 hermetic）、单模型样本小、`file_contains` 是字面匹配；hermetic 结论需用 `--sandbox docker` 并确保干净 git 树。

不要把单次运行写成「模型 X 在 Bale 上通过率 Y%」这类结论；样本量、温度与网络波动都未受控。

## 结果记录模板

新增实验报告时，复制下面的结构（可放在本目录，命名如 `YYYYMMDD-<topic>.md`）：

```markdown
# <实验名称>

## 问题
这次实验想验证什么？只写一个可被数据回答的问题。

## 环境
- 日期：
- 操作系统 / Python：
- Bale 提交：
- Provider / base_url / 模型：
- 任务文件与 fixture 提交：

## 方法
- 命令：
- 任务数量与范围：
- 重复次数与隔离方式：

## 结果
| 指标 | 数值 |
|---|---|
| passed / total | |
| failed / errors | |
| 平均耗时 | |
| 平均 iterations / tool_calls | |

## 结论
数据支持什么、不支持什么。

## 证据边界
未覆盖的场景、可能的偏差、不可外推的结论。
```

## 示例任务

- `examples/eval/fixtures/greeting`：修改问候语并保持函数签名。
- `examples/eval/fixtures/config`：关闭调试开关且不触碰无关配置。

新增任务时优先选择「修改明确、检查确定、无需网络」的小仓库场景，并同时提交 fixture。
