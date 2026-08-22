"""W2-F TDD: OTel compose 收口配置结构验证。"""
from __future__ import annotations

import pathlib
import yaml


COMPOSE = pathlib.Path("docker-compose.yml")
CFG = pathlib.Path("deploy/otel-collector-config.yaml")


def _load_compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _load_collector():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def _hero_env_dict(compose) -> dict[str, str]:
    env = compose["services"]["hero-quant"].get("environment", {})
    # compose supports dict or list form; normalize to dict
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    # list like ["KEY=VAL"]
    out: dict[str, str] = {}
    for item in env or []:
        if isinstance(item, str) and "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
        elif isinstance(item, dict):
            out.update({str(k): str(v) for k, v in item.items()})
    return out


def test_otel_collector_config_exists():
    assert CFG.exists(), "deploy/otel-collector-config.yaml must exist"
    data = _load_collector()
    assert isinstance(data, dict)


def test_otel_collector_config_has_otlp_receivers():
    data = _load_collector()
    receivers = data.get("receivers") or {}
    assert "otlp" in receivers, "receivers.otlp missing"
    otlp = receivers["otlp"] or {}
    protocols = otlp.get("protocols") or {}
    # grpc 4317
    assert "grpc" in protocols, "otlp.protocols.grpc missing"
    grpc_ep = str((protocols["grpc"] or {}).get("endpoint", ""))
    assert "4317" in grpc_ep, f"grpc endpoint must contain 4317, got {grpc_ep!r}"
    # http 4318
    assert "http" in protocols, "otlp.protocols.http missing"
    http_ep = str((protocols["http"] or {}).get("endpoint", ""))
    assert "4318" in http_ep, f"http endpoint must contain 4318, got {http_ep!r}"


def test_otel_collector_config_has_debug_exporter():
    data = _load_collector()
    exporters = data.get("exporters") or {}
    assert "debug" in exporters, "exporters.debug missing"
    # service pipelines must wire otlp -> debug
    service = data.get("service") or {}
    pipelines = service.get("pipelines") or {}
    assert pipelines, "service.pipelines missing"
    # at least logs pipeline uses otlp receiver and debug exporter
    found = False
    for name, pipe in pipelines.items():
        recvs = pipe.get("receivers") or []
        exps = pipe.get("exporters") or []
        if "otlp" in recvs and "debug" in exps:
            found = True
            break
    assert found, "no pipeline with receivers [otlp] and exporters [debug]"


def test_compose_has_otel_collector_service():
    data = _load_compose()
    services = data.get("services") or {}
    assert "otel-collector" in services
    assert "hero-quant" in services
    # volumes/networks should still exist (keep existing behavior)
    assert "pgdata" in (data.get("volumes") or {})
    assert "hero-runs" in (data.get("volumes") or {})
    oc = services["otel-collector"]
    # image must not be upgraded arbitrarily; keep otel collector image
    assert oc.get("image", "").startswith("otel/opentelemetry-collector")


def test_compose_otel_collector_mounts_config():
    data = _load_compose()
    oc = data["services"]["otel-collector"]
    volumes = oc.get("volumes") or []
    cmd = oc.get("command")
    # either volume mount or command referencing deploy config
    vol_text = " ".join(str(v) for v in volumes)
    cmd_text = ""
    if isinstance(cmd, list):
        cmd_text = " ".join(str(c) for c in cmd)
    elif isinstance(cmd, str):
        cmd_text = cmd
    has_mount = "otel-collector-config.yaml" in vol_text or "otel-collector-config.yaml" in cmd_text
    # also allow config inline via volumes referencing deploy/
    if not has_mount:
        # check raw file text fallback
        raw = COMPOSE.read_text(encoding="utf-8")
        has_mount = "otel-collector-config.yaml" in raw
    assert has_mount, "otel-collector must mount deploy/otel-collector-config.yaml"


def test_compose_hero_has_otel_env():
    data = _load_compose()
    env = _hero_env_dict(data)
    assert env.get("HERO_OTEL_MODE") == "shared", f"HERO_OTEL_MODE must be shared, got {env.get('HERO_OTEL_MODE')!r}"
    assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://otel-collector:4318/v1/logs", (
        f"OTEL_EXPORTER_OTLP_ENDPOINT must be http://otel-collector:4318/v1/logs, got {env.get('OTEL_EXPORTER_OTLP_ENDPOINT')!r}"
    )


def test_compose_yaml_valid():
    # both yamls must be parseable
    yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    yaml.safe_load(CFG.read_text(encoding="utf-8"))


def test_compose_does_not_use_external_otel():
    data = _load_compose()
    env = _hero_env_dict(data)
    # must be internal collector, not external SaaS
    ep = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert "otel-collector" in ep, "OTLP endpoint must point to internal otel-collector service"
    # compose should still have postgres/temporal/hero-quant services
    services = data.get("services") or {}
    for svc in ("postgres", "temporal", "hero-quant", "otel-collector"):
        assert svc in services, f"service {svc} missing"


def test_compose_otel_does_not_block_business_on_optional_collector():
    data = _load_compose()
    services = data["services"]
    collector = services["otel-collector"]
    hero_depends_on = services["hero-quant"].get("depends_on") or {}

    assert "healthcheck" not in collector
    assert "otel-collector" not in hero_depends_on
    raw = COMPOSE.read_text(encoding="utf-8").lower()
    assert "wget" not in raw
    assert "curl" not in raw
