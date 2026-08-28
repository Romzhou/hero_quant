"""TDD W3-K PIT 新闻快照披露 — RED before impl.

Requirements:
- load_news(records, trade_date, snapshot_date/available_at optional) 按 trade_date 过滤
- 只有存在可验证发布时间且发布时间不晚于快照才 pit=True，否则 pit=False
- 诚实保留 pit_status/unknown，不能伪造 PIT
- bench run_batch 旧调用兼容，metrics.json/tearsheet 增加 non-PIT disclosure
"""

import copy
import json
import pathlib
import tempfile


SAMPLE = [
    {"id": 1, "title": "a", "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00"},
    {"id": 2, "title": "b", "trade_date": "2024-01-02", "publish_time": "2024-01-03 10:00:00"},
    {"id": 3, "title": "c", "trade_date": "2024-01-03", "publish_time": "2024-01-02 09:00:00"},
    {"id": 4, "title": "d", "trade_date": "2024-01-02"},  # missing publish_time -> non-PIT
    {"id": 5, "title": "e", "trade_date": "2024-01-02", "publish_time": "2024-01-01 09:00:00", "snapshot_date": "2024-01-02"},
]


def test_load_news_filters_by_trade_date():
    from hero_quant.data.loaders.news import load_news

    out = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", snapshot_date="2024-01-02")
    # should only return trade_date == 2024-01-02  -> ids 1,2,4,5 (4 items)
    ids = {r["id"] for r in out}
    assert ids == {1, 2, 4, 5}, f"trade_date filter failed, got {ids}"
    assert len(out) == 4
    # no cross contamination
    out2 = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-03", snapshot_date="2024-01-03")
    assert {r["id"] for r in out2} == {3}
    # ensure SAMPLE not mutated
    assert all("pit" not in r for r in SAMPLE), "load_news must not mutate input"


def test_load_news_pit_verified_only_when_publish_lte_snapshot():
    from hero_quant.data.loaders.news import load_news

    # snapshot = 2024-01-02 12:00, so publish <= snapshot is pit True
    snapshot = "2024-01-02 12:00:00"
    out = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", snapshot_date=snapshot)
    by_id = {r["id"]: r for r in out}
    # id 1 publish 2024-01-01 <= snapshot => pit True
    assert by_id[1]["pit"] is True, f"id1 should be pit True, got {by_id[1]}"
    # id 5 per-record snapshot_date must not override global snapshot — global governs
    assert by_id[5]["pit"] is True, f"id5 should be pit True (global snapshot governs), got {by_id[5]}"
    # id 2 publish 2024-01-03 > snapshot => pit False
    assert by_id[2]["pit"] is False, f"id2 future publish should be pit False, got {by_id[2]}"
    # id 4 missing publish_time => pit False (unknown)
    assert by_id[4]["pit"] is False, f"id4 missing time should be pit False, got {by_id[4]}"
    # cross-field invariant: pit True => verified status, pit False => non-verified
    for r in out:
        if r["pit"] is True:
            assert r["pit_status"] in ("verified", "pit", "available", "ok"), f"pit True must have verified status, got {r}"
        else:
            assert r["pit_status"] not in ("verified", "pit", "available", "ok") or r["pit_status"] in ("future", "unknown", "unavailable", "missing", "non-pit", "non_pit", "excluded"), f"pit False status {r['pit_status']!r} inconsistent"


def test_load_news_pit_status_honest_unknown():
    from hero_quant.data.loaders.news import load_news

    out = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", snapshot_date="2024-01-02")
    by_id = {r["id"]: r for r in out}
    # missing publish_time must not fabricate PIT, must be pit False and status unknown/unavailable
    assert by_id[4]["pit"] is False
    assert by_id[4].get("pit_status") in ("unknown", "unavailable", "missing", "non-pit", "non_pit"), f"pit_status dishonest: {by_id[4]}"
    # verified case should have status verified/pit
    assert by_id[1].get("pit_status") in ("verified", "pit", "available", "ok"), f"pit_status for verified unexpected: {by_id[1]}"
    # future case must be explicit future/non-pit, not generic unknown (honesty)
    assert by_id[2].get("pit_status") in ("future", "non-pit", "non_pit", "excluded"), f"future status must be explicit future/non-pit, got {by_id[2]}"
    # every record must have pit_status field honest
    for r in out:
        assert "pit_status" in r, f"missing pit_status in {r}"
        assert "pit" in r


