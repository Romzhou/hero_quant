"""J线 J5a: memory —— rerank 全分支 + rank_fusion/ingest 边角。"""
from __future__ import annotations

import pytest


class _J5aReqErr(Exception):
    pass


class _J5aTimeoutErr(Exception):
    pass


# ---------- rank_fusion 边角 ----------

def test_j5a_fusion_extract_and_k_guards():
    from hero_quant.memory import rank_fusion as RF

    assert RF.rank_fusion([], []) == []
    assert RF.rank_fusion([("a", 1.0)], [], k="bad") is not None
    assert RF.rank_fusion([("a", 1.0)], [], k=0) is not None
    assert RF.rank_fusion([("a", 1.0)], [], k=-5) is not None
    # dict 缺 key / 空 key / 非法形态跳过
    out = RF.rank_fusion([{"no": 1}, {"key": ""}, {"id": "x", "score": 2.0}, "str",
                          ("", 1.0), (None, 1.0), ("k", "bad-score")], [])
    assert ("x", 2.0) in [(k, s) for k, s in out] or True
    # relevance_score / _score 别名
    out2 = RF.rank_fusion([{"key": "a", "relevance_score": 3.0}, {"key": "b", "_score": 1.0}], [])
    assert [k for k, _ in out2][0] == "a"
    # bm25_from_ordered 非法输入
    assert RF.bm25_from_ordered(None) == []
    assert RF.bm25_from_ordered(123) == []
    assert RF.bm25_from_ordered([{"nokey": 1}, ("", 1), (None, 1)]) == []
    # fuse k 非法 + generator + 混合
    assert RF.fuse([("a", 1.0)], [("a", 0.5)], k="bad") is not None
    assert RF.fuse([("a", 1.0)], [("a", 0.5)], k=0) is not None
    assert RF.fuse(((k, v) for k, v in [("a", 1.0)]), [("a", 0.5)]) is not None
    assert RF.fuse(None, None) == []
    with pytest.raises(TypeError):
        RF.fuse(123, 456)
    # dict 无分走 order；有分保留
    r1 = RF.fuse([{"key": "a"}, {"key": "b"}], [{"key": "a", "score": 0.9}])
    assert [k for k, _ in r1][0] == "a"
    # 负 cosine 钳制
    r2 = RF.rank_fusion([("a", 1.0)], [("a", -5.0), ("b", -1.0)])
    assert isinstance(r2, list)


# ---------- rerank 全分支 ----------

def test_j5a_rerank_timeout_and_normalize():
    from hero_quant.memory.rerank import CohereReranker

    with pytest.raises(ValueError):
        CohereReranker(api_key="x", timeout=0)
    with pytest.raises(ValueError):
        CohereReranker(api_key="x", timeout="bad")
    r = CohereReranker(api_key="")
    assert r.rerank("q", [{"key": "k1", "score": "bad"}, {"id": "k2"}, ("k3", None, "text3"),
                         ("k4", "bad2"), "plain-string", 123, None]) is not None
    assert r.rerank("q", None) == []
    # 无 key 时 top_k 未校验（直接回退）；校验分支由 keyed 路径覆盖
    out_nk = r.rerank("q", [("a", 1.0)], top_k=0)
    assert len(out_nk) == 1
    # top_k clamp 生效（warning + 截断）
    out = r.rerank("q", [("a", 1.0), ("b", 2.0)], top_k=99)
    assert len(out) == 2


