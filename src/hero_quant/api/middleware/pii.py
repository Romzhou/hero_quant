"""api.middleware.pii — PII 加密/脱敏辅助。

职责：Fernet 对称加密（cryptography），无依赖/无密钥时回退 !NOENC!/掩码。
参考 skills/fastapi-agent-module-skill/references/pii.py。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken

    CRYPTO_AVAILABLE = True
except ImportError:
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

_PII_KEY: Optional[bytes] = None


def _get_key() -> Optional[bytes]:
    """懒加载 PII_ENCRYPTION_KEY，无密钥/无效密钥返回 None。"""
    global _PII_KEY
    if _PII_KEY is not None:
        return _PII_KEY
    if not CRYPTO_AVAILABLE:
        logger.warning("cryptography 未安装，PII 加密不可用")
        return None
    key_str = os.getenv("PII_ENCRYPTION_KEY")
    if not key_str:
        logger.warning("PII_ENCRYPTION_KEY 未设置，PII 回退掩码/明文标记")
        return None
    try:
        _PII_KEY = key_str.encode()
        Fernet(_PII_KEY)  # 校验有效性
        return _PII_KEY
    except (ValueError, TypeError) as e:
        logger.error("PII_ENCRYPTION_KEY 无效: %s", e)
        return None


def pii_encrypt(plaintext: str) -> str:
    """加密 PII；无密钥时返回 !NOENC! 前缀便于排查。"""
    if not plaintext:
        return plaintext
    key = _get_key()
    if key is None:
        return f"!NOENC!{plaintext}"
    try:
        return Fernet(key).encrypt(plaintext.encode()).decode()  # type: ignore[operator]
    except (ValueError, TypeError, OSError) as e:
        logger.error("PII 加密失败: %s", e)
        return f"!NOENC!{plaintext}"


def pii_decrypt(ciphertext: str) -> str:
    """解密 PII；兼容 !NOENC! 历史数据。"""
    if not ciphertext:
        return ciphertext
    if ciphertext.startswith("!NOENC!"):
        return ciphertext[7:]
    key = _get_key()
    if key is None:
        return ciphertext
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()  # type: ignore[operator]
    except (InvalidToken, ValueError, TypeError) as e:  # type: ignore[misc]
        logger.error("PII 解密失败: %s", e)
        return ciphertext


def mask_pii(value: str, mask_char: str = "*", visible_prefix: int = 3, visible_suffix: int = 4) -> str:
    """脱敏：保留前后缀，中间掩码。"""
    if not value:
        return ""
    if len(value) < visible_prefix + visible_suffix:
        return mask_char * len(value)
    return value[:visible_prefix] + mask_char * (len(value) - visible_prefix - visible_suffix) + value[-visible_suffix:]


def is_pii_field(field_name: str) -> bool:
    """判断字段名是否为 PII。"""
    pii_keywords = {"password", "token", "secret", "phone", "email", "id_card", "idcard", "ssn"}
    field_lower = (field_name or "").lower()
    return any(kw in field_lower for kw in pii_keywords)


def safe_log_args(args: dict) -> dict:
    """日志参数脱敏：PII 掩码，非标量记类型名。"""
    safe: dict = {}
    for k, v in (args or {}).items():
        if is_pii_field(k):
            safe[k] = mask_pii(str(v)) if v else None
        elif isinstance(v, (str, int, float, bool)):
            safe[k] = v
        else:
            safe[k] = type(v).__name__
    return safe


__all__ = [
    "pii_encrypt",
    "pii_decrypt",
    "mask_pii",
    "is_pii_field",
    "safe_log_args",
    "CRYPTO_AVAILABLE",
]
