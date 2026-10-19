"""O线 API 模糊测试复现脚本（fake LLM 模式）。

用法（服务须先起：HERO_ENV=development uvicorn hero_quant.api.server:app --port 8899）：
    python scripts/fuzz_repro.py [--base http://127.0.0.1:8899] [--only F1,F5]

每条用例输出一行：CASE_ID METHOD PATH -> STATUS (+备注)。
500 / 未捕获异常 / 超时会被汇总到末尾 FAILURES；退出码 1 表示有 500。
复现 curl 命令随行打印，可直接重放。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CASES = []
FAILURES = []
TOTAL = {"n": 0, "status": {}}


def req(method, path, params=None, headers=None, body=None, timeout=30):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()[:500]
    except urllib.error.HTTPError as e:
        try:
            b = e.read()[:500]
        except Exception:
            b = b""
        return e.code, b
    except Exception as e:  # 超时/连接重置等：记为异常类失败
        return -1, repr(e).encode()[:300]


def log(cid, method, path, params, status, note=""):
    TOTAL["n"] += 1
    TOTAL["status"][status] = TOTAL["status"].get(status, 0) + 1
    curl = "curl -s -o /dev/null -w '%%{http_code}' -X %s '%s'" % (method, BASE + path + ("?" + urllib.parse.urlencode(params or {}, doseq=True) if params else ""))
    flag = ""
    if status == 500 or status == -1:
        flag = "  <-- 500/EXC"
        FAILURES.append((cid, method, path, params, status, note, curl))
    print("%s %s %s -> %s %s %s" % (cid, method, path, status, note, flag), flush=True)
    if flag:
        print("    repro: %s" % curl, flush=True)


def req_retry(method, path, params=None, headers=None, body=None, timeout=30, retries=2):
    """429 自适应：ticket 10/min 配额耗尽时等窗口重置后重试。"""
    for attempt in range(retries + 1):
        st, b = req(method, path, params, headers, body, timeout)
        if st == 429 and attempt < retries:
            time.sleep(65)
            continue
        return st, b
    return st, b


def fresh_ticket(scope="query", user=None):
    p = {}
    if scope is not None:
        p["scope"] = scope
    if user is not None:
        p["user"] = user
    for attempt in (0, 1, 2, 3):
        st, body = req("POST", "/v1/query/ticket", params=p or None)
        if st == 200:
            try:
                return json.loads(body)["ticket"]
            except Exception:
                return None
        if st == 429 and attempt < 3:
            time.sleep(65)  # ticket 10/min 窗口重置后重试
            continue
        return None
    return None


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    BASE = a.base.rstrip("/")
    only = set(x for x in a.only.split(",") if x)

    def run(cid, fn):
        if only and cid not in only:
            return
        fn()

    LONG = "A" * 20000
    HUGE = "B" * 200000

    # ── F1: ticket 非法 scope 枚举 ──
    def f1():
        for i, sc in enumerate(["admin", "QUERY", "", "query ", "metrics;DROP", "null", LONG[:5000], "1", "0", "Query"]):
            st, b = req("POST", "/v1/query/ticket", params={"scope": sc})
            log("F1-%d" % i, "POST", "/v1/query/ticket", {"scope": sc[:40]}, st,
                "expect 400/200(scope default? no:invalid->400)" if st not in (200, 400, 429) else "")
    run("F1", f1)

    # ── F2: ticket user 超长/特殊 ──
    def f2():
        for i, u in enumerate(["", "x" * 5000, "<script>alert(1)</script>", "a'b\"c", "用户-ü-🎉", "../admin", "%2e%2e/admin"]):
            st, b = req_retry("POST", "/v1/query/ticket", params={"user": u})
            log("F2-%d" % i, "POST", "/v1/query/ticket", {"user": "len=%d" % len(u)}, st, "")
    run("F2", f2)

    # ── F3: ticket 方法混淆 + 类型混淆 ──
    def f3():
        st, _ = req("GET", "/v1/query/ticket")
        log("F3-0", "GET", "/v1/query/ticket", None, st, "expect 405")
        st, _ = req("PUT", "/v1/query/ticket")
        log("F3-1", "PUT", "/v1/query/ticket", None, st, "expect 405")
        st, _ = req("DELETE", "/v1/query/ticket")
        log("F3-2", "DELETE", "/v1/query/ticket", None, st, "expect 405")
        # scope 重复传参（数组语义）-> doseq
        url = BASE + "/v1/query/ticket?" + urllib.parse.urlencode([("scope", "query"), ("scope", "metrics")])
        r = urllib.request.Request(url, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                st = resp.status
        except urllib.error.HTTPError as e:
            st = e.code
        except Exception as e:
            st = -1
        TOTAL["n"] += 1
        TOTAL["status"][st] = TOTAL["status"].get(st, 0) + 1
        print("F3-3 POST /v1/query/ticket scope=array -> %s" % st, flush=True)
    run("F3", f3)

    # ── F4: 无票/伪票/复用票访问 query ──
    def f4():
        st, _ = req("GET", "/v1/query", params={"q": "hello"})
        log("F4-0", "GET", "/v1/query", {"q": "hello"}, st, "no ticket expect 401")
        st, _ = req("GET", "/v1/query", params={"q": "hello"}, headers={"X-Ticket": "forged-ticket-123"})
        log("F4-1", "GET", "/v1/query", {"q": "hello"}, st, "forged expect 401")
        st, _ = req("GET", "/v1/query", params={"q": "hello"}, headers={"X-Ticket": "A" * 20000})
        log("F4-2", "GET", "/v1/query", {"q": "len=20000 ticket"}, st, "long ticket expect 401")
        t = fresh_ticket()
        if t:
            st, _ = req("GET", "/v1/query", params={"q": "reuse-probe"}, headers={"X-Ticket": t})
            log("F4-3", "GET", "/v1/query", {"q": "reuse-probe"}, st, "first use")
            st, _ = req("GET", "/v1/query", params={"q": "reuse-probe"}, headers={"X-Ticket": t})
            log("F4-4", "GET", "/v1/query", {"q": "reuse-probe"}, st, "reuse expect 401")
        else:
            log("F4-3", "GET", "/v1/query", {"q": "skip"}, "SKIP", "no ticket (429 window)")
    run("F4", f4)

    # ── F5: query q 超长/特殊输入（持有效票） ──
    def f5():
        payloads = [
            ("empty", ""),
            ("long20k", LONG),
            ("huge200k", HUGE),
            ("unicode", "白酒股 600519.SH 怎么样？🎉\x00null"),
            ("sqli", "' OR '1'='1'; --"),
            ("path", "../../etc/passwd"),
            ("newlines", "a\nb\r\nc"),
        ]
        for i, (name, q) in enumerate(payloads):
            t = fresh_ticket()
            if not t:
                log("F5-%d" % i, "GET", "/v1/query", {"q": name}, "SKIP", "ticket 429 window")
                continue
            st, b = req("GET", "/v1/query", params={"q": q}, headers={"X-Ticket": t}, timeout=90)
            note = ""
            if st == 200:
                try:
                    j = json.loads(b)
                    note = "degraded=%s" % j.get("degraded")
                except Exception:
                    note = "non-json-200?"
            log("F5-%d" % i, "GET", "/v1/query", {"q": "%s(len=%d)" % (name, len(q))}, st, note)
            time.sleep(1)
    run("F5", f5)

    # ── F6: query 参数类型混淆 ──
    def f6():
        cases = [
            {"use_graph": "notabool"},
            {"use_graph": "2"},
            {"wall_time_budget": "abc"},
            {"wall_time_budget": "-5"},
            {"wall_time_budget": "1e9"},
            {"wall_time_budget": "NaN"},
            {"replay_path": "../../etc/passwd"},
            {"replay_path": "/etc/hostname"},
            {"trace_dir": "../../tmp-x"},
            {"trace_dir": "/etc"},
            {"q": ["a", "b"]},  # 数组语义
            {"unknown_param_xyz": "1"},
        ]
        for i, p in enumerate(cases):
            t = fresh_ticket()
            if not t:
                log("F6-%d" % i, "GET", "/v1/query", {"case": i}, "SKIP", "ticket 429 window")
                continue
            st, b = req("GET", "/v1/query", params=dict(p, q="probe"), headers={"X-Ticket": t}, timeout=90)
            log("F6-%d" % i, "GET", "/v1/query", {k: str(v)[:30] for k, v in p.items()}, st,
                "expect 200/400/422, never 500")
            time.sleep(1)
    run("F6", f6)

    # ── F7: stream 票据面 + 超长 q（只读首包即关） ──
    def f7():
        st, _ = req("GET", "/v1/query/stream", params={"q": "x"})
        log("F7-0", "GET", "/v1/query/stream", {"q": "x"}, st, "no ticket expect 403")
        t = fresh_ticket(scope="metrics")
        if t:
            st, _ = req("GET", "/v1/query/stream", params={"q": "x"}, headers={"X-Ticket": t}, timeout=30)
            log("F7-1", "GET", "/v1/query/stream", {"q": "x", "scope": "metrics"}, st, "wrong scope expect 403")
        t = fresh_ticket(scope="stream")
        if t:
            st, _ = req("GET", "/v1/query/stream", params={"q": LONG}, headers={"X-Ticket": t}, timeout=90)
            log("F7-2", "GET", "/v1/query/stream", {"q": "len=20000", "scope": "stream"}, st, "long q stream")
        else:
            log("F7-2", "GET", "/v1/query/stream", {"q": "skip"}, "SKIP", "ticket 429 window")
    run("F7", f7)

    # ── F8: trace/events offset 边界 ──
    def f8():
        for i, off in enumerate(["-1", "-999999", "abc", "1.5", "999999999999", "0x10", "", "0"]):
            st, _ = req("GET", "/v1/trace/events", params={"offset": off})
            log("F8-%d" % i, "GET", "/v1/trace/events", {"offset": off}, st, "neg expect 400")
        url = BASE + "/v1/trace/events?" + urllib.parse.urlencode([("offset", "0"), ("offset", "5")])
        r = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                st = resp.status
        except urllib.error.HTTPError as e:
            st = e.code
        except Exception:
            st = -1
        TOTAL["n"] += 1
        TOTAL["status"][st] = TOTAL["status"].get(st, 0) + 1
        print("F8-8 GET /v1/trace/events offset=array -> %s" % st, flush=True)
    run("F8", f8)

    # ── F9: backtest/risk/metrics 方法混淆 + 未知参数 ──
    def f9():
        for i, (m, p) in enumerate([
            ("POST", "/v1/backtest/metrics.json"),
            ("PUT", "/v1/backtest/metrics.json"),
            ("DELETE", "/v1/backtest/drawdowns.json"),
            ("POST", "/v1/trace/events"),
            ("POST", "/v1/risk/summary"),
            ("GET", "/v1/risk/summary"),
        ]):
            st, b = req(m, p, params={"zzz_unknown": LONG[:1000], "q": "x"})
            note = ""
            if p == "/v1/risk/summary" and st == 200:
                try:
                    j = json.loads(b)
                    note = "degraded=%s reason=%s" % (j.get("degraded"), (j.get("reason") or "")[:60])
                except Exception:
                    note = "non-json?"
            log("F9-%d" % i, m, p, {"zzz": "len=1000"}, st, note or ("expect 405" if m != "GET" else ""))
        st, b = req("GET", "/metrics")
        log("F9-6", "GET", "/metrics", None, st, "prometheus text expect 200")
    run("F9", f9)

    print("\n==== SUMMARY total=%d status=%s failures(500/exc)=%d ====" % (TOTAL["n"], TOTAL["status"], len(FAILURES)), flush=True)
    for f in FAILURES:
        print("FAIL %s %s %s %s -> %s %s\n  %s" % f, flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
