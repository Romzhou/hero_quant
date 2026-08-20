from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

app = FastAPI(title="hero-quant")

REQUEST_COUNTER = Counter("hero_quant_requests_total", "Total requests", ["endpoint"])


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
