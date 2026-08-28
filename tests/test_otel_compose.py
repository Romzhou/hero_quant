"""W2-F TDD: OTel compose 收口配置结构验证。"""
from __future__ import annotations

import pathlib
import re
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
CFG = ROOT / "deploy/otel-collector-config.yaml"


def _load_compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _load_collector():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def _hero_env_dict(compose) -> dict[str, str]:
    services = compose.get("services") or {}
    assert "hero-quant" in services, "services.hero-quant missing"
    env = services["hero-quant"].get("environment", {})
    if env is None:
        return {}
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items() if v is not None}
    assert isinstance(env, list), f"environment must be dict or list, got {type(env)}"
    out: dict[str, str] = {}
    for item in env:
        if isinstance(item, str):
            assert "=" in item, f"malformed env entry without '=': {item!r}"
            k, v = item.split("=", 1)
            out[k] = v
        elif isinstance(item, dict):
            out.update({str(k): str(v) for k, v in item.items()})
        else:
            raise AssertionError(f"unsupported env entry type {type(item)}: {item!r}")
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
    # grpc 4317 — anchor to :<port> boundary
    assert "grpc" in protocols, "otlp.protocols.grpc missing"
    grpc_ep = str((protocols["grpc"] or {}).get("endpoint", ""))
    assert re.search(r":4317\b", grpc_ep), f"grpc endpoint must contain :4317, got {grpc_ep!r}"
    # http 4318
    assert "http" in protocols, "otlp.protocols.http missing"
    http_ep = str((protocols["http"] or {}).get("endpoint", ""))
    assert re.search(r":4318\b", http_ep), f"http endpoint must contain :4318, got {http_ep!r}"


def test_otel_collector_config_has_debug_exporter():
    data = _load_collector()
    exporters = data.get("exporters") or {}
    assert "debug" in exporters, "exporters.debug missing"
    service = data.get("service") or {}
    pipelines = service.get("pipelines") or {}
    assert pipelines, "service.pipelines missing"
    assert "logs" in pipelines, "service.pipelines.logs missing"
    logs = pipelines["logs"] or {}
    assert isinstance(logs, dict), "pipelines.logs must be a mapping"
    recvs = logs.get("receivers") or []
    exps = logs.get("exporters") or []
    assert isinstance(recvs, list) and isinstance(exps, list), "receivers/exporters must be lists"
    assert "otlp" in recvs, "logs pipeline must include otlp receiver"
    assert "debug" in exps, "logs pipeline must include debug exporter"


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
    vol_text = " ".join(str(v) for v in volumes)
    cmd_text = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else (cmd or "")
    has_mount = "otel-collector-config.yaml" in vol_text or "otel-collector-config.yaml" in cmd_text
    assert has_mount, "otel-collector must mount deploy/otel-collector-config.yaml in volumes or command"


def test_compose_hero_has_otel_env():
    data = _load_compose()
    env = _hero_env_dict(data)
    assert env.get("HERO_OTEL_MODE") == "shared", f"HERO_OTEL_MODE must be shared, got {env.get('HERO_OTEL_MODE')!r}"
    assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://otel-collector:4318/v1/logs", (
        f"OTEL_EXPORTER_OTLP_ENDPOINT must be http://otel-collector:4318/v1/logs, got {env.get('OTEL_EXPORTER_OTLP_ENDPOINT')!r}"
    )


def test_compose_yaml_valid():
    compose_data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    cfg_data = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    assert isinstance(compose_data, dict) and compose_data, "docker-compose.yml must parse to non-empty dict"
    assert isinstance(cfg_data, dict) and cfg_data, "otel-collector-config.yaml must parse to non-empty dict"


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
    hero = services["hero-quant"]
    depends = hero.get("depends_on")
    # normalize list vs dict form — only block if otel-collector is hard-depended with service_healthy
    if isinstance(depends, list):
        assert "otel-collector" not in depends, "hero-quant must not hard-depend on otel-collector"
    elif isinstance(depends, dict):
        dep = depends.get("otel-collector")
        if dep is not None:
            if isinstance(dep, dict):
                assert dep.get("condition") not in ("service_healthy", "service_completed_successfully"), \
                    "otel-collector must be optional (condition must not be service_healthy)"
                assert dep.get("required") is not True
            else:
                assert False, "otel-collector dependency must be optional"
    # healthcheck on collector itself is allowed; only blocking depends_on is forbidden
    # (previous raw-file wget/curl scan was too broad — scoped to hero depends_on is sufficient)
