"""Task13 TDD: security 真鉴权与 Host 校验 (fail-closed)."""

import hashlib
import hmac
import importlib
import os


def test_verify_api_key_empty_rejects():
    """空key拒：未配置 HERO_API_KEY 时默认 False，仅 HERO_ALLOW_INSECURE==1 才放行."""
    os.environ.pop("HERO_API_KEY", None)
    os.environ.pop("HERO_ALLOW_INSECURE", None)
    # reload to ensure env read is fresh (verify_api_key reads env when expected_key is None)
    import hero_quant.api.security as sec
    importlib.reload(sec)

    class Req:
        headers = {"X-API-Key": "anything"}

    assert sec.verify_api_key(Req()) is False
    # 显式 allow insecure 时才放行
    os.environ["HERO_ALLOW_INSECURE"] = "1"
    importlib.reload(sec)
    assert sec.verify_api_key(Req()) is True
    # 清理
    os.environ.pop("HERO_ALLOW_INSECURE", None)
    importlib.reload(sec)


def test_host_empty_rejects():
    """Host 校验：空 host 拒，[::1]:8000 正确规范化，且 check_host fail-closed."""
    from hero_quant.api.security import check_host, _normalize_host

    # 空 host 直接拒绝，即使白名单非空
    assert check_host("", ["example.com"]) is False
    assert check_host("", None) is False
    # 正常 host 命中才放行
    assert check_host("example.com", ["example.com"]) is True
    # IPv6 bracket 处理：[::1]:8000 -> [::1]
    assert _normalize_host("[::1]:8000") == "[::1]"
    assert _normalize_host("[::1]") == "[::1]"
    # 大小写与端口去除
    assert _normalize_host("EXAMPLE.COM:8000") == "example.com"
    # check_host 对带端口的 IPv6 也应命中
    assert check_host("[::1]:8000", ["[::1]"]) is True


def test_ticket_ttl_bounded():
    """票据 TTL 有界且存储有上限 _MAX_TICKETS."""
    from hero_quant.api import security as sec

    # 必须存在 _MAX_TICKETS 且为 10000
    assert hasattr(sec, "_MAX_TICKETS")
    assert sec._MAX_TICKETS == 10000
    # TTL 默认 60 秒且 issue/consume 正常
    assert sec.SSE_TICKET_TTL_SECONDS == 60
    t = sec.issue_ticket(ttl=1)
    assert isinstance(t, str) and len(t) > 10
    assert sec.consume_ticket(t) is True
    # 已消费票据不可重用
    assert sec.consume_ticket(t) is False
    # 过期票据不可用（ttl=0 立即过期）
    t2 = sec.issue_ticket(ttl=0)
    assert sec.consume_ticket(t2) is False
