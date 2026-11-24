# Bale Coding Agent Harness

Bale Coding Agent Harness 是一个面向 Coding Agent 运行时的项目，目标是将模型交互、工具治理、上下文管理、隔离执行、状态恢复和执行证据组织为可扩展的薄内核。

**当前仓库处于早期建设阶段。** 已落地 B0 工程基础：可安装 Python 包、核心数据契约、配置校验、离线 FakeProvider，以及只创建 Turn 的 CLI/HTTP 入口。仓库同时保留一个可独立运行的 MCP（Model Context Protocol）`stdio` 文件写入示例。Turn 目前不会执行；真实 OpenAIProvider、Agent Loop、ToolRegistry、Sandbox、Session 等运行时能力仍未实现。

## 当前功能

### 核心契约与入口

- `Turn` 和 `ToolResult` 使用 Pydantic 校验，支持严格字段检查和 JSON 往返。
- `Settings` 读取 `BALE_` 环境变量；`openai` 模式缺少有效 Key 时立即报错，密钥不会在 `repr` 中明文显示。
- `ModelProvider` 定义最小异步契约，`FakeProvider` 提供确定性离线响应并记录收到的 Turn。
- `bale turn` 与 `POST /v1/turns` 复用同一 `build_turn()` 构造逻辑，返回字段一致的 `created` Turn。
- CLI 和 HTTP 只创建 Turn，不调用模型或工具。

### MCP 文件写入示例

- 使用 `FastMCP` 创建名为 `WriteServer` 的 MCP 服务，通过标准输入输出通信。
- 注册异步 `write_file(content: str)` 工具，将内容以 UTF-8 写入 `output/note_<时间戳>.txt`。
- 返回写入结果或错误文本，便于验证 MCP list/call 流程。

当前功能不需要模型 API、数据库、Docker 或 GPU。

## 功能边界

当前实现提供契约与 Turn 构造入口，不是完整 Coding Agent：

- 不包含 Agent Loop、ReAct、真实模型调用或自动代码修改。
- 真实 OpenAIProvider 尚未实现；即使配置了有效 Key，`openai` 模式也会明确报告未实现，不会退回 Fake。
- `ToolResult` 目前只是数据契约，不包含 ToolRegistry、工具执行、权限审批或超时治理。
- 不包含 Docker Sandbox、工作区隔离或不可信代码执行能力。
- 不包含 Session、Checkpoint、Trace、评测、Turn 执行接口、SSE 或持久化。
- MCP 示例的文件名精度为秒，同一秒内的重复调用可能覆盖同名文件。
- MCP 输出目录相对于进程当前工作目录，不应将其误认为安全沙箱。

完整能力的分层设计与扩展方向见[架构设计](docs/architecture.md)。

## 技术栈

| 组成 | 当前选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 本地已在 Python 3.11.18 验证 |
| 数据与配置 | Pydantic v2、pydantic-settings | 严格契约、环境变量与 `.env` |
| CLI / HTTP | Typer、FastAPI、Uvicorn | 共享 Turn 构造逻辑 |
| Provider | Protocol、FakeProvider | 最小异步接口与离线测试实现 |
| 工具协议 | Model Context Protocol | MCP 示例使用 `stdio` transport |
| SDK | `mcp==1.30.0` | 使用 `mcp.server.fastmcp.FastMCP` |
| 测试与检查 | pytest、Ruff、pip-tools | 离线测试、静态检查与哈希锁文件 |
| 存储 | 本地文件系统 | 仅 MCP 示例写入 `output/`，Turn 不持久化 |

## 项目结构

```text
Bale_Coding_Agent_Harness/
├── src/bale_harness/
│   ├── contracts.py          # Turn、ToolResult 与状态类型
│   ├── settings.py           # BALE_ 配置与密钥校验
│   ├── providers.py          # ModelProvider、FakeProvider 与工厂
│   ├── turns.py              # TurnRequest 与共享 build_turn()
│   ├── cli.py                # bale turn
│   └── http.py               # POST /v1/turns 应用工厂
├── tests/                    # 契约、配置、Provider 与入口测试
├── examples/mcp/
│   └── write_server.py       # 独立 FastMCP 服务端示例
├── docs/
│   ├── architecture.md       # 当前实现与目标运行时架构
│   └── quickstart.md         # 安装、运行、验证与故障排查
├── .github/workflows/ci.yml  # Python 3.11 基础检查
├── .env.example              # 无密钥配置样例
├── pyproject.toml            # 包元数据与工具配置
├── requirements.lock         # 带哈希的完整依赖锁文件
└── requirements.txt          # 原 MCP 示例的固定依赖
```

`output/` 在 MCP 工具成功调用后生成，已由 `.gitignore` 排除，不属于源码。

## 快速开始

推荐使用虚拟环境安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps .
```

创建一个 Turn，默认使用离线 Fake 配置：

```bash
bale turn --message "Inspect the repository" --workspace "$PWD" --token-budget 4096
```

启动只提供 Turn 创建接口的 HTTP 应用：

```bash
uvicorn bale_harness.http:create_app --factory
```

从项目根目录启动原 MCP 服务：

```bash
python examples/mcp/write_server.py
```

MCP 命令通过 `stdio` 等待客户端请求，不是交互式命令行程序。HTTP 请求示例、完整客户端验证脚本、配置说明和常见问题见[快速启动指南](docs/quickstart.md)。

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
3. 新功能应包含必要注释、使用说明和可复现验证；新增运行时模块时同步补充自动化测试。
4. 提交前执行 `ruff check src tests`、`ruff format --check src tests`、`pytest -q` 和 `python -m compileall examples`。
5. 修改 MCP 示例后，按快速启动指南验证 `list_tools` 和 `call_tool`。
6. 不提交 `.venv/`、`output/`、日志、密钥或本地客户端配置。
7. 修改公开契约、工具签名、目录结构或运行方式时，同步更新 README 和相关文档。
8. 提交 Pull Request 时说明修改动机、行为变化、验证命令和已知限制。

## 许可证

当前仓库尚未指定开源许可证，也未包含 `LICENSE` 文件。在维护者明确选择许可证之前，本仓库不授予额外的复制、修改或再分发许可。添加许可证后，以根目录 `LICENSE` 文件为准。
