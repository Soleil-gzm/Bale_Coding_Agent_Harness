# 评测指南

本目录记录 Bale 的固定任务评测方式与结果记录规范。它是一个**确定性回归 harness**：用固定 fixture、固定消息和文件级断言检查 Agent 是否真的完成了修改，而不是模型排行榜。

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 把 fixture 复制到临时工作区，隔离原始文件 | 默认在本机执行任务命令，除非显式传 `--sandbox docker` |
| 运行一个真实 Turn（含工具调用与审批） | 不自动生成任务、不做 LLM-as-judge |
| 用 `file_exists` / `file_contains` 等确定性断言判定 | 不比较模型之间的绝对能力，也不承诺跨环境可复现 |
| 输出 Markdown/JSON 报告与退出码 | 不持久化任务结果或历史趋势 |

评测内部固定使用 `auto_approve`，因为工作区是临时副本；这**不代表**生产运行可以关闭审批。

## 快速运行

```bash
# 运行示例任务并打印 Markdown
bale eval --tasks examples/eval/tasks.json

# 同时保存 Markdown 与 JSON
bale eval --tasks examples/eval/tasks.json \
  --output reports/eval.md \
  --json reports/eval.json
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

`EvalReport` 汇总 `passed`、`failed`、`errors`、`total` 与逐任务结果；Markdown 由 `render_markdown` 生成。

## 证据边界

发布任何评测结论时，至少标注：

- 日期、操作系统、Python 版本、Bale 提交。
- Provider、`BALE_OPENAI_BASE_URL`、模型名与是否本地。
- 任务文件路径与 fixture 版本（提交哈希）。
- 完整命令、`passed/failed/errors`、退出码。
- 已知限制：无容器隔离、无双跑方差统计、单模型样本小、`file_contains` 是字面匹配。

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
