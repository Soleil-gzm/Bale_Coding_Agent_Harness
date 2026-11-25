# NOTE: Turn 混合了输入参数与运行状态（含 status）。
# 已知取舍：简单优先，单对象贯通创建/运行/持久化全流程。
# 触发条件（并发读写、审计重放、API 隔离）出现时，
# 拆分为 frozen 的 TurnRequest + 可变的 TurnState，转移收敛到 transition()。

from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

ApprovalMode = Literal["auto_read", "approve_write", "approve_all"]
TurnStatus = Literal[
    "created",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]
ToolStatus = Literal["success", "error", "timeout", "denied", "cancelled"]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")       # 禁止传入未定义字段，拼写错误直接报错

    turn_id: UUID = Field(default_factory=uuid4)        # 工厂函数：每个实例生成新随机ID，而不是所有实例共享一个固定值
    session_id: UUID = Field(default_factory=uuid4)
    message: str = Field(min_length=1)      # 非空
    workspace: Path     # 必填，沙箱根目录
    repo_revision: str | None = None
    max_iterations: int = Field(default=20, ge=1)       # ReAct循环最大轮数下限保护
    token_budget: int = Field(gt=0)                     # Token 和成本预算，gt=0 / ge=0
    cost_budget: float | None = Field(default=None, ge=0)
    approval_mode: ApprovalMode = "approve_write"
    status: TurnStatus = "created"                      # 自定义校验：strip 后不能是空白

    @field_validator("message")     # 装饰器1：注册
    @classmethod                    # 装饰器2：方法类型
    def validate_message(cls, value: str) -> str:
        message = value.strip()     # 去掉首尾空白
        if not message:             # strip 后为空（如 "   " 或 ""）
            raise ValueError("message must not be blank")
        return message              # 返回处理后的值，会写回字段


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str            # 对应 LLM 返回的 tool_call id（多工具并发时靠它配对）
    name: str               # 工具名
    status: ToolStatus      # 五种结局
    content: str            # 给 LLM 看的结果文本
    error_code: str | None = None       # 机器可读错误码，与 content（人可读）分离
    truncated: bool = False             # 结果是否被截断（上下文管理的关键信号）
    latency_ms: int = Field(ge=0)       # 耗时，可观测性
    metadata: dict[str, Any] = Field(default_factory=dict)      # 扩展字段，default_factory=dict 避免可变默认值共享
