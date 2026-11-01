"""api.middleware.pii — PII 加密/脱敏辅助。

职责：Fernet 对称加密（cryptography），无依赖/无密钥/解密失败时抛错，
拒绝明文落盘与明文透传（fail-closed）。历史遗留 !NOENC! 明文默认拒绝，
仅显式 PII_ALLOW_LEGACY_NOENC=1 迁移开关放行并告警。
参考 skills/fastapi-agent-module-skill/references/pii.py。

密钥来源（PR-151 接线）：走 `hero_quant.secrets.resolve.resolve_secret("pii_encryption_key", ...)`，
即 `HERO_SECRETS_BACKEND` 决定的后端；env 后端下才回退 `Settings.pii_encryption_key`。
`vault`/`kms` 未实现时取不到 key —— 行为与"未配置 key"一致：**拒绝加密/解密**（RuntimeError），
绝不静默降级成明文。
"""

from __future__ import annotations

import logging
import warnings
from typing import Optional

from hero_quant.config.settings import Settings
from hero_quant.secrets.resolve import resolve_secret

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken

    CRYPTO_AVAILABLE = True
except ImportError:
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

_PII_KEY: Optional[bytes] = None
# 缺 key 告警只打一次，避免热路径刷屏。
_WARNED_NO_KEY = False
_WARNED_NO_CRYPTO = False
# 最近一次取 key 失败的原因（探针用；不含值）：None = 未取过或取到了
_LAST_REASON: Optional[str] = None


def _get_key() -> Optional[bytes]:
    """懒加载 PII 加密口令（经 secrets provider），无密钥/无效密钥返回 None。"""
    global _PII_KEY, _WARNED_NO_KEY, _WARNED_NO_CRYPTO, _LAST_REASON
    if _PII_KEY is not None:
        return _PII_KEY
    if not CRYPTO_AVAILABLE:
        _LAST_REASON = "crypto_unavailable"
        # 缺依赖告警只打一次
        if not _WARNED_NO_CRYPTO:
            logger.warning("cryptography 未安装，PII 加密不可用")
            _WARNED_NO_CRYPTO = True
        return None
    settings = Settings()
    # PR-151：provider 优先；env 后端下才回退 Settings 同源字段；vault/kms 不可用时不回落。
    key_str, key_reason = resolve_secret("pii_encryption_key", settings.pii_encryption_key)
    if not key_str:
        _LAST_REASON = key_reason or "pii_key_missing"
        # 缺 key 告警只打一次，避免热路径刷屏
        if not _WARNED_NO_KEY:
            logger.warning(
                "PII 加密口令不可用，拒绝明文落盘 error_code=%s backend_reason=%s",
                "pii_key_missing",
                key_reason or "not_configured",
            )
            _WARNED_NO_KEY = True
        return None
    try:
        # 无效 key 不得入缓存：先校验后赋值，避免毒化后续调用的错误契约
        candidate = key_str.strip().encode()
        Fernet(candidate)  # 校验有效性
        _PII_KEY = candidate
        _LAST_REASON = None
        return _PII_KEY
    except (ValueError, TypeError) as e:
        _LAST_REASON = "pii_key_invalid"
        logger.error("PII 加密口令无效 error_code=pii_key_invalid: %s", e)
        return None


def pii_key_status() -> dict:
    """PII 密钥可用性探针（不含任何密钥值）：运维/探针/单测用。"""
    key = _get_key()
    return {
        "configured": key is not None,
        "reason": None if key is not None else (_LAST_REASON or "pii_key_missing"),
        "crypto_available": CRYPTO_AVAILABLE,
    }


def pii_encrypt(plaintext: str) -> str:
    """加密 PII；无密钥/加密失败时抛错，拒绝明文落盘（fail-closed）。"""
    if not plaintext:
        return plaintext
    key = _get_key()
    if key is None:
        raise RuntimeError("PII_ENCRYPTION_KEY 缺失：拒绝明文持久化 PII")
    try:
        return Fernet(key).encrypt(plaintext.encode()).decode()  # type: ignore[operator]
    except (ValueError, TypeError, OSError) as e:
        logger.error("PII 加密失败: %s", e)
        raise RuntimeError("PII 加密失败：拒绝明文落盘") from e


