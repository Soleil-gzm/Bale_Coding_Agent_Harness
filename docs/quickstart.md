# Bale Coding Agent Harness 快速启动指南

本指南帮助你在本地安装并验证当前仓库中的 FastMCP 文件写入示例。完整 Agent Runtime 尚未包含在仓库中，因此不需要配置模型、数据库或容器服务。

## 1. 环境要求

| 依赖 | 要求 | 用途 |
|---|---|---|
| Python | 3.10 或更高版本 | 运行 MCP 服务端和客户端 |
| pip | 建议使用当前稳定版本 | 安装锁定依赖 |
| MCP 客户端 | 支持 `stdio` 的客户端，或使用本文 Python 脚本 | 初始化和调用工具 |
| 文件系统权限 | 对启动目录具有写权限 | 创建 `output/` 和文本文件 |

当前示例不需要 GPU、Docker、数据库或 API Key。

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
python -m pip install -r requirements.txt
python -m pip check
```

`requirements.txt` 将 MCP SDK 固定为 `mcp==1.30.0`。不要直接升级到不兼容的主版本后再沿用当前 FastMCP 示例。

## 3. 启动服务端

```bash
python examples/mcp/write_server.py
```

正常情况下，进程会持续等待标准输入中的 MCP 消息，不会显示交互式提示符，也不应通过浏览器访问。

直接启动适合检查导入和进程是否可用。实际调用工具时，建议让 MCP 客户端启动并管理服务进程，而不是在一个终端中手动输入协议消息。

## 4. 使用 Python 客户端验证

如果第 3 节的手动服务进程仍在运行，可以先停止它；以下脚本会启动一个独立的服务端子进程。激活同一虚拟环境，然后从项目根目录执行：

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

## 5. 配置常见 MCP 客户端

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

## 6. 基础使用约束

- 工具只接受 `content` 字符串，不接受目标文件路径。
- 文件统一命名为 `note_YYYYMMDD_HHMMSS.txt`。
- 同一秒内的多次写入可能使用相同文件名并覆盖内容。
- 文件操作异常会作为文本返回，调用方应检查返回内容和实际文件。
- 当前示例不具备工作区隔离或权限审批，只应在受控目录中运行。

## 7. 常见问题排查

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

这是 `stdio` 服务的正常行为。使用第 4 节客户端脚本验证，不要把该进程当作交互式 REPL。

### 工具调用成功但找不到文件

`OUTPUT_DIR` 是相对路径。文件位于**服务端进程工作目录**的 `output/` 下，不一定是客户端目录。为客户端配置显式 `cwd`，或在返回文本中查看相对路径。

### 无法创建 `output/` 或写入文件

检查启动目录权限、磁盘空间和只读文件系统。建议选择普通可写目录启动服务，不要通过提升系统权限绕过路径管理。

### 文件内容被覆盖

当前文件名只有秒级精度。避免同秒并发写入；如需可靠文件标识，应将命名改为 UUID 等唯一值并同步更新测试与文档。

### 协议连接中断或出现普通文本解析错误

确保服务代码没有向标准输出打印日志。`stdio` 必须保留给 MCP 消息；诊断信息应使用标准错误或结构化日志方案。

## 8. 修改后的验证

语法检查：

```bash
python -m compileall examples
python -m pip check
```

行为验证：重新执行第 4 节脚本，确认：

- `write_file` 出现在工具列表中；
- 工具调用无异常；
- 返回文本包含生成路径；
- 文件内容与传入字符串完全一致；
- 客户端退出后没有残留服务进程。

当前仓库没有独立自动化测试目录。新增 Runtime 或复杂工具逻辑时，应同时建立测试目录和可重复执行的测试命令。

## 9. 下一步

- 了解当前实现与目标模块的边界：[架构设计](architecture.md)。
- 返回项目功能、结构和贡献说明：[README](../README.md)。
