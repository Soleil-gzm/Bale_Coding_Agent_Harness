# Bale Coding Agent Harness 快速启动指南

本指南帮助你在本地安装并验证 B0 核心契约、CLI/HTTP Turn 创建入口、B1 只读 ReAct 运行时、B2a 写入运行时（`write_file`/`edit_file`/`shell`/`run_tests`/`git_diff`/`finish`、审批与 ShadowGit）和 FastMCP 文件写入示例。真实 OpenAIProvider 与 Docker 容器隔离尚未实现；B2a 的本地执行只适用于受控开发场景。默认 Fake 配置不需要模型、数据库或容器服务。

## 1. 环境要求

| 依赖 | 要求 | 用途 |
|---|---|---|
| Python | 3.11 或更高版本，本地已验证 3.11.18 | 运行包、CLI/HTTP 和 MCP 示例 |
| pip | 建议使用当前稳定版本 | 安装锁定依赖 |
| MCP 客户端 | 支持 `stdio` 的客户端，或使用本文 Python 脚本 | 初始化和调用工具 |
| 文件系统权限 | 对启动目录具有写权限 | 创建 `output/` 和文本文件 |

默认 Fake 配置和 MCP 示例不需要 GPU、Docker、数据库或 API Key。选择 `openai` 时必须配置 Key，但真实 Provider 目前仍会明确报告未实现。

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
| `BALE_OPENAI_API_KEY` | `openai` 模式必填，空白值视为缺失，使用 SecretStr 保护 |

`openai` 模式缺少有效 Key 时会在初始化阶段失败。即使 Key 有效，Provider 工厂也会报告真实 OpenAIProvider 尚未实现，不会静默退回 Fake。`.env` 已被 Git 忽略，不要提交真实密钥或打印完整 Settings。

## 4. 使用 CLI 创建 Turn

```bash
bale turn \
  --message "Inspect the repository" \
  --workspace "$PWD" \
  --token-budget 4096
```

命令向标准输出写入一个 Turn JSON。默认生成随机 `turn_id` 和 `session_id`，状态为 `created`，`max_iterations` 为 20，`approval_mode` 为 `approve_write`。可通过 `--repo-revision`、`--max-iterations`、`--cost-budget` 和 `--approval-mode` 覆盖相应输入。

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
  "checkpoints": []
}
```

退出码：`completed` 为 0，`failed` 或 `cancelled` 为 1。可用 `--max-iterations` 限制循环次数，`--token-budget` 设置预算。所有路径都受 `WorkspaceGuard` 防护，无法逃逸工作区。

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
- `shell` 不接受管道、重定向或串联等 shell 元字符，推荐直接传参数数组。`run_tests` 使用当前 Python 解释器执行 pytest。
- `LocalSandbox` 只是受控开发环境的防护层，提供命令 deny-list、环境变量白名单、cwd 限定、超时和输出截断，**不是**不可信代码的安全边界；容器隔离属于后续里程碑。

## 5. 使用 HTTP 创建 Turn

启动应用工厂：

```bash
uvicorn bale_harness.http:create_app --factory --host 127.0.0.1 --port 8000
```

在另一个终端发送请求，将 workspace 替换为实际路径：

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

当前业务接口只有 `POST /v1/turns`，没有 Turn 执行、Session、SSE、健康检查或持久化接口。

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

测试覆盖契约、配置、FakeProvider、CLI/HTTP 同构、HTTP 输入校验、B1 只读运行时（消息契约、路径防护、工具注册器、只读工具、ReAct 循环、CLI run）、B2a 写入运行时（审批策略、Sandbox 防护、ShadowGit、写入/编辑、执行工具、幂等与端到端改码闭环）和 MCP 示例语法，不需要网络或 API Key。部分测试会在临时目录启动真实 pytest 子进程。仓库已配置 GitHub Actions，但远程结果应以 Actions 页面为准。

修改 MCP 示例后，重新执行第 7 节脚本，确认：

- `write_file` 出现在工具列表中；
- 工具调用无异常；
- 返回文本包含生成路径；
- 文件内容与传入字符串完全一致；
- 客户端退出后没有残留服务进程。

## 12. 下一步

- 了解当前实现与目标模块的边界：[架构设计](architecture.md)。
- 返回项目功能、结构和贡献说明：[README](../README.md)。
