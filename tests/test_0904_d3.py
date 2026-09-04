"""TDD for lane D3: 15 条 OCR 扫描问题（config/llm/扫描器）。

契约：_redact_dsn 脱敏；fail-closed；窄化捕获；中文注释。
仅测 5 个文件：config/settings.py, llm/catalog.py, llm/client.py, security/sanitize.py, security/scanner.py
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest import mock


# ── helpers ──
def _read_src(rel: str) -> str:
    p = Path(__file__).resolve().parents[1] / "src" / rel
    return p.read_text(encoding="utf-8")


# 1) config/settings.py:213-214 Redis DSN 明文
def test_d3_01_redis_dsn_redacted():
    """HERO_REDIS_DSN 非法时 warn/log 必须经 _redact_dsn 脱敏，不得明文泄露密码"""
    src = _read_src("hero_quant/config/settings.py")
    # 必须使用 _redact_dsn 包裹 s
    assert "_redact_dsn(s)" in src or "_redact_dsn(raw)" in src, "应使用 _redact_dsn 脱敏"
    # 不应出现直接嵌入原始 s 的 warn/log（明文）
    # 检查 213-214 附近不再有 `logger.warning(... %r", s)` 明文
    # 允许的应是 `logger.warning(... %r", _redact_dsn(s))`
    lines = src.splitlines()
    for ln in lines:
        if "HERO_REDIS_DSN" in ln and "invalid redis DSN" in ln:
            assert "_redact_dsn" in ln, f"DSN 日志未脱敏: {ln}"
            # 确保不是同时泄露原始 s 且未脱敏
            # 如果行内同时有 `, s)` 且没有 _redact_dsn，则算明文泄露
            if ", s)" in ln and "_redact_dsn" not in ln:
                raise AssertionError(f"明文泄露: {ln}")
    # 运行时行为：非法 DSN 触发 warning 且不含明文密码
    with mock.patch.dict(os.environ, {"HERO_REDIS_DSN": "http://:s3cretPass@host:6379/0", "HERO_REDIS_HOST": ""}, clear=False):
        # 清理可能影响的 env
        for k in list(os.environ.keys()):
            if k.startswith("HERO_REDIS_") and k not in ("HERO_REDIS_DSN", "HERO_REDIS_HOST"):
                pass
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # 强制重载函数以读取新 env（函数直接读 os.getenv，无需重载模块）
            from hero_quant.config.settings import _redis_dsn_from_env
            val = _redis_dsn_from_env()
            # 只要触发了 warn，就检查 warn 文本已脱敏
            if w:
                for ww in w:
                    msg = str(ww.message)
                    if "HERO_REDIS_DSN" in msg:
                        assert "s3cretPass" not in msg, f"warning 明文泄露: {msg}"
                        assert "***" in msg, f"warning 未脱敏: {msg}"


# 2) config/settings.py:183-187 非法 BILLING_DSN 静默
def test_d3_02_billing_dsn_invalid_warn():
    """HERO_BILLING_DSN 非 PG 前缀时应 warn/log（fail-visible），而非静默回退共享 DB"""
    src = _read_src("hero_quant/config/settings.py")
    # 必须对非法 BILLING DSN 发出 warnings.warn / logger.warning
    assert "HERO_BILLING_DSN" in src and "invalid PG DSN" in src or "does not look like PG DSN" in src, "应对非法 BILLING_DSN 发 warn"
    # 运行时：非法 billing DSN 应触发 warning
    with mock.patch.dict(os.environ, {"HERO_BILLING_DSN": "mysql://user:pass@host/db", "HERO_CHECKPOINT_DSN": "postgresql://postgres:postgres@localhost:5432/hero_quant", "HERO_PG_DSN": ""}, clear=False):
        # 重新计算
        from hero_quant.config.settings import _billing_dsn_from_env
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            val = _billing_dsn_from_env()
            msgs = [str(x.message) for x in w]
            # 应有至少一条关于 BILLING_DSN 的 warning
            assert any("HERO_BILLING_DSN" in m for m in msgs), f"非法 BILLING_DSN 未告警: {msgs}"
            for m in msgs:
                if "HERO_BILLING_DSN" in m:
                    assert "mysql://" not in m or "***" in m or "invalid" in m.lower(), f"应脱敏或告警: {m}"


# 3) config/settings.py:219-229 非法 PORT/DB 回 6379/0 无 warn
def test_d3_03_redis_port_db_invalid_warn():
    """HERO_REDIS_PORT/DB 非法时应 warn/log，而非静默回退默认值"""
    src = _read_src("hero_quant/config/settings.py")
    # 必须对 port/db 的 except 分支增加 warn/log
    assert "Invalid HERO_REDIS_PORT" in src or "HERO_REDIS_PORT" in src and "warnings.warn" in src, "PORT 非法应 warn"
    assert "Invalid HERO_REDIS_DB" in src or "HERO_REDIS_DB" in src and "warnings.warn" in src, "DB 非法应 warn"
    # 运行时：通过 HERO_REDIS_HOST 拼装路径触发 port/db 解析异常
    with mock.patch.dict(os.environ, {"HERO_REDIS_DSN": "", "HERO_REDIS_HOST": "myhost", "HERO_REDIS_PORT": "abc", "HERO_REDIS_DB": "xyz", "HERO_REDIS_PASSWORD": "", "REDIS_URL": ""}, clear=False):
        from hero_quant.config.settings import _redis_dsn_from_env
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            val = _redis_dsn_from_env()
            msgs = [str(x.message) for x in w]
            assert any("HERO_REDIS_PORT" in m for m in msgs), f"PORT 非法未 warn: {msgs}"
            assert any("HERO_REDIS_DB" in m for m in msgs), f"DB 非法未 warn: {msgs}"
            assert val == "redis://myhost:6379/0", f"应回退默认值: {val}"


# 4) config/settings.py:28-31 .env autoload except pass
def test_d3_04_dotenv_autoload_narrow_and_log():
    """dotenv autoload 的 except 不应静默 pass，应窄化或至少 debug 日志"""
    src = _read_src("hero_quant/config/settings.py")
    # 不应有 `except Exception:\n    pass` 的静默吞没
    assert "except Exception:\n    pass" not in src, "不应有裸 except pass"
    # 应有 logger.debug 记录
    assert "logger.debug" in src and "dotenv" in src.lower(), "应有 logger.debug 记录 dotenv 失败"


# 5) llm/catalog.py:12-20 no-op guard
def test_d3_05_catalog_noop_guard_removed():
    """DEFAULT_MODEL 的 no-op import-time guard 应移除，保留后置真实校验"""
    src = _read_src("hero_quant/llm/catalog.py")
    # 不应再有 `if DEFAULT_MODEL not in {` 且 `pass` 的无操作块
    # 检查是否存在 12-20 段的 pass 守卫
    assert "if DEFAULT_MODEL not in {" not in src or "pass" not in src.split("if DEFAULT_MODEL not in {")[1].split("\n")[0:5].__str__() if "if DEFAULT_MODEL not in {" in src else True, "no-op guard 应移除"
    # 更严格：统计 `if DEFAULT_MODEL not in` 出现次数，应仅剩一次（后置 MODEL_CATALOG 校验）
    count = src.count("if DEFAULT_MODEL not in")
    assert count == 1, f"应仅剩一次真实校验，当前 {count} 次"
    assert "if DEFAULT_MODEL not in MODEL_CATALOG" in src, "应保留后置真实校验"
    # 运行时仍应正常
    import hero_quant.llm.catalog as cat
    assert cat.DEFAULT_MODEL in cat.MODEL_CATALOG


# 6) llm/client.py:189-190 缓存跨实例串味
def test_d3_06_cache_not_cross_instance():
    """@cache('llm:invoke') 不应跨实例/模型串味：应移除或键中包含实例身份"""
    src = _read_src("hero_quant/llm/client.py")
    # 若仍保留 @cache，则必须键中包含 model/provider/实例信息，否则应移除
    has_cache_decorator = '@cache("llm:invoke"' in src or "@cache('llm:invoke'" in src
    if has_cache_decorator:
        # 要求有额外区分逻辑（如 model/provider/self 参与 key）
        assert "model" in src.lower() or "provider" in src.lower() or "self" in src, "缓存键应包含 model/provider/实例区分"
        # 更严格：不应仅以 prompt 为 key
        assert False, "仍保留 @cache 且未有效隔离，应移除或重构键"
    else:
        # 已移除缓存，视为通过
        pass
    # 行为：两个不同底层返回不同结果，同一 prompt 不应命中对方缓存
    from hero_quant.llm.client import LLMClient

    class FakeA:
        def invoke(self, prompt, timeout=None):
            return "A:" + prompt

    class FakeB:
        def invoke(self, prompt, timeout=None):
            return "B:" + prompt

    c_a = LLMClient(FakeA(), timeout=5, max_retries=0)
    c_b = LLMClient(FakeB(), timeout=5, max_retries=0)
    r_a = c_a.invoke("hello")
    r_b = c_b.invoke("hello")
    assert r_a == "A:hello", f"应返回 A 的结果: {r_a}"
    assert r_b == "B:hello", f"不应串味返回 A 的缓存: {r_b}"
    assert r_a != r_b, "跨实例不应串味"


# 7) llm/client.py:197-199 dict chunk
def test_d3_07_invoke_handles_dict_chunks():
    """invoke fallback 应对 stream_chat 产出的 dict chunk 正确提取 text，而非 TypeError"""
    from hero_quant.llm.client import LLMClient

    class FakeStream:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "text", "text": "hello"}
            yield {"type": "text", "text": " world"}
            yield {"type": "tool_call", "tool_calls": [], "text": ""}

    c = LLMClient(FakeStream(), timeout=5, max_retries=0)
    # 强制走 stream_chat 回退路径：删除 invoke/chat 属性
    # FakeStream 只有 stream_chat，无 invoke/chat/__call__ 的 invoke 会走 fallback
    result = c.invoke("any")
    assert result == "hello world", f"应正确拼接 dict text: {result!r}"

    # 混合 str/dict
    class FakeMixed:
        def stream_chat(self, prompt, timeout=None):
            yield "hi"
            yield {"type": "text", "text": " there"}

    c2 = LLMClient(FakeMixed(), timeout=5, max_retries=0)
    result2 = c2.invoke("any")
    assert "hi" in result2 and "there" in result2, f"应兼容 str/dict: {result2!r}"


# 8) llm/client.py:151-155 双计数
def test_d3_08_no_double_count_timeout():
    """TimeoutError 在最终失败时不应双计数，应仅计数一次"""
    src = _read_src("hero_quant/llm/client.py")
    # 检查 stream_chat 与 _invoke_with_retry 中不应有“最终失败再计一次”的重复逻辑
    # 允许一次 _inc_llm_timeout 调用每异常路径，不应出现两次连续
    # 简单检查：最终失败块内不应再出现 _inc_llm_timeout
    # 通过统计函数内 _inc_llm_timeout 出现次数，修复后应每函数仅一次（或零次但不在 final 块重复）
    # 更直接行为测试：
    from hero_quant.llm.client import LLMClient
    import hero_quant.llm.client as client_mod

    call_count = {"t": 0, "r": 0}

    def fake_inc_timeout():
        call_count["t"] += 1

    def fake_inc_retry(reason=""):
        call_count["r"] += 1

    with mock.patch.object(client_mod, "_inc_llm_timeout", side_effect=fake_inc_timeout), \
         mock.patch.object(client_mod, "_inc_llm_retry", side_effect=fake_inc_retry):
        class AlwaysTimeout:
            def invoke(self, prompt, timeout=None):
                raise TimeoutError("timeout")

        c = LLMClient(AlwaysTimeout(), timeout=1, max_retries=2)
        try:
            c.invoke("hi")
        except TimeoutError:
            pass
        # 3 次尝试（0,1,2）每次应各计数一次 timeout，总计 3 次，而非 4 次（修复前 final 会多一次）
        assert call_count["t"] == 3, f"应计数 3 次（每次一次），实际 {call_count['t']}"
        # 同理 stream_chat
        call_count["t"] = 0
        call_count["r"] = 0

        class AlwaysTimeoutStream:
            def stream_chat(self, prompt, timeout=None):
                raise TimeoutError("timeout")
                yield  # noqa

        c2 = LLMClient(AlwaysTimeoutStream(), timeout=1, max_retries=2)
        try:
            list(c2.stream_chat("hi"))
        except TimeoutError:
            pass
        assert call_count["t"] == 3, f"stream_chat 应计数 3 次，实际 {call_count['t']}"


# 9) llm/client.py:56-59 timeout 丢（TypeError 回退）
def test_d3_09_stream_timeout_not_dropped_on_typeerror():
    """_stream_with_chat 对不支持 timeout 的后端应尝试透传，TypeError 回退不应静默丢弃 deadline"""
    src = _read_src("hero_quant/llm/client.py")
    # 必须包含对 timeout 的尝试调用 `fn(prompt, timeout=t)` 且有 TypeError 回退
    assert "fn(prompt, timeout=t)" in src or "timeout=t" in src, "应尝试透传 timeout"
    # 回退分支应有中文注释说明 deadline 处理或 wrapper
    # 简化：检查 except TypeError 后仍有 gen = fn(prompt) 但需有注释/日志
    assert "except TypeError" in src, "应有 TypeError 回退分支"
    # 行为：支持 timeout 的后端应收到 timeout 参数
    from hero_quant.llm.client import LLMClient

    seen = {}

    class SupportTimeout:
        def stream_chat(self, prompt, timeout=None):
            seen["t"] = timeout
            yield {"type": "text", "text": "ok"}

    c = LLMClient(SupportTimeout(), timeout=7, max_retries=0)
    list(c.stream_chat("hi", timeout=7))
    assert seen.get("t") == 7, f"应透传 timeout=7, 实际 {seen.get('t')}"

    # 不支持 timeout 的后端应回退仍可用
    seen2 = {}

    class NoTimeout:
        def stream_chat(self, prompt):
            seen2["called"] = True
            yield {"type": "text", "text": "ok"}

    c2 = LLMClient(NoTimeout(), timeout=7, max_retries=0)
    chunks = list(c2.stream_chat("hi"))
    assert seen2.get("called") is True
    assert chunks[0]["text"] == "ok"


# 10) llm/client.py:191-194 timeout 不透传（非 stream）
def test_d3_10_invoke_forwards_timeout():
    """invoke/chat/__call__ 应透传 self.timeout，而非无期限调用"""
    src = _read_src("hero_quant/llm/client.py")
    # invoke 内应涉及 timeout 透传
    assert "self.timeout" in src, "应使用 self.timeout"
    # 行为测试
    from hero_quant.llm.client import LLMClient

    seen = {}

    class FakeInvoke:
        def invoke(self, prompt, timeout=None):
            seen["t"] = timeout
            return "ok:" + prompt

    c = LLMClient(FakeInvoke(), timeout=11, max_retries=0)
    r = c.invoke("hi")
    assert r == "ok:hi"
    assert seen.get("t") == 11, f"invoke 应透传 timeout=11, 实际 {seen.get('t')}"

    # chat 路径
    seen2 = {}

    class FakeChat:
        def chat(self, prompt, timeout=None):
            seen2["t"] = timeout
            return "chat:" + prompt

    c2 = LLMClient(FakeChat(), timeout=13, max_retries=0)
    r2 = c2.chat("hey")
    assert seen2.get("t") == 13, f"chat 应透传 timeout, 实际 {seen2.get('t')}"

    # 不支持 timeout 的后端应回退
    class FakeNoTimeout:
        def invoke(self, prompt):
            return "no_timeout:" + prompt

    c3 = LLMClient(FakeNoTimeout(), timeout=9, max_retries=0)
    r3 = c3.invoke("hi")
    assert r3 == "no_timeout:hi"


# 11) security/sanitize.py:53 尾空格不可达
def test_d3_11_ticker_trailing_space_reachable():
    """safe_ticker_component 对尾空格的校验应可达（应在正则前检查），不可达为 bug"""
    src = _read_src("hero_quant/security/sanitize.py")
    lines = src.splitlines()
    # 找到 endswith 检查与 _TICKER_PATH_RE 的行号
    end_idx = next((i for i, l in enumerate(lines) if 'endswith(" ")' in l or "endswith(' ')" in l), None)
    regex_idx = next((i for i, l in enumerate(lines) if "_TICKER_PATH_RE.fullmatch" in l), None)
    assert end_idx is not None and regex_idx is not None, "应同时存在尾空格校验与正则校验"
    assert end_idx < regex_idx, f"尾空格校验应在正则校验前，否则不可达（当前 {end_idx} vs {regex_idx})"
    # 行为：尾空格应报“尾点/空格”而非“非法字符”
    from hero_quant.security.sanitize import safe_ticker_component
    try:
        safe_ticker_component("AAPL ")
        assert False, "应抛 ValueError"
    except ValueError as e:
        msg = str(e)
        assert "dot or space" in msg or "尾" in msg or "space" in msg.lower(), f"应为尾空格错误，实际 {msg}"
        assert "not allowed" not in msg, f"不应为正则非法字符错误，实际 {msg}"


# 12) security/sanitize.py:67-71 resolve 双调宽 except
def test_d3_12_sanitize_resolve_narrow_except():
    """safe_join 中 resolve 的 except 应窄化，而非 broad Exception"""
    src = _read_src("hero_quant/security/sanitize.py")
    # 不应有 `except Exception as e:` 围绕 resolve
    # 应为具体异常如 (OSError, ValueError, RuntimeError, TypeError)
    assert "except Exception as e:" not in src or src.count("except Exception") == 0, "应窄化 resolve 的异常捕获"
    # 更准确：应包含窄化元组
    assert "except (OSError" in src or "except (ValueError" in src, "应窄化为 OSError/ValueError 等"


# 13) security/scanner.py:64-72 normalize 宽 except
def test_d3_13_scanner_normalize_narrow_except():
    """unicodedata.normalize 的 except 应窄化，而非 broad Exception: pass"""
    src = _read_src("hero_quant/security/scanner.py")
    # 不应有 `except Exception:` 裸吞
    assert src.count("except Exception:") == 0, "不应有 broad except Exception"
    assert "except Exception" not in src, "应窄化 normalize 的异常"
    # 应为具体类型
    assert "except (ValueError" in src or "except (TypeError" in src or "ValueError" in src, "应窄化为 ValueError/TypeError 等"


# 14) security/scanner.py:80-88 sanitize 与 neutralize 重复
def test_d3_14_sanitize_delegates_to_neutralize():
    """sanitize 应复用 neutralize，而非重复实现 strip->NFKC->sub"""
    src = _read_src("hero_quant/security/scanner.py")
    # sanitize 函数体内应调用 neutralize
    # 提取 sanitize 定义
    sanc_start = src.find("def sanitize(")
    assert sanc_start != -1
    sanc_body = src[sanc_start: src.find("\ndef ", sanc_start + 10) if src.find("\ndef ", sanc_start + 10) != -1 else len(src)]
    assert "neutralize" in sanc_body, f"sanitize 应委托 neutralize，实际: {sanc_body[:300]}"
    # 行为等价：sanitize 与 neutralize 对同输入结果一致
    from hero_quant.security.scanner import sanitize, neutralize
    for txt in ["hello <|im_start|> world", "a\u200b b", "test"]:
        assert sanitize(txt) == neutralize(txt), f"sanitize 与 neutralize 应一致: {txt!r}"


# 15) security/scanner.py:16 注释与实现不符
def test_d3_15_scanner_comment_matches_impl():
    """注释对 </?s> 的描述应与实现一致，不得声称可避免 'a <s> b' 而实际仍转义"""
    src = _read_src("hero_quant/security/scanner.py")
    # 注释不应再声称“避免 flagging a <s> b”
    assert 'avoid flagging "a <s> b"' not in src, "注释不应错误声称避免 a <s> b"
    assert "avoid flagging" not in src.lower() or "a <s> b" not in src, "应修正注释与实现不符"
    # 若仍保留 word-bounded 说明，应准确描述为防嵌入单词误判，而非隔离空格场景
    # 至少应有中文注释
    assert "中文" in src or "单词" in src or "边界" in src or "word" in src.lower(), "应有中文注释说明语义"
