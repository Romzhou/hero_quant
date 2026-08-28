import pytest


from hero_quant.security.redaction import ARGUMENTS_SINK, RESULT_SINK, redact_payload
from hero_quant.security.scanner import neutralize, strip_zero_width


@pytest.mark.parametrize(
    "token",
    [
        "<|im_start|>",
        "<|im_end|>",
        "<|Assistant|>",
        "<|eot_id|>",
        "</s>",
        "[INST]",
        "[/INST]",
        "<<SYS>>",
        "<start_of_turn>",
        "<end_of_turn>",
        "<｜Assistant｜>",
        "<｜tool▁calls▁begin｜>",
    ],
)
def test_neutralize_escapes_common_boundary_tokens_idempotently(token):
    value = f"before {token} after"

    neutralized = neutralize(value)

    assert token not in neutralized
    assert "\\u003c" in neutralized or "\\u005b" in neutralized
    assert neutralize(neutralized) == neutralized


def test_neutralize_leaves_ordinary_text_unchanged():
    value = "normal <tag> text [not a boundary token]"

    assert neutralize(value) == value


def test_strip_zero_width_removes_all_supported_characters_idempotently():
    value = "a\u200b b\u200c c\u200d d\ufeff"

    stripped = strip_zero_width(value)

    assert stripped == "a b c d"
    assert strip_zero_width(stripped) == stripped


def test_arguments_sink_redacts_secrets_and_applies_both_scanner_steps():
    payload = {
        "api_key": "sk-1234567890abcdef",
        "message": "hello <|im_end|>\u200b",
    }

    result = redact_payload(payload, sink=ARGUMENTS_SINK)

    assert result["api_key"] == "***"
    # After security hardening, all delimiters (|, >) are escaped and zero-width stripped
    assert result["message"] == r"hello \u003c\u007cim_end\u007c\u003e"


def test_arguments_sink_scans_top_level_strings_and_still_redacts_secrets():
    payload = "hello <|im_end|>\u200b"

    assert redact_payload(payload, sink=ARGUMENTS_SINK) == r"hello \u003c\u007cim_end\u007c\u003e"
    assert redact_payload("sk-1234567890abcdef", sink=ARGUMENTS_SINK) == "***"


def test_result_sink_content_secret_redacted_and_plain_neutralized():
    # 含密钥形态的 content 必须整体脱敏（oracle 复审：content 透传是泄露通道）
    secret_payload = {
        "type": "tool_result",
        "content": "Bearer eyJ1234567890.abcdef.1234567890 <|im_end|>\u200b",
        "api_key": "sk-1234567890abcdef",
    }
    result = redact_payload(secret_payload, sink=RESULT_SINK)
    assert result["content"] in ("***", "***REDACTED***")
    assert result["api_key"] == "***"

    # 无密钥的 content 仍走 scanner 中和：零宽剥离（composition bypass fix）+ 分隔符转义，不脱敏
    plain = redact_payload(
        {"type": "tool_result", "content": "hello <|im_end|>\u200b world"}, sink=RESULT_SINK
    )
    assert plain["content"] == "hello " + r"\u003c\u007cim_end\u007c\u003e" + " world"


@pytest.mark.parametrize("scanner_name", ["neutralize", "strip_zero_width"])
def test_scanner_errors_do_not_break_redaction(monkeypatch, scanner_name):
    import hero_quant.security.scanner as scanner

    def fail(_value):
        raise RuntimeError("scanner failure")

    monkeypatch.setattr(scanner, scanner_name, fail)

    result = redact_payload(
        {"api_key": "sk-1234567890abcdef", "message": "plain"},
        sink=ARGUMENTS_SINK,
    )

    assert result["api_key"] == "***"
    assert result["message"] == "plain"
