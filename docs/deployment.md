# Docker 部署指南

本页说明如何把 Bale 的 HTTP 服务构建为容器镜像并运行。容器部署解决的是「进程与依赖打包」，**不是** B04 计划中的 Docker 沙箱适配器：Agent 的 `shell`/`run_tests` 仍然通过 `LocalSandbox` 在容器内以同一用户执行。

## 1. 适用范围与边界

适合：

- 在本机或内网提供固定的 `/v1` HTTP 入口；
- 把 Python、git、依赖和运行目录封装进同一镜像；
- 用只读挂载、非 root 用户和资源限制降低对宿主的影响。

不适合：

- 运行不受信任的代码。当前镜像没有为每个工具调用启动独立容器，也没有 seccomp/AppArmor 自定义策略；
- 多租户隔离。HTTP 没有用户体系、限流或 Session 持久化，`workspace` 由调用方指定；
- 直接暴露到公网。镜像默认只绑定 `127.0.0.1`，且不提供 TLS。

## 2. 前置要求

- Docker Desktop 已启动，`docker info` 能看到 `Server` 段；
- Docker Compose v2（`docker compose version`）；
- 至少给 WSL/Docker 预留 2 GB 内存与 2 GB 磁盘；
- 仓库根目录已存在 `pyproject.toml`、`src/` 和本页所在的 `docs/`。

## 3. 快速启动

```powershell
cd Bale_Coding_Agent_Harness
New-Item -ItemType Directory -Force workspace | Out-Null

# 国内网络建议指定 PyPI 与 Debian 镜像，能直连时可省略
$env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
$env:DEBIAN_MIRROR = "mirrors.tuna.tsinghua.edu.cn"
docker compose build

docker compose up -d
docker compose ps
```

验证：

```powershell
curl.exe http://127.0.0.1:8000/health
```

预期：

```json
{"status":"ok"}
```

仓库根目录没有 `.env` 时默认 `BALE_PROVIDER=fake`，不调用模型，适合先验证容器链路。若已有 `.env`，Compose 会自动读取其中的 `BALE_*` 变量；把宿主机专用配置（例如 `127.0.0.1` 的 Ollama 地址）直接带进容器会导致连接失败，见下节。

## 4. 配置

Compose 默认值：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BALE_PROVIDER` | `fake` | `fake` 或 `openai` |
| `BALE_OPENAI_API_KEY` | 空 | 远程端点必填；本地回环端点可省略 |
| `BALE_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容端点 |
| `BALE_OPENAI_MODEL` | `gpt-4o-mini` | 模型名 |
| `BALE_RETRIEVAL_ENABLED` | `true` | 自动注入 BM25 检索片段 |
| `BALE_RETRIEVAL_TOP_K` | `5` | 注入片段上限 |
| `BALE_TRACE_PATH` | `/data/trace.jsonl` | 容器内 Trace 路径 |
| `BALE_BIND` | `127.0.0.1` | 宿主机监听地址 |
| `BALE_PORT` | `8000` | 宿主机端口 |
| `BALE_WORKSPACE` | `./workspace` | 挂载到 `/workspace` 的宿主目录 |

其余变量（`BALE_HTTP_API_KEY`、`BALE_MCP_CONFIG`、成本费率等）通过仓库根目录的 `.env` 注入：

```dotenv
BALE_PROVIDER=openai
BALE_OPENAI_API_KEY=sk-...
BALE_OPENAI_BASE_URL=https://api.deepseek.com/v1
BALE_OPENAI_MODEL=deepseek-chat
BALE_HTTP_API_KEY=change-me
```

然后重建容器使环境生效：

```powershell
docker compose up -d --force-recreate
```

注意：

- `BALE_HTTP_API_KEY` 必须是非空值；设置为空字符串会要求空 Bearer，等同于配置错误。
- `.env` 中的相对路径按**容器内**解析。`BALE_MCP_CONFIG=examples/mcp/servers.json` 在容器里不存在，必须挂载后改成 `/config/...`。
- 使用宿主机 Ollama 时，`BALE_OPENAI_BASE_URL` 要写 `http://host.docker.internal:11434/v1`，不能写 `127.0.0.1`，因为容器内的回环是容器自己。

完整变量与校验规则见[配置参考](configuration.md)。

## 5. 调用 HTTP 接口

### 5.1 执行一次 Turn

```powershell
$body = @{
  message = "List the top-level files"
  workspace = "/workspace"
  max_iterations = 3
  approve_writes = $false
} | ConvertTo-Json

Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/runs `
  -Method Post -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 5
```

`workspace` 必须是容器内已存在的目录，通常就是 `/workspace`。容器内路径不能写成 `E:\...`。

### 5.2 开启 Bearer 认证

```powershell
$headers = @{ Authorization = "Bearer change-me" }
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/runs `
  -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

`/health` 始终公开；`/v1` 在配置 Key 后全部要求 Bearer。

### 5.3 SSE 与取消

先把请求体保存为 `request.json`，或在命令中使用等价的内联 JSON：

```json
{"message":"Inspect the repository","workspace":"/workspace","run_id":"inspect-1"}
```

```powershell
curl.exe -N -X POST http://127.0.0.1:8000/v1/runs/stream `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer change-me" `
  --data-binary "@request.json"
```

请求体需要包含 `run_id` 时，可在另一个终端取消：

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/runs/<run_id>/cancel `
  -H "Authorization: Bearer change-me"
```

## 6. 在容器内使用 CLI

镜像同时安装了 `bale` 命令，Compose 已挂载工作区与数据卷：

```powershell
docker compose run --rm bale bale --help

