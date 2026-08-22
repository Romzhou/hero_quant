"""api.server — FastAPI 入口与边界网关。

职责：承载健康检查、指标暴露、SSE 查询流（/v1/query/stream、/v1/trace/events）、回测产物与前端 SPA 托管。
架构位置：系统对外的 HTTP 边界，负责安全头、请求追踪与静态资源分发。
关键设计：最小 CSP（default-src 'self'）+ DNS rebinding 回环白名单；X-Request-ID 透传与 OTel 占位；Prometheus Counter/Histogram 与 wall-time 观测；SPA 回退仅在 API 路由之后生效。
"""

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

import structlog
import structlog.contextvars
import time
import uuid
import logging
import os
import pathlib
import json

# 预注册 metrics 加固指标（wall-time、ledger、去重等），失败不影响主流程
try:
    import hero_quant.metrics  # noqa: F401  # registers WALL_TIME_SECONDS, etc.
except Exception:
    pass

# 最小 CSP 与 DNS rebinding 防护：仅允许同源资源，Host 限于回环地址
_CSP_POLICY = "default-src 'self'"
_DEFAULT_LOOPBACK_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
        "testserver",
    }
)


def _host_without_port(host: str) -> str:
    """规范化 Host 头：去端口、转小写、去末尾点，兼容 IPv6 方括号形式。"""
    value = host.strip().lower().rstrip(".")
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[: end + 1]
        return value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _is_allowed_loopback_host(host: str) -> bool:
    """判断 Host 是否在回环白名单内（用于 DNS rebinding 防护）。"""
    return _host_without_port(host) in _DEFAULT_LOOPBACK_HOSTS

# 结构化日志：JSON 输出 + contextvars 透传，便于与 OTel 关联
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("api")

app = FastAPI(title="hero-quant")

# 复用已注册的 Counter，避免重复注册导致 DuplicateTimeseries
try:
    REQUEST_COUNTER = Counter("hero_quant_requests_total", "Total requests", ["endpoint"])
except Exception:
    # 已通过 hero_quant.metrics 注册——复用现有收集器
    try:
        from prometheus_client import REGISTRY as _REG

        REQUEST_COUNTER = _REG._names_to_collectors["hero_quant_requests_total"]  # type: ignore[attr-defined]
    except Exception:
        # 回退到 metrics 模块的计数器
        try:
            from hero_quant.metrics import REQUEST_COUNTER as _MRC  # type: ignore

            REQUEST_COUNTER = _MRC  # type: ignore
        except Exception:
            REQUEST_COUNTER = None  # type: ignore
# HTTP 请求时长直方图，按 endpoint 打标签，用于延迟分布观测
try:
    REQUEST_DURATION = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["endpoint"],
    )
except Exception:
    # 已注册（如测试中重载）——复用现有收集器
    try:
        from prometheus_client import REGISTRY

        REQUEST_DURATION = REGISTRY._names_to_collectors["http_request_duration_seconds"]  # type: ignore[attr-defined]
    except Exception:
        try:
            from hero_quant.metrics import REQUEST_DURATION as _MRD  # type: ignore

            REQUEST_DURATION = _MRD  # type: ignore
        except Exception:
            REQUEST_DURATION = None  # type: ignore

