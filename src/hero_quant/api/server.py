from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
import structlog
import structlog.contextvars
import uuid
import logging
import os
import pathlib

# -- structlog JSON backbone (OTel placeholder) --
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

REQUEST_COUNTER = Counter("hero_quant_requests_total", "Total requests", ["endpoint"])

# -- X-Request-ID middleware + OTel export placeholder --
@app.middleware("http")
async def add_request_id_and_otel(request: Request, call_next):
    # Propagate or generate X-Request-ID
    request_id = request.headers.get("X-Request-ID")
    if not request_id:
        request_id = str(uuid.uuid4())
    # Bind to contextvars for downstream structured logs
    structlog.contextvars.bind_contextvars(request_id=request_id)
    # OTel export placeholder: read mode lazily to avoid import cycle at module load
    try:
        from hero_quant.telemetry.otel import get_otel_mode

        otel_mode = get_otel_mode()
        # No-op export; placeholder keeps collector wiring point
        if otel_mode != "disabled":
            logger.debug("otel.export.placeholder", mode=otel_mode, path=request.url.path)
    except Exception:
        # Telemetry must not break request path
        pass

    logger.info("request.start", method=request.method, path=request.url.path, request_id=request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info("request.end", status_code=response.status_code, request_id=request_id)
    structlog.contextvars.clear_contextvars()
    return response


@app.get("/live")
def live():
    REQUEST_COUNTER.labels(endpoint="/live").inc()
    return {"status": "ok"}


@app.get("/ready")
def ready():
    REQUEST_COUNTER.labels(endpoint="/ready").inc()
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/query")
def query(q: str = ""):
    """SSE placeholder - returns JSON now, will stream in Task16."""
    # Minimal placeholder to keep route registered
    REQUEST_COUNTER.labels(endpoint="/v1/query").inc()

    # If client expects SSE, we could return StreamingResponse but test only checks /live /metrics
    # Provide simple JSON response
    return {"query": q, "status": "ok"}


@app.get("/v1/query/stream")
def query_stream(q: str = ""):
    """SSE stream placeholder."""
    REQUEST_COUNTER.labels(endpoint="/v1/query/stream").inc()

    def event_generator():
        yield f"data: {{\"query\": \"{q}\", \"status\": \"ok\"}}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# -- Static frontend serving (Docker single-machine deployment) --
# Mount frontend/dist at "/" if present. Must be last so /live /ready /metrics
# /v1/query remain reachable and StaticFiles does not shadow API routes.
# Supports both Docker (/app/frontend/dist) and local repo layout.
def _resolve_frontend_dist() -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    # Explicit env override (e.g. FRONTEND_DIST=/app/frontend/dist)
    env_path = os.environ.get("FRONTEND_DIST")
    if env_path:
        candidates.append(pathlib.Path(env_path))
    # Docker absolute path
    candidates.append(pathlib.Path("/app/frontend/dist"))
    # Repo-root relative: file is at src/hero_quant/api/server.py -> repo root is 4 parents up
    try:
        repo_root_dist = pathlib.Path(__file__).resolve().parents[3] / "frontend" / "dist"
        candidates.append(repo_root_dist)
    except Exception:
        pass
    # CWD relative (covers `uvicorn` launched from repo root)
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
    app.mount("/", StaticFiles(directory=str(_dist_path), html=True), name="frontend")
    logger.info("frontend.mounted", dist_path=str(_dist_path))
else:
    logger.info("frontend.not_found", msg="frontend/dist not found, serving API only")