docker compose run --rm bale bale run `
  --workspace /workspace `
  --message "Inspect the repository" `
  --yes `
  --session-db /data/sessions.db `
  --trace /data/trace.jsonl
```

说明：

- `docker compose run` 默认不在后台运行，适合一次性任务；
- `--yes` 自动批准写与执行；不加时，非交互环境会拒绝写操作；
- Session 数据库建议放在 `/data`，否则容器删除后丢失；
- 运行时镜像不安装 pytest、ruff 等开发依赖，`run_tests` 只能测试工作区里已安装的测试框架。

## 7. 目录、卷与持久化

| 容器路径 | 来源 | 内容 |
|---|---|---|
| `/workspace` | 宿主 `./workspace`（可用 `BALE_WORKSPACE` 覆盖） | Agent 读写、检索与执行的工作区 |
| `/data` | 命名卷 `bale-harness_bale-data` | HTTP 的 Trace；CLI 的 Session/Checkpoint |
| `/app` | 镜像内 | 只读代码与依赖，非 root 用户不可写 |
| `/config` | 默认未挂载 | 可选的 MCP 配置目录 |

查看卷位置：

```powershell
docker volume inspect bale-harness_bale-data
```

`docker compose down` 保留卷；`docker compose down -v` 会删除卷和 Trace/Session 数据。

## 8. 挂载 MCP 服务器

在 `compose.yaml` 中追加：

```yaml
    volumes:
      - ./examples/mcp:/config
    environment:
      BALE_MCP_CONFIG: /config/servers.json
```

- `servers.json` 中的相对 `cwd` 以**配置文件所在目录**为基准，因此 `write_server.py` 需要与它一起挂载。
- 示例服务器把输出写到 `./output`，该目录必须在挂载内可写；只读挂载会导致工具失败。
- `read_only` 只是调用方声明，不构成隔离；写入工具仍需要审批或 `approve_writes=true`。

## 9. 安全默认与加固

镜像与 Compose 已经做的：

- 非 root 用户 `bale`（UID 10001）；
- `no-new-privileges`；
- `cap_drop: ALL`；
- 默认只绑定 `127.0.0.1`；
- 不挂载 Docker socket；
- 工作区之外的应用目录对运行用户只读。

仍需注意：

- `LocalSandbox` 的命令允许列表与工作区限制在容器内继续生效，但它**不是**针对恶意代码的安全边界；
- 容器内可以读取 `/app`、`/etc` 等非工作区文件，只是不能写入镜像层；
- 没有 CPU/内存/磁盘配额；需要时在 Compose 中加 `mem_limit`、`cpus`，或在 `docker run` 时加 `--memory`、`--cpus`；
- 没有 TLS。对外提供服务时应放在反向代理或内网网关之后；
- 不要把宿主目录以可写方式挂载到 `/` 或用户主目录。

如需更严格的隔离，应实现 B04 的独立执行容器，而不是复用本部署容器。

## 10. 运维命令

```powershell
docker compose logs -f bale          # 查看日志
docker compose restart bale          # 重启服务
docker compose ps                    # 查看状态与健康检查
docker compose down                  # 停止并保留数据卷
docker compose build --pull          # 更新基础镜像并重建
docker compose up -d --force-recreate
docker image prune                   # 清理悬空镜像
```

HTTP 的 Trace 在 `/data/trace.jsonl`，可直接查看：

```powershell
docker compose exec bale tail -n 5 /data/trace.jsonl
```

## 11. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `BALE_OPENAI_API_KEY is required...` | `provider=openai` 且远程端点没有有效 Key | 在 `.env` 填写 Key；Ollama 使用 `host.docker.internal` |
| `/v1` 返回 401 | Key 为空、拼写错误或缺少 Bearer | 检查 `BALE_HTTP_API_KEY` 与请求头；`/health` 不需认证 |
| 422 `workspace must be an existing directory` | 传了宿主路径或未挂载 | 使用 `/workspace`，确认宿主目录存在 |
| `git executable not found on PATH` | 使用了自定义精简镜像 | 安装 git，或使用仓库自带 Dockerfile |
| 写工作区报 Permission denied（Linux） | 绑定目录属主不是 UID 10001 | `sudo chown -R 10001:10001 ./workspace` |
| 构建卡在 apt/pip | 网络无法访问 Debian/PyPI | 配置 Docker 代理，或设置 `DEBIAN_MIRROR`、`PIP_INDEX_URL` 后重建 |
| 端口被占用 | 宿主机 8000 已使用 | 设置 `BALE_PORT=18000` 后重启 |
| 镜像拉取超时 | 无法访问 Docker Hub | 配置镜像加速器或代理，参见[问题排查](troubleshooting.md) |

## 12. 与发布流程的衔接

```powershell
docker build `
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple `
  --build-arg DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn `
  -t registry.example.com/bale-harness:0.1.0 .

docker push registry.example.com/bale-harness:0.1.0
```

推送前确认：

- `.env` 未被打进镜像（`.dockerignore` 已排除）；
- 镜像内没有 `tests/`、`docs/`、本地 Trace 或数据库；
- 部署环境通过环境变量或 Secret 注入 Key，而不是写进镜像；
- 在目标主机执行一次 `GET /health` 与 `POST /v1/runs` 冒烟。

镜像与 Compose 变更后，应同步更新本页、[配置参考](configuration.md)、[快速启动 §5](quickstart.md)与 [README](../README.md)。