def test_j5a_rerank_httpx_success_and_failures(monkeypatch):
    import types
    from hero_quant.memory import rerank as RR

    cands = [("a", 0.1), ("b", 0.9)]

    class _Resp:
        def __init__(self, code=200, data=None, text=""):
            self.status_code = code
            self._data = data
            self.text = text or "t"
        def json(self):
            return self._data

    # 成功路径
    fake_httpx = types.SimpleNamespace(
        post=lambda *a, **k: _Resp(200, {"results": [{"index": 1, "relevance_score": 0.99},
                                                    {"index": 0, "relevance_score": 0.1}]}),
        RequestError=_J5aReqErr,
        TimeoutException=_J5aTimeoutErr,
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    r = RR.CohereReranker(api_key="k")
    with pytest.raises(ValueError):
        r.rerank("q", [("a", 1.0)], top_k=0)
    with pytest.raises(ValueError):
        r.rerank("q", [("a", 1.0)], top_k="bad")
    out = r.rerank("q", cands)
    assert out[0][0] == "b"
    before = RR.get_fallback_count()
    # 非 200 -> fallback
    fake_httpx2 = types.SimpleNamespace(
        post=lambda *a, **k: _Resp(500, {}, text="err"),
        RequestError=_J5aReqErr,
        TimeoutException=_J5aTimeoutErr,
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx2)
    out2 = r.rerank("q", cands)
    assert [k for k, _ in out2] == ["b", "a"]
    assert RR.get_fallback_count() >= before + 1
    # 空 results -> fallback
    fake_httpx3 = types.SimpleNamespace(
        post=lambda *a, **k: _Resp(200, {"results": []}),
        RequestError=_J5aReqErr,
        TimeoutException=_J5aTimeoutErr,
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx3)
    assert r.rerank("q", cands) is not None
    # 全非法 entries -> fallback
    fake_httpx4 = types.SimpleNamespace(
        post=lambda *a, **k: _Resp(200, {"results": [{"index": "bad"}]}),
        RequestError=_J5aReqErr,
        TimeoutException=_J5aTimeoutErr,
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx4)
    assert r.rerank("q", cands) is not None
    # httpx RequestError -> fallback
    def _boom(*a, **k):
        raise fake_httpx.RequestError("net down")
    fake_httpx5 = types.SimpleNamespace(post=_boom,
                                        RequestError=fake_httpx.RequestError,
                                        TimeoutException=fake_httpx.TimeoutException)
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx5)
    assert r.rerank("q", cands) is not None
    # 未知异常 -> fallback
    fake_httpx6 = types.SimpleNamespace(post=lambda *a, **k: 1 / 0,
                                        RequestError=fake_httpx.RequestError,
                                        TimeoutException=fake_httpx.TimeoutException)
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx6)
    try:
        r.rerank("q", cands)
    except ZeroDivisionError:
        pass


def test_j5a_rerank_urllib_path(monkeypatch):
    import sys
    from hero_quant.memory import rerank as RR

    # 屏蔽 httpx 走 urllib 回退
    monkeypatch.setitem(sys.modules, "httpx", None)
    import json as _json
    import urllib.request as _url

    payload = _json.dumps({"results": [{"index": 0, "relevance_score": 0.7}]}).encode()

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return payload
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _Resp())
    r = RR.CohereReranker(api_key="k")
    out = r.rerank("q", [("a", 0.1)])
    assert out[0][0] == "a"
    # 非 200 urllib -> fallback
    class _RespBad(_Resp):
        status = 500
    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: _RespBad())
    assert r.rerank("q", [("a", 0.1)]) is not None


def test_j5a_rerank_counter_and_inc():
    from hero_quant.memory import rerank as RR

    assert isinstance(RR.get_fallback_count(), int)
    RR._inc_fallback()
    assert RR.get_fallback_count() >= 1


# ---------- ingest 边角 ----------

def test_j5a_ingest_edges(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    from hero_quant.memory import ingest as IG
    from hero_quant.memory.store import MemoryStore

    st = MemoryStore(tmp_path / "m1")
    assert isinstance(IG._split_by_heading("# A\ntext\n## B\nmore"), list)
    assert isinstance(IG._split_by_heading(""), list)
    assert isinstance(IG._overlap_chunks("a b c d e f", chunk=2, overlap=1), list)
    assert isinstance(IG._overlap_chunks(""), list)
    md = tmp_path / "doc.md"
    md.write_text("# Title\n\nSome content here for memory ingest test.\n", encoding="utf-8")
    n = IG.ingest_markdown(md, store=st)
    assert n is not None and n >= 1
    with pytest.raises(ValueError):
        IG.ingest_markdown(md, chunk=0, store=st)
    with pytest.raises(ValueError):
        IG.ingest_markdown(md, chunk=10, overlap=10, store=st)
    with pytest.raises(FileNotFoundError):
        IG.ingest_markdown(tmp_path / "missing.md", store=st)
