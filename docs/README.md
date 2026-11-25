# Bale 文档索引

Bale Coding Agent Harness 是一个本地优先的 Coding Agent 运行时与工程脚手架：CLI/HTTP 入口、ReAct 循环、工具治理、本地/Docker 执行沙箱、Session/恢复、ShadowGit 快照、Trace/成本、BM25 检索、评测 harness 与 stdio MCP 适配。

文档按「先跑起来 → 理解边界 → 排错与评测」组织。命令默认在仓库根目录执行，Python 为 3.11+。

## 按目标选择阅读路径

| 目标 | 先读 | 再读 |
|---|---|---|
| 5 分钟离线跑通 | [快速启动 §1–4](quickstart.md) | [根 README](../README.md#快速开始) |
| 接入真实模型（DeepSeek/Ollama） | [快速启动 §3、§4.1](quickstart.md) | [配置参考](configuration.md) |
| 调整环境变量与运行参数 | [配置参考](configuration.md) | [快速启动 §3](quickstart.md) |
| 接入 HTTP / SSE | [快速启动 §5](quickstart.md) | [安全模型 · HTTP](security.md#http-入口) |
| 用 Docker 部署 HTTP 服务 | [Docker 部署](deployment.md) | [配置参考](configuration.md) |
| 隔离 shell / run_tests | [配置参考 §6](configuration.md) | [安全模型 · DockerSandbox](security.md#本地执行) |
| 接入 MCP 工具 | [快速启动 §4.11、§6–8](quickstart.md) | [架构 §3.3–3.5](architecture.md) |
| 理解运行时设计 | [架构设计](architecture.md) | [安全模型](security.md) |
| 排查失败 | [问题排查](troubleshooting.md) | [快速启动 §10](quickstart.md) |
| 跑评测 / 记录结果 | [评测指南](evaluation/README.md) | [examples/eval/tasks.json](../examples/eval/tasks.json) |

## 文档清单

| 文档 | 内容 | 主要读者 |
|---|---|---|
| [README](../README.md) | 能力概览、功能边界、项目结构、常用命令 | 所有人 |
| [quickstart.md](quickstart.md) | 安装、配置、CLI/HTTP/MCP 的可复制命令与预期输出 | 首次使用者、接入方 |
| [configuration.md](configuration.md) | 配置优先级、环境变量总表、Provider/HTTP/MCP/检索/执行沙箱开关与校验失败 | 部署者、接入方 |
| [deployment.md](deployment.md) | 镜像构建、Compose 运行、卷、认证、MCP 挂载与容器加固 | 部署者、运维 |
| [architecture.md](architecture.md) | 当前实现分层、模块职责、核心契约、数据流、测试与演进约束 | 贡献者、评审者 |
| [troubleshooting.md](troubleshooting.md) | 按现象组织的安装/运行/HTTP/MCP/检索/评测排错 | 使用者、运维 |
| [security.md](security.md) | 信任模型、工作区/审批/执行/MCP/HTTP 边界、密钥与提示注入 | 安全评审、部署者 |
| [evaluation/README.md](evaluation/README.md) | 评测 harness 的用法、任务格式、报告字段与证据边界 | 评测者、面试展示 |

## 写作与维护约定

- **状态必须显式**：区分「已实现」「部分实现」「未实现」；目标设计不能写成现状。
- **命令可复制**：给出前置条件、命令、预期输出或退出码；失败路径写入 [troubleshooting.md](troubleshooting.md)。
- **结论带环境**：涉及模型或网络的数字必须标注 Provider、模型、`base_url`、任务版本与日期，不把单次运行当作基准。
- **同步更新**：修改 CLI 参数、HTTP 路由、环境变量、工具签名或目录结构时，同时更新 README、quickstart、architecture 与本索引。
- **安全默认**：示例不包含密钥；新增副作用工具必须说明审批与回滚方式。

## 相关入口

- 环境变量总表与优先级：[configuration.md](configuration.md)、[quickstart.md §3](quickstart.md)
- HTTP 路由与认证：[quickstart.md §5](quickstart.md)
- 容器构建与运行：[Dockerfile](../Dockerfile)、[compose.yaml](../compose.yaml)、[deployment.md](deployment.md)
- MCP 配置示例：[examples/mcp/servers.json](../examples/mcp/servers.json)
- 评测任务示例：[examples/eval/tasks.json](../examples/eval/tasks.json)
- 开发与提交检查：[README · 开发与贡献](../README.md#开发与贡献)