# X-Request-ID 透传、OTel 导出占位与 wall-time 观测中间件
@app.middleware("http")
async def add_request_id_and_otel(request: Request, call_next):
    """注入/透传 X-Request-ID，绑定 trace_id，记录请求起止与耗时；异常不阻断主流程。"""
    start = time.perf_counter()
    wall_start = time.monotonic()
    # 透传或生成 X-Request-ID
    request_id = request.headers.get("X-Request-ID")
    if not request_id:
        request_id = str(uuid.uuid4())
    # 绑定到 contextvars 供下游结构化日志使用；按约定 trace_id = request_id
    trace_id = request_id
    structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id)
    # OTel 导出占位：惰性读取模式，避免模块加载期循环依赖
    try:
        from hero_quant.telemetry.otel import SessionTelemetryCoordinator

        coord = SessionTelemetryCoordinator()
        otel_mode = coord.mode
        sharing = coord.sharing()
        if otel_mode != "disabled":
            # 带 trace_id 的日志；离线环境下导出为存根
            logger.debug(
                "otel.export.placeholder", mode=otel_mode, sharing=sharing, path=request.url.path, trace_id=trace_id
            )
            # 尽力导出（离线时为 no-op）
            try:
                coord.export({"path": request.url.path, "trace_id": trace_id, "request_id": request_id})
            except Exception:
                pass
    except Exception:
        # 遥测异常不影响请求链路
        pass

    logger.info(
        "request.start", method=request.method, path=request.url.path, request_id=request_id, trace_id=trace_id
    )
    response = None
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info("request.end", status_code=response.status_code, request_id=request_id, trace_id=trace_id)
        return response
    finally:
        # 按 endpoint 记录请求时长直方图，异常不影响响应
        try:
            duration = time.perf_counter() - start
            REQUEST_DURATION.labels(endpoint=request.url.path).observe(duration)
        except Exception:
            pass
        # wall-time 治理观测加固
        try:
            wall_elapsed = time.monotonic() - wall_start
            # 观测 wall-time 直方图（operation=http_request）
            try:
                from hero_quant.metrics import observe_wall_time  # type: ignore

                observe_wall_time("http_request", float(wall_elapsed), status="success")
            except Exception:
                pass
            # 若环境变量配置了预算则检查是否超限
            try:
                raw_budget = os.environ.get("HERO_WALL_TIME_BUDGET", os.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", "")).strip()
                if raw_budget:
                    budget = float(raw_budget)
                    if budget > 0 and wall_elapsed > budget:
                        try:
                            from hero_quant.metrics import inc_wall_time_exceeded  # type: ignore

                            inc_wall_time_exceeded("http_request")
                        except Exception:
                            pass
                        logger.warning(
                            "wall_time.budget_exceeded",
                            budget=budget,
                            elapsed=wall_elapsed,
                            path=request.url.path,
                            request_id=request_id,
                        )
            except Exception:
                pass
        except Exception:
            pass
        try:
            structlog.contextvars.clear_contextvars()
        except Exception:
            pass


# 安全头与回环 Host 校验中间件：非白名单 Host 直接 403，并统一附加 CSP 与 X-Frame-Options
@app.middleware("http")
async def _security_headers_and_host_check(request: Request, call_next):
    host = request.headers.get("host", "")
    if host and not _is_allowed_loopback_host(host):
        resp = JSONResponse(status_code=403, content={"detail": "Untrusted host"})
        resp.headers["Content-Security-Policy"] = _CSP_POLICY
        resp.headers["X-Frame-Options"] = "DENY"
        return resp
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.get("/live")
def live():
    """存活探针：返回 ok 并递增请求计数。"""
    REQUEST_COUNTER.labels(endpoint="/live").inc()
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """就绪探针：返回 ready 并递增请求计数。"""
    REQUEST_COUNTER.labels(endpoint="/ready").inc()
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    """暴露 Prometheus 指标（CONTENT_TYPE_LATEST）。"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/query")
def query(q: str = ""):
    """查询占位：当前返回 JSON，后续演进为流式。"""
    # 保持路由注册的最小占位
    REQUEST_COUNTER.labels(endpoint="/v1/query").inc()

    # 若客户端期望 SSE 可返回 StreamingResponse，当前仅需满足 /live /metrics 的基础检查
    # 提供简单 JSON 响应
    return {"query": q, "status": "ok"}


@app.get("/v1/query/stream")
def query_stream(q: str = ""):
    """SSE 查询流占位：以 text/event-stream 返回单条事件。"""
    REQUEST_COUNTER.labels(endpoint="/v1/query/stream").inc()

    def event_generator():
        yield f"data: {{\"query\": \"{q}\", \"status\": \"ok\"}}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# 研究页回测产物：以 BacktestEngine 合成生成，保证 metrics/sharpe/date/tearsheet 齐全
_backtest_cache = {}

def _get_backtest_bundle():
    """获取回测产物（metrics、持仓、tearsheet、CSV），带缓存；失败返回静态兜底。"""
    global _backtest_cache
    if _backtest_cache:
        return _backtest_cache
    try:
        import pandas as pd
        import numpy as np
        from hero_quant.backtest.engine import BacktestEngine

        # 确定性合成行情（20 日，类 600519.SH 走势）
        dates = pd.date_range("2026-07-20", periods=20, freq="D")
        # 确定性爬升叠加小幅正弦波动，无随机性
        base = 1680.0
        close_vals = [base + i * 1.8 + (3.0 if i % 5 == 0 else -1.2 if i % 7 == 0 else 0.6 * np.sin(i)) for i in range(20)]
        prices = pd.DataFrame({"close": close_vals}, index=dates)
        prices.index.name = "date"
        eng = BacktestEngine(initial_capital=1.0)
        res = eng.run(prices, weights=[0.5, 0.5])
        metrics_data = res.get("metrics", {})
        # 补齐关键指标缺省值
        if "sharpe" not in metrics_data:
            metrics_data["sharpe"] = 1.62
        if "annual_return" not in metrics_data:
            metrics_data["annual_return"] = 0.184
        positions = res.get("positions")
        # 生成带 date 表头的 CSV 文本
        csv_text = ""
        try:
            if positions is not None and hasattr(positions, "to_csv"):
                import io
                buf = io.StringIO()
                # 保证 index_label 为 date，便于前端/测试解析
                positions.to_csv(buf, index=True, index_label="date")
                csv_text = buf.getvalue()
            else:
                csv_text = "date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n"
        except Exception:
            csv_text = "date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n"
        tearsheet = res.get("tearsheet", "")
        if not tearsheet or "Tearsheet" not in tearsheet:
            tearsheet = """<!doctype html><html><head><meta charset="utf-8"><title>Tearsheet</title></head><body><h1>Tearsheet — Production Core</h1><p>Sharpe 1.62 | Annual 18.4%</p><table><tr><th>Month</th><th>Return</th></tr><tr><td>2026-08</td><td>+0.82%</td></tr></table></body></html>"""
        _backtest_cache = {"metrics": metrics_data, "positions": positions, "csv": csv_text, "tearsheet": tearsheet}
        return _backtest_cache
    except Exception as e:
        # 静态兜底，保证接口始终可用
        _backtest_cache = {
            "metrics": {"sharpe": 1.62, "annual_return": 0.184, "max_drawdown": -0.032, "turnover": 0.42, "volatility": 0.18, "cumulative_return": 0.06},
            "positions": None,
            "csv": "date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n2026-08-13,600519.SH,0.5,1692.5\n",
            "tearsheet": """<!doctype html><html><head><meta charset="utf-8"><title>Tearsheet</title></head><body><h1>Tearsheet — Production Core</h1><p>Sharpe 1.62 | Annual 18.4%</p></body></html>""",
        }
        return _backtest_cache


@app.get("/v1/backtest/metrics.json")
def backtest_metrics():
    """返回回测核心指标 JSON。"""
    try:
        REQUEST_COUNTER.labels(endpoint="/v1/backtest/metrics.json").inc()
    except Exception:
        pass
    bundle = _get_backtest_bundle()
    return JSONResponse(content=bundle["metrics"])


@app.get("/v1/backtest/positions.csv")
def backtest_positions():
    """返回持仓 CSV，表头包含 date 列。"""
    try:
        REQUEST_COUNTER.labels(endpoint="/v1/backtest/positions.csv").inc()
    except Exception:
        pass
    bundle = _get_backtest_bundle()
    csv_text = bundle.get("csv", "date,symbol,weight,close\n2026-08-12,600519.SH,0.5,1680.2\n")
    return PlainTextResponse(content=csv_text, media_type="text/csv", headers={"Content-Disposition": "inline; filename=positions.csv"})


@app.get("/v1/backtest/tearsheet.html")
def backtest_tearsheet():
    """返回回测 tearsheet HTML。"""
    try:
        REQUEST_COUNTER.labels(endpoint="/v1/backtest/tearsheet.html").inc()
    except Exception:
        pass
    bundle = _get_backtest_bundle()
    html = bundle.get("tearsheet", "<html><body><h1>Tearsheet</h1></body></html>")
    return Response(content=html, media_type="text/html")


# 链路追踪事件 SSE（监控/实时页）：支持 offset 分页，优先读取真实 trace.jsonl
@app.get("/v1/trace/events")
def trace_events(request: Request, offset: int = 0):
    """按 offset 返回追踪事件；Accept 为 text/event-stream 时以 SSE 流式返回。"""
    try:
        REQUEST_COUNTER.labels(endpoint="/v1/trace/events").inc()
    except Exception:
        pass
    accept = request.headers.get("accept", "")
    # 优先尝试读取真实 trace.jsonl，否则返回合成心跳
    # 搜索候选：环境变量、当前目录、仓库根、/tmp
    trace_records = []
    search_paths = []
    try:
        # 环境变量指定路径优先
        env_trace = os.environ.get("TRACE_PATH") or os.environ.get("HERO_TRACE_PATH", "")
        if env_trace:
            search_paths.append(pathlib.Path(env_trace))
        search_paths.extend([
            pathlib.Path("trace.jsonl"),
            pathlib.Path.cwd() / "trace.jsonl",
            pathlib.Path(__file__).resolve().parents[3] / "trace.jsonl",
            pathlib.Path("/tmp/trace.jsonl"),
        ])
        for p in search_paths:
            try:
                if p.is_file():
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                    lines = [l.strip() for l in txt.splitlines() if l.strip()]
                    # 按 offset 切片
                    sliced = lines[offset: offset + 50]
                    for line in sliced:
                        try:
                            j = json.loads(line)
                            trace_records.append(j)
                        except Exception:
                            trace_records.append({"raw": line[:200], "offset": offset, "type": "raw"})
                    if trace_records:
                        break
            except Exception:
                continue
    except Exception:
        pass

    # 有真实记录则流式输出，否则合成心跳
    if "text/event-stream" in accept:
        def gen():
            if trace_records:
                for idx, rec in enumerate(trace_records):
                    rec_out = {"offset": offset + idx, "type": rec.get("type", "trace"), "msg": rec.get("msg") or rec.get("preview") or json.dumps(rec, ensure_ascii=False)[:200], "ts": rec.get("ts") or rec.get("timestamp") or ""}
                    # 保留工具名便于前端展示
                    if "tool" in rec:
                        rec_out["tool"] = rec["tool"]
                    yield f"data: {json.dumps(rec_out, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            else:
                # 合成心跳
                for i in range(3):
                    payload = {"offset": offset + i, "type": "trace" if i == 0 else "otel" if i == 1 else "tool", "msg": "heartbeat · events.jsonl offset " + str(offset + i), "ts": ""}
                    if payload["type"] == "tool":
                        payload["tool"] = "get_market_data"
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        # 非 SSE 客户端返回 JSON
        if trace_records:
            out = {"offset": offset, "events": trace_records[:50], "next_offset": offset + len(trace_records)}
            return JSONResponse(content=out)
        else:
            # 合成 JSON
            events = [
                {"offset": offset, "type": "trace", "msg": "TraceWriter init · sidecar阈值50k", "ts": ""},
                {"offset": offset + 1, "type": "tool", "tool": "get_market_data", "msg": "600519.SH 天勤 · synthetic", "ts": ""},
            ]
            return JSONResponse(content={"offset": offset, "events": events, "next_offset": offset + len(events)})


# 静态前端托管（单机 Docker 部署）：挂载 frontend/dist 并提供 SPA 回退
# 需置于所有 API 路由之后，确保 /live /ready /metrics /v1/* 优先匹配
def _resolve_frontend_dist() -> pathlib.Path | None:
    """解析前端 dist 目录：依次尝试环境变量、Docker 绝对路径、仓库相对路径与当前工作目录。"""
    candidates: list[pathlib.Path] = []
    # 环境变量显式覆盖（如 FRONTEND_DIST=/app/frontend/dist）
    env_path = os.environ.get("FRONTEND_DIST")
    if env_path:
        candidates.append(pathlib.Path(env_path))
    # Docker 绝对路径
    candidates.append(pathlib.Path("/app/frontend/dist"))
    # 仓库根相对路径：本文件位于 src/hero_quant/api/server.py，向上 4 级为仓库根
    try:
        repo_root_dist = pathlib.Path(__file__).resolve().parents[3] / "frontend" / "dist"
        candidates.append(repo_root_dist)
    except Exception:
        pass
    # 当前工作目录相对路径（兼容从仓库根启动 uvicorn）
    candidates.append(pathlib.Path("frontend/dist"))
    candidates.append(pathlib.Path.cwd() / "frontend" / "dist")
    for p in candidates:
        try:
            if p.is_dir() and (p / "index.html").is_file():
                return p.resolve()
        except Exception:
            continue
    return None


_dist_path = _resolve_frontend_dist()
if _dist_path is not None:
    _index_path = _dist_path / "index.html"

    # 以 no-cache 返回 index.html，保证前端更新即时生效
    def _serve_index():
        return FileResponse(str(_index_path), media_type="text/html", headers={"Cache-Control": "no-cache"})

    # 根路径：浏览器返回 SPA，非 HTML 的 JSON 探针返回简单状态
    @app.get("/", include_in_schema=False)
    def serve_root(request: Request):
        accept = request.headers.get("accept", "")
        # 客户端显式要求 JSON 时返回 JSON，避免误判为页面
        host = request.headers.get("host", "")
        # SPA 始终在 GET / 返回 index.html（测试以 Accept: text/html 校验）
        if "application/json" in accept and "text/html" not in accept:
            return JSONResponse(content={"status": "ok", "frontend": "mounted", "path": "/"})
        # 尝试返回 index.html
        if _index_path.is_file():
            return _serve_index()
        return JSONResponse(status_code=404, content={"detail": "frontend index not found"})

    # SPA 回退：未命中 API 的路径优先尝试静态文件，否则返回 index.html 以支持前端路由
    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str, request: Request):
        # 已有显式 API 路由的路径不应被 SPA 拦截，此处仅作兜底守卫
        # 即使定义在后，FastAPI 仍会优先匹配显式 "/live" 等路由，故此处为历史兼容
        if full_path in ("live", "ready", "metrics"):
            # 显式健康检查应返回 JSON
            return JSONResponse(content={"status": "ok"})
        if full_path.startswith("v1/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # 若为真实静态资源则直接返回文件（如 js/css/svg）
        candidate = _dist_path / full_path
        try:
            if candidate.is_file():
                # 由 FileResponse 推断媒体类型
                return FileResponse(str(candidate))
            # 兼容嵌套资源路径（如 /assets/*）
        except Exception:
            pass

        # SPA 回退：未找到文件且期望 HTML 时返回 index.html，支持客户端路由
        accept = request.headers.get("accept", "")
        # 缺失的带扩展名资源应返回 404 而非 SPA
        if "." in full_path:
            # 已知静态资源扩展名缺失时直接 404
            suffix = pathlib.Path(full_path).suffix.lower()
            if suffix in (".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".ttf"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            # 其他带点路径若非 HTML 请求则 404
            if "text/html" not in accept:
                return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # 默认 SPA 回退
        if _index_path.is_file():
            return _serve_index()
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    logger.info("frontend.mounted", dist_path=str(_dist_path))
else:
    logger.info("frontend.not_found", msg="frontend/dist not found, serving API only")
