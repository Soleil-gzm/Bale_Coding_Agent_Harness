# Bale Coding Agent Harness

Bale Coding Agent Harness 是一个面向 Coding Agent 运行时的项目，目标是将模型交互、工具治理、上下文管理、隔离执行、状态恢复和执行证据组织为可扩展的薄内核。

**当前仓库处于早期建设阶段。** 现有代码提供一个可独立运行的 MCP（Model Context Protocol）`stdio` 服务端示例，展示如何通过官方 MCP Python SDK 暴露异步文件写入工具。Agent Loop、模型 Provider、Sandbox、Session 等完整运行时能力尚在设计实施过程中，不能作为现有功能使用。

## 当前功能

- 使用 `FastMCP` 创建名为 `WriteServer` 的 MCP 服务。
- 通过标准输入输出与 MCP 客户端通信。
- 注册异步 `write_file(content: str)` 工具。
- 将内容以 UTF-8 写入 `output/note_<时间戳>.txt`。
- 返回写入结果或错误文本，便于验证 MCP list/call 流程。
- 不需要模型 API、数据库、Docker 或 GPU。

## 功能边界

当前实现是 MCP 集成示例，不是完整 Coding Agent，也不是通用文件写入服务：

- 不包含 Agent Loop、ReAct、模型调用或自动代码修改。
- 不包含 ToolRegistry、权限审批、超时治理或统一 `ToolResult`。
- 不包含 Docker Sandbox、工作区隔离或不可信代码执行能力。
- 不包含 Session、Checkpoint、Trace、评测或 HTTP API。
- 文件名精度为秒，同一秒内的重复调用可能覆盖同名文件。
- 输出目录相对于进程当前工作目录，不应将其误认为安全沙箱。

完整能力的分层设计与扩展方向见[架构设计](docs/architecture.md)。

## 技术栈

| 组成 | 当前选型 | 说明 |
|---|---|---|
| 语言 | Python 3.10+ | MCP SDK 1.30.0 的最低解释器要求 |
| 异步模型 | `asyncio` | 工具函数采用 `async def` |
| 工具协议 | Model Context Protocol | 当前使用 `stdio` transport |
| SDK | `mcp==1.30.0` | 使用 `mcp.server.fastmcp.FastMCP` |
| 存储 | 本地文件系统 | 仅写入进程工作目录下的 `output/` |

## 项目结构

```text
Bale_Coding_Agent_Harness/
├── examples/
│   └── mcp/
│       └── write_server.py   # 当前可运行的 FastMCP 服务端
├── docs/
│   ├── architecture.md       # 当前实现与目标运行时架构
│   └── quickstart.md         # 安装、运行、验证与故障排查
├── .gitignore
├── requirements.txt
└── README.md
```

`output/` 在工具成功调用后生成，已由 `.gitignore` 排除，不属于源码。

## 快速开始

推荐使用虚拟环境安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

从项目根目录启动 MCP 服务：

```bash
python examples/mcp/write_server.py
```

该命令通过 `stdio` 等待 MCP 客户端请求，不是交互式命令行程序。完整的客户端验证脚本、MCP 配置示例和常见问题见[快速启动指南](docs/quickstart.md)。

## 工具概览

### `write_file`

| 项目 | 内容 |
|---|---|
| 输入 | `content: str`，要写入文件的完整文本 |
| 输出 | 包含相对文件路径的成功文本，或包含错误信息的失败文本 |
| 写入位置 | `<进程工作目录>/output/note_YYYYMMDD_HHMMSS.txt` |
| 编码 | UTF-8 |
| 副作用 | 创建输出目录并写入或覆盖同名文件 |

调用方应根据返回文本检查执行结果，并妥善控制 MCP 服务的启动目录和文件系统权限。

## 开发与贡献

欢迎围绕示例可靠性、协议兼容性和后续运行时模块参与开发。

1. 提交 Issue 前先说明问题现象、复现步骤、Python/MCP SDK 版本和预期行为。
2. 从最新主分支创建主题分支，保持每次修改聚焦一个目标。
3. 新功能应包含必要的中文注释、使用说明和可复现验证；新增运行时模块时同步补充自动化测试。
4. 修改 MCP 示例后，至少执行 `python -m compileall examples`，并按快速启动指南验证 `list_tools` 和 `call_tool`。
5. 不提交 `.venv/`、`output/`、日志、密钥或本地客户端配置。
6. 修改公开工具签名、目录结构或运行方式时，同步更新 README 和相关文档。
7. 提交 Pull Request 时说明修改动机、行为变化、验证命令和已知限制。

## 许可证

当前仓库尚未指定开源许可证，也未包含 `LICENSE` 文件。在维护者明确选择许可证之前，本仓库不授予额外的复制、修改或再分发许可。添加许可证后，以根目录 `LICENSE` 文件为准。
