"""PR-151 密钥托管抽象 —— provider 接口与脱敏值对象。

安全契约（本批硬约束，测试锁定）：
1. **缺失返回 None**：provider 不得抛、不得造值、不得用占位符冒充真实密钥。
2. **真值只在 reveal() 里**：SecretValue 的 __repr__ / __str__ / as_log_dict()
   只暴露「长度 + sha256 前 12 位指纹」，落日志/落盘一律走后者。
3. **双读槽位**：next 槽用于轮换窗口期的新值（env 后端即 <VAR>_NEXT）；
   不支持的后端对 next 返回 None，调用方不得据此假装轮换已完成。

真 Vault/KMS 后端本批**不实现**，见 docs/SECURITY-SECRETS.md §未覆盖范围。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass

# 槽位：current = 当前生效值；next = 双读窗口期的新值（轮换用）
SLOT_CURRENT = "current"
SLOT_NEXT = "next"
SLOTS = (SLOT_CURRENT, SLOT_NEXT)

_FINGERPRINT_LEN = 12


def fingerprint(value: str) -> str:
    """密钥指纹：sha256 前 12 位。用于「不存值也能比对是否真的换了」。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


@dataclass(frozen=True, repr=False)
class SecretValue:
    """一次取到的密钥值。真值只能经 reveal() 取用，repr/日志全部脱敏。"""

    name: str
    value: str
    backend: str
    slot: str = SLOT_CURRENT

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.value)

    @property
    def length(self) -> int:
        return len(self.value)

    def reveal(self) -> str:
        """显式取真值。调用点是唯一可审计的「密钥离开托管层」位置。"""
        return self.value

    def __repr__(self) -> str:  # 脱敏：f-string / 日志 / 异常回溯都看不到真值
        return (
            f"SecretValue(name={self.name!r}, backend={self.backend!r}, slot={self.slot!r}, "
            f"length={self.length}, fingerprint={self.fingerprint!r})"
        )

    __str__ = __repr__

    def as_log_dict(self) -> dict[str, object]:
        """可安全落日志/落盘的字段集合（不含 value）。"""
        return {
            "name": self.name,
            "backend": self.backend,
            "slot": self.slot,
            "length": self.length,
            "fingerprint": self.fingerprint,
        }


class SecretsProvider(ABC):
    """密钥后端接口。实现必须诚实：拿不到就说拿不到。"""

    backend: str = "unknown"
    # True = 该后端相对生产目标（Vault/KMS）属降级形态，必须在 capabilities 里写明
    degraded: bool = True

    @abstractmethod
    def get_secret(self, name: str, *, slot: str = SLOT_CURRENT) -> SecretValue | None:
        """按逻辑名取密钥。未配置 / 未知 name / 未配置的槽位 → None（不抛、不造值）。"""

    @abstractmethod
    def names(self) -> tuple[str, ...]:
        """本后端已知的逻辑密钥名全集。"""

    def capabilities(self) -> dict[str, object]:
        """后端能力声明：轮换方式、持久化位置、是否降级形态。"""
        return {
            "backend": self.backend,
            "rotation": "unsupported",
            "persistence": "unknown",
            "degraded": self.degraded,
        }

    def describe(self) -> dict[str, object]:
        """整体描述（不含任何密钥值），供探针与文档校验使用。"""
        return {
            "backend": self.backend,
            "degraded": self.degraded,
            "names": list(self.names()),
            "capabilities": self.capabilities(),
        }
