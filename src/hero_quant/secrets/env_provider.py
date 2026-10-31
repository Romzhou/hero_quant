"""PR-151 env 密钥后端 —— 当前唯一真实后端。

要点：
- 逻辑名 → 环境变量名的映射集中在本模块的 `SECRET_ENV_VARS`，不在业务代码里散落环境变量直读。
- env 读取经 config 网关 `hero_quant.config.settings.secret_env_value` 完成
  （AGENTS §2：env gate 唯一入口；`tests/test_config.py::test_no_raw_getenv_outside_config` 锁定）。
- env 后端**不算降级**（它就是当前唯一在用的真实后端），但它有两个必须如实披露的性质：
  `rotation: manual`（无自动轮换）、`persistence: env_file`（明文落 `.env`）。
"""

from __future__ import annotations

from typing import Mapping

from hero_quant.config.settings import secret_env_value

from .base import SLOT_CURRENT, SLOT_NEXT, SecretValue, SecretsProvider

# 逻辑名 → 环境变量名（唯一登记处；新增密钥先改这里，再改 docs/SECURITY-SECRETS.md）
SECRET_ENV_VARS: Mapping[str, str] = {
    "llm_api_key": "HERO_API_KEY",
    "cohere_api_key": "COHERE_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "pii_encryption_key": "PII_ENCRYPTION_KEY",
    "pg_password": "HERO_PG_PASSWORD",
    "redis_password": "HERO_REDIS_PASSWORD",
}

# 双读窗口期的新值变量名后缀：HERO_API_KEY -> HERO_API_KEY_NEXT
NEXT_SUFFIX = "_NEXT"


class EnvSecretsProvider(SecretsProvider):
    """进程环境 / `.env` 后端：明文，人工轮换，无版本概念。"""

    backend = "env"
    degraded = False  # 当前唯一真实后端，不算降级；明文风险见 capabilities.persistence

    def env_var(self, name: str, *, slot: str = SLOT_CURRENT) -> str | None:
        """逻辑名 → 环境变量名；未知 name 返回 None。槽位非法是编程错误，抛 ValueError。"""
        base = SECRET_ENV_VARS.get(name)
        if base is None:
            return None
        if slot == SLOT_CURRENT:
            return base
        if slot == SLOT_NEXT:
            return base + NEXT_SUFFIX
        raise ValueError(f"unknown secret slot {slot!r} (expected one of {('current', 'next')})")

    def get_secret(self, name: str, *, slot: str = SLOT_CURRENT) -> SecretValue | None:
        var = self.env_var(name, slot=slot)
        if var is None:  # 未知 name —— 不抛、不造值
            return None
        raw = secret_env_value(var)  # 空串视同未配置
        if raw is None:
            return None
        return SecretValue(name=name, value=raw, backend=self.backend, slot=slot)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(SECRET_ENV_VARS))

    def capabilities(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "rotation": "manual",  # 人工改 .env，本模块不自动写入任何密钥
            "persistence": "env_file",  # 明文落 .env —— 未覆盖范围，见 docs/SECURITY-SECRETS.md
            "degraded": self.degraded,
            "dual_read_slot": True,  # 支持 <VAR>_NEXT 双读窗口
            "versioned": False,  # env 无版本概念，版本由 rotation.py 记账
        }