def pii_decrypt(ciphertext: str) -> str:
    """解密 PII；无密钥/解密失败时抛错，不把密文当明文返回。"""
    if not ciphertext:
        return ciphertext
    if ciphertext.startswith("!NOENC!"):
        # 历史遗留明文默认拒绝（fail-closed）：无 Fernet 认证即信任明文
        # 即未认证透传。迁移需显式 PII_ALLOW_LEGACY_NOENC=1 并留下告警审计。
        if not Settings().pii_allow_legacy_noenc:
            logger.warning("legacy !NOENC! PII 拒绝：需显式 PII_ALLOW_LEGACY_NOENC=1 迁移")
            raise RuntimeError("legacy plaintext PII rejected (!NOENC!)")
        warnings.warn("legacy !NOENC! PII 明文放行（显式迁移开关）", UserWarning, stacklevel=2)
        logger.warning("legacy !NOENC! PII 明文放行（显式迁移开关）")
        return ciphertext[7:]
    key = _get_key()
    if key is None:
        raise RuntimeError("PII_ENCRYPTION_KEY 缺失：无法解密")
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()  # type: ignore[operator]
    except (InvalidToken, ValueError, TypeError) as e:  # type: ignore[misc]
        logger.error("PII 解密失败: %s", e)
        raise


def mask_pii(value: str, mask_char: str = "*", visible_prefix: int = 3, visible_suffix: int = 4) -> str:
    """脱敏：保留前后缀，中间掩码。"""
    if not value:
        return ""
    # 防御：负数/非 int 统一 fail-closed 全掩码，避免切片语义泄露明文
    try:
        visible_prefix = int(visible_prefix)
        visible_suffix = int(visible_suffix)
    except (TypeError, ValueError):
        return mask_char * len(value)
    if visible_prefix < 0 or visible_suffix < 0:
        return mask_char * len(value)
    if len(value) <= visible_prefix + visible_suffix:
        return mask_char * len(value)
    # 关键：visible_suffix=0 时 value[-0:] == value[0:] 会追加全文明文，必须短路为空
    prefix = value[:visible_prefix] if visible_prefix > 0 else ""
    suffix = value[-visible_suffix:] if visible_suffix > 0 else ""
    return prefix + mask_char * (len(value) - visible_prefix - visible_suffix) + suffix


def is_pii_field(field_name: object) -> bool:
    """判断字段名是否为 PII（子串命中即视为敏感；计量类 token 键放行）。"""
    # 计量键显式放行，避免误杀成本统计
    allow = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
             "prompttokens", "completiontokens", "generated_tokens"}
    field_lower = str(field_name or "").lower()
    if field_lower in allow:
        return False
    pii_keywords = {
        "password", "passwd", "token", "secret", "phone", "mobile", "email",
        "id_card", "idcard", "ssn", "name", "address", "birthday", "bank",
        "card", "passport", "license", "private",
    }
    return any(kw in field_lower for kw in pii_keywords)


def _safe_log_text(value: str) -> str:
    """日志文本脱敏：截断+去换行，避免日志伪造与膨胀。"""
    return str(value)[:200].replace("\n", " ").replace("\r", " ")


def safe_log_args(args: object) -> dict:
    """日志参数脱敏：PII 掩码，未知键字符串默认脱敏（fail-closed）。"""
    if not isinstance(args, dict):
        return {}
    safe: dict = {}
    for k, v in args.items():
        if is_pii_field(k):
            safe[k] = mask_pii(str(v)) if v else None
        elif isinstance(v, str):
            # 未知键字符串默认不信任：fail-closed 全掩码（短串亦然，防 is_pii_field 漏命中泄露）
            text = _safe_log_text(v)
            safe[k] = mask_pii(text, visible_prefix=0, visible_suffix=0)
        elif isinstance(v, (int, float, bool)) or v is None:
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
    "pii_key_status",
]