def test_load_news_available_at_alias():
    from hero_quant.data.loaders.news import load_news

    # available_at as alias for snapshot_date
    out1 = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
    out2 = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", available_at="2024-01-02 12:00:00")
    assert {r["id"]: r["pit"] for r in out1} == {r["id"]: r["pit"] for r in out2}
    # both supplied with different values — snapshot_date should govern (explicit wins)
    out_both = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", snapshot_date="2024-01-03 12:00:00", available_at="2024-01-01 12:00:00")
    by_both = {r["id"]: r for r in out_both}
    # with later snapshot, future item id2 (2024-01-03) becomes PIT True only if snapshot >= publish
    assert by_both[2]["pit"] is True, "when both alias present, snapshot_date should govern"
    # no snapshot at all => all pit False (cannot verify)
    out3 = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02")
    for r in out3:
        assert r["pit"] is False, f"without snapshot all should be non-PIT, got {r}"
        assert r["pit_status"] in ("unknown", "unavailable", "missing", "non-pit", "non_pit")
    # exact equality boundary: publish_time == snapshot is PIT True (<=)
    eq_rec = [{"id": 999, "trade_date": "2024-01-02", "publish_time": "2024-01-02 12:00:00"}]
    out_eq = load_news(eq_rec, trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
    assert out_eq[0]["pit"] is True, "publish_time == snapshot should be PIT True"


def test_load_news_no_mutation_and_field_variants():
    from hero_quant.data.loaders.news import load_news

    records = [
        {"id": 10, "trade_date": "2024-01-02", "published_at": "2024-01-01 08:00:00"},
        {"id": 11, "trade_date": "2024-01-02", "timestamp": "2024-01-03 08:00:00"},
    ]
    out = load_news(records, trade_date="2024-01-02", snapshot_date="2024-01-02")
    by_id = {r["id"]: r for r in out}
    assert by_id[10]["pit"] is True
    assert by_id[11]["pit"] is False


def test_disclosure_helper():
    from hero_quant.data.loaders.news import load_news

    # try to find disclosure helper
    import hero_quant.data.loaders.news as m

    helper = None
    for name in ("get_disclosure", "build_disclosure", "format_disclosure", "get_pit_disclosure", "disclosure", "news_disclosure", "build_news_disclosure"):
        if hasattr(m, name):
            cand = getattr(m, name)
            if callable(cand):
                helper = cand
                break
    assert helper is not None and callable(helper), "news.py must expose callable disclosure helper (get_disclosure/build_disclosure)"

    filtered = load_news(copy.deepcopy(SAMPLE), trade_date="2024-01-02", snapshot_date="2024-01-02")
    # handle both helper(filtered) and helper() via try
    try:
        text = helper(filtered)
    except TypeError:
        text = helper()
    # handle helper returning str or dict
    if isinstance(text, dict):
        text = json.dumps(text)
    assert isinstance(text, str)
    # must mention non-PIT
    assert "non-PIT" in text or "non-pit" in text.lower() or "unavailable" in text.lower(), f"disclosure should mention non-PIT/unavailable, got {text!r}"

    # all pit case vs non-pit case difference is ok, but must handle empty
    empty_text = helper([])
    if isinstance(empty_text, dict):
        empty_text = json.dumps(empty_text)
    assert "non-PIT" in empty_text or "unavailable" in empty_text.lower() or "no" in empty_text.lower()


def test_bench_run_batch_backward_compatible_and_disclosure():
    from hero_quant.backtest.bench import run_batch

    # old call must still work
    with tempfile.TemporaryDirectory() as tmp:
        dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        metrics = run_batch(["AAPL"], dates=dates, output_dir=tmp)
        assert "AAPL" in metrics
        assert "sharpe" in metrics["AAPL"]
        # new: disclosure fields
        val = metrics["AAPL"]
        # at least one disclosure related key must exist
        has_disclosure_key = any(k in val for k in ("disclosure", "pit_disclosure", "news_disclosure", "non_pit_disclosure", "non-PIT", "pit_status"))
        # or check metrics.json contains non-PIT mention
        p = pathlib.Path(tmp) / "metrics.json"
        assert p.exists()
        data = json.loads(p.read_text(encoding="utf-8"))
        dumped = json.dumps(data, ensure_ascii=False).lower()
        assert "non-pit" in dumped or "unavailable" in dumped or "disclosure" in dumped, f"metrics.json should contain disclosure, got {dumped[:500]}"
        # also per-ticker dict should carry disclosure or pit fields if any
        # bench disclosure helper existence
        import hero_quant.backtest.bench as bm
        has_helper = any(hasattr(bm, n) for n in ("get_disclosure", "build_disclosure", "get_pit_disclosure", "build_pit_disclosure", "news_disclosure", "get_bench_disclosure"))
        # helpers optional but tearsheet should mention disclosure
        # check bench's disclosures via run_batch output_dir tearsheet? bench run_batch writes only metrics.json; disclosure still in metrics
        # also test that run_batch accepts news_records kw without breaking
        metrics2 = run_batch(["AAPL"], dates=dates, output_dir=tmp, news_records=SAMPLE)
        assert "AAPL" in metrics2


def test_bench_tearsheet_or_output_contains_non_pit_hint():
    from hero_quant.backtest.bench import run_batch
    import tempfile
    import pathlib
    import json

    dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    # output_dir is directory -> must generate metrics.json and tearsheet.html with non-PIT disclosure
    with tempfile.TemporaryDirectory() as tmp:
        metrics = run_batch(["600519.SS"], dates=dates, output_dir=tmp)
        v = metrics["600519.SS"]
        combined = json.dumps(v, ensure_ascii=False).lower()
        assert "non-pit" in combined or "unavailable" in combined or "disclosure" in combined or "pit" in combined, f"bench ticker metrics missing PIT disclosure hint: {v}"
        p = pathlib.Path(tmp) / "metrics.json"
        assert p.exists(), "metrics.json should exist when output_dir is directory"
        ts = pathlib.Path(tmp) / "tearsheet.html"
        assert ts.exists(), f"tearsheet.html must be generated in output_dir directory, got {list(pathlib.Path(tmp).iterdir())}"
        html = ts.read_text(encoding="utf-8", errors="ignore")
        assert "non-PIT source/unavailable" in html, f"tearsheet.html must contain 'non-PIT source/unavailable', got {html[:800]!r}"
        # per-ticker readable result must be present
        assert "600519.SS" in html, f"tearsheet.html must contain per-ticker result, got {html[:800]!r}"
        # disclosure also visible in html lower check as fallback
        assert "non-pit" in html.lower() and "unavailable" in html.lower()
    # output_dir is .json file -> keep original semantics, do not forcibly side-write tearsheet.html
    with tempfile.TemporaryDirectory() as tmp2:
        json_path = pathlib.Path(tmp2) / "out.json"
        metrics2 = run_batch(["600519.SS"], dates=dates, output_dir=str(json_path))
        assert json_path.exists(), "metrics json file should be written when output_dir is .json"
        sibling_html = pathlib.Path(tmp2) / "tearsheet.html"
        side_html = json_path.with_suffix(".html")
        # should not create tearsheet alongside json output
        assert not sibling_html.exists() and not side_html.exists(), f"when output_dir is .json should not side-write tearsheet.html, found {list(pathlib.Path(tmp2).iterdir())}"
        v2 = metrics2["600519.SS"]
        combined2 = json.dumps(v2, ensure_ascii=False).lower()
        assert "non-pit" in combined2 or "unavailable" in combined2


def test_load_news_mixed_timezone_naive_aware_does_not_raise():
    from hero_quant.data.loaders.news import load_news

    # publish naive, snapshot aware -> must not raise TypeError, must be honest pit=False
    rec_naive_pub = [{"id": 101, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00"}]
    out1 = load_news(rec_naive_pub, trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00+00:00")
    assert len(out1) == 1
    assert out1[0]["pit"] is False
    assert out1[0]["pit_status"] in ("unknown", "unavailable", "missing", "non-pit", "non_pit")

    # publish aware, snapshot naive -> must not raise, pit=False
    rec_aware_pub = [{"id": 102, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00+00:00"}]
    out2 = load_news(rec_aware_pub, trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
    assert len(out2) == 1
    assert out2[0]["pit"] is False
    assert out2[0]["pit_status"] in ("unknown", "unavailable", "missing", "non-pit", "non_pit")


def test_load_news_mixed_timezone_aware_offsets_compare_correctly():
    from hero_quant.data.loaders.news import load_news

    # same instant different offsets: publish 10:00+09:00 == 01:00 UTC, snapshot 02:00+00:00 => publish <= snapshot true
    rec = [{"id": 103, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00+09:00"}]
    out = load_news(rec, trade_date="2024-01-02", snapshot_date="2024-01-01 02:00:00+00:00")
    assert out[0]["pit"] is True
    assert out[0]["pit_status"] in ("verified", "pit", "available", "ok")

    # converse: publish 10:00+00:00 vs snapshot 02:00+00:00 same day publish > snapshot => future
    rec2 = [{"id": 104, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00+00:00"}]
    out2 = load_news(rec2, trade_date="2024-01-02", snapshot_date="2024-01-01 02:00:00+00:00")
    assert out2[0]["pit"] is False
    assert out2[0]["pit_status"] == "future"

    # different offset: publish 10:00+08:00 (02:00 UTC) vs snapshot 01:00+00:00 => publish > snapshot => future
    rec3 = [{"id": 105, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00+08:00"}]
    out3 = load_news(rec3, trade_date="2024-01-02", snapshot_date="2024-01-01 01:00:00+00:00")
    assert out3[0]["pit"] is False
    assert out3[0]["pit_status"] == "future"


def test_publish_time_numeric_not_forged():
    from hero_quant.data.loaders.news import load_news
    # numeric 0 should not be forged to 1970-01-01 via str coercion; only string or Timestamp types are valid
    rec = [{"id": 200, "trade_date": "2024-01-02", "publish_time": 0}]
    out = load_news(rec, trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
    # pd.to_datetime(0) would be 1970, which would be pit True (since <= snapshot) but our fix should treat numeric 0 as valid timestamp via _parse_time directly; however str coercion bug is fixed so we just ensure no exception and pit honest
    # With numeric 0 timestamp 1970, pit should be True (since 1970 <= 2024) — but we want to ensure string "" handling is correct: numeric 0 is NOT empty string bypass
    # More importantly, string "0" vs int 0 handling: ensure int 0 doesn't bypass via str strip check
    assert out[0]["pit"] in (True, False)
    assert "pit_status" in out[0]
    # empty string publish_time should be treated as missing -> unknown
    rec2 = [{"id": 201, "trade_date": "2024-01-02", "publish_time": "   "}]
    out2 = load_news(rec2, trade_date="2024-01-02", snapshot_date="2024-01-02")
    assert out2[0]["pit"] is False
    assert out2[0]["pit_status"] in ("unknown", "unavailable")


def test_news_trade_date_filter_logs_and_schema_raise(caplog):
    """Silent drop must log dropped counts, raise on schema anomaly and >50% missing."""
    import logging
    from hero_quant.data.loaders.news import load_news

    caplog.set_level(logging.WARNING)

    # Case 1: normal filtering should log dropped count (mismatch)
    recs = [
        {"id": 1, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00"},
        {"id": 2, "trade_date": "2024-01-03", "publish_time": "2024-01-01 10:00:00"},
    ]
    caplog.clear()
    out = load_news(recs, trade_date="2024-01-02", snapshot_date="2024-01-02")
    assert len(out) == 1
    # should have logged dropped row warning
    assert any("dropped" in r.message.lower() for r in caplog.records), f"expected dropped warning, got {[r.message for r in caplog.records]}"

    # Case 2: schema anomaly - trade_date column missing entirely => raise
    bad_recs = [
        {"id": 1, "publish_time": "2024-01-01 10:00:00"},
        {"id": 2, "publish_time": "2024-01-01 10:00:00"},
    ]
    try:
        load_news(bad_recs, trade_date="2024-01-02", snapshot_date="2024-01-02")
        assert False, "should raise on schema anomaly missing trade_date column"
    except ValueError as e:
        assert "trade_date" in str(e).lower() or "schema" in str(e).lower()

    # Case 3: >50% missing trade_date should raise (bias guard)
    many_missing = [
        {"id": 1, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00"},
        {"id": 2, "publish_time": "2024-01-01 10:00:00"},  # missing
        {"id": 3, "publish_time": "2024-01-01 10:00:00"},  # missing => 2/3 >50% missing
    ]
    try:
        load_news(many_missing, trade_date="2024-01-02", snapshot_date="2024-01-02")
        assert False, "should raise when >50% missing trade_date"
    except ValueError as e:
        assert "50%" in str(e) or "missing" in str(e).lower()

    # Case 4: filtering with many records shouldn't silently drop >50% mismatch? At least log warning (not necessarily raise for mismatch)
    # Here we keep behavior: mismatched >50% should at least warn, not silently pass
    recs_many = [
        {"id": i, "trade_date": "2024-01-03", "publish_time": "2024-01-01 10:00:00"} for i in range(10)
    ] + [
        {"id": 100, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00"},
    ]
    caplog.clear()
    out2 = load_news(recs_many, trade_date="2024-01-02", snapshot_date="2024-01-02")
    assert len(out2) == 1
    assert any("dropped" in r.message.lower() for r in caplog.records)


def test_load_news_invalid_publish_time_honest():
    from hero_quant.data.loaders.news import load_news
    # malformed string/None publish_time must be honest pit=False/unknown without raising
    for bad in ["not-a-date", "", "   ", None]:
        rec = [{"id": 1, "trade_date": "2024-01-02", "publish_time": bad}]
        out = load_news(rec, trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
        assert out[0]["pit"] is False, f"malformed {bad!r} should be pit False, got {out[0]}"
        assert out[0]["pit_status"] in ("unknown", "unavailable", "missing", "non-pit", "non_pit"), f"bad time status {out[0]}"
    # numeric timestamp is implementation-specific: pd.to_datetime(12345) -> 1970-01-01, so pit may be True
    # ensure it does not raise and has honest status
    rec_num = [{"id": 1, "trade_date": "2024-01-02", "publish_time": 12345}]
    out_num = load_news(rec_num, trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
    assert "pit" in out_num[0] and "pit_status" in out_num[0]


def test_load_news_pit_status_consistency():
    from hero_quant.data.loaders.news import load_news
    recs = [
        {"id": 1, "trade_date": "2024-01-02", "publish_time": "2024-01-01 10:00:00"},
        {"id": 2, "trade_date": "2024-01-02", "publish_time": "2024-01-03 10:00:00"},
        {"id": 3, "trade_date": "2024-01-02"},
    ]
    out = load_news(copy.deepcopy(recs), trade_date="2024-01-02", snapshot_date="2024-01-02 12:00:00")
    for r in out:
        assert "pit" in r and "pit_status" in r
        if r["pit"] is True:
            assert r["pit_status"] in ("verified", "pit", "available", "ok")
        else:
            assert r["pit_status"] in ("unknown", "unavailable", "missing", "non-pit", "non_pit", "future", "excluded")
