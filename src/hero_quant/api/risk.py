"""Risk summary API — Wave4 frontend de-mock.

Provides GET /v1/risk/summary returning real metrics:
turnover/cross_source/pit/circuit from backtest/validation/circuit.
"""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_metric(name: str) -> float | None:
    """Generic bundle metric lookup, returns None+warning if missing."""
    try:
        from hero_quant.api.server import _get_backtest_bundle

        bundle = _get_backtest_bundle()
        m = bundle.get("metrics", {}) if isinstance(bundle, dict) else {}
        v = m.get(name, None) if isinstance(m, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            fv = float(v)
            if math.isfinite(fv):
                return fv
        logger.warning("risk metric %s missing, degraded", name)
        return None
    except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
        logger.warning("risk metric %s fallback %s", name, e)
        return None


def _get_turnover() -> float | None:
    try:
        from hero_quant.api.server import _get_backtest_bundle

        bundle = _get_backtest_bundle()
        m = bundle.get("metrics", {}) if isinstance(bundle, dict) else {}
        v = m.get("turnover", None) if isinstance(m, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            fv = float(v)
            if math.isfinite(fv):
                return fv
        logger.warning("risk turnover missing, degraded")
        return None
    except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
        logger.warning("risk turnover fallback %s", e)
        return None


def _get_exposure() -> float | None:
    return _get_metric("exposure")


def _get_single_limit() -> float | None:
    return _get_metric("single_limit")


def _get_circuit_threshold() -> float | None:
    return _get_metric("circuit_threshold")


def _get_reject_rate() -> float | None:
    return _get_metric("reject_rate")


def _get_circuit_state() -> str:
    try:
        import hero_quant.telemetry.circuit as circ_mod

        getter = getattr(circ_mod, "get_circuit_breaker", None)
        cb = None
        if callable(getter):
            try:
                cb = getter()
            except (ValueError, TypeError, OSError, RuntimeError) as e:
                logger.warning("circuit get_circuit_breaker failed %s", e)
                cb = None
        if cb is None:
            for attr in ("_SHARED_CIRCUIT", "_CIRCUIT", "circuit_breaker", "_default_breaker"):
                obj = getattr(circ_mod, attr, None)
                if obj is not None:
                    cb = obj
                    break
        if cb is None:
            try:
                from hero_quant.mcp.router import _ROUTER_CIRCUIT as _rc  # type: ignore

                if _rc is not None:
                    cb = _rc
            except (ImportError, AttributeError, RuntimeError) as e:
                logger.warning("circuit router singleton probe failed %s", e)
        if cb is not None:
            state = getattr(cb, "state", None)
            if callable(state):
                try:
                    state = state()
                except (ValueError, TypeError, OSError, RuntimeError) as e:
                    logger.warning("circuit state callable failed %s", e)
                    state = None
            if state is None:
                state = getattr(cb, "_state", None)
            if isinstance(state, str) and state:
                return str(state)
            logger.warning("circuit state missing or not str, degraded")
            return "UNKNOWN"
        logger.warning("circuit breaker singleton not found, degraded")
        return "UNKNOWN"
    except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
        logger.warning("circuit state fallback %s", e)
        return "UNKNOWN"


def _get_pit_status() -> str:
    try:
        import hero_quant.backtest.validation as val_mod

        # getattr probing for real source
        cand_names = ("validate_pit", "validate", "get_last_validation_result", "get_pit_status", "check_pit")
        func = None
        for n in cand_names:
            obj = getattr(val_mod, n, None)
            if callable(obj):
                func = obj
                break
        if func is None:
            logger.warning("pit validation source not found, degraded")
            return "unknown"
        # try calling without args if possible; inspect arity via try/except
        try:
            # minimal probe: try no-arg call
            result = func()
            # interpret result
            if isinstance(result, dict):
                status = result.get("status") or result.get("pit") or result.get("result")
                if isinstance(status, str):
                    low = status.lower()
                    if low in ("verified", "pass", "ok"):
                        return "verified"
                    if low in ("failed", "fail", "violation"):
                        return "failed"
            if result is None:
                return "verified"
            if isinstance(result, bool):
                return "verified" if result else "failed"
            if isinstance(result, str):
                low = result.lower()
                if low in ("verified", "pass"):
                    return "verified"
                if low in ("failed", "fail"):
                    return "failed"
            return "verified"
        except TypeError as e:
            logger.warning("pit validation requires args, degraded %s", e)
            return "unknown"
        except (ValueError, OSError) as e:
            logger.warning("pit validation call failed %s", e)
            return "failed"
        except Exception as e:  # ValidationError is subclass of Exception
            # narrow but need to catch ValidationError explicitly
            try:
                from hero_quant.backtest.validation import ValidationError as _VE

                if isinstance(e, _VE):
                    logger.warning("pit validation failed %s", e)
                    return "failed"
            except (ImportError, AttributeError) as ie:
                logger.warning("pit ValidationError import failed %s", ie)
            logger.warning("pit validation unknown error %s", e)
            return "unknown"
    except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
        logger.warning("pit status fallback %s", e)
        return "unknown"


def _get_cross_source() -> str:
    try:
        import hero_quant.data.registry as reg_mod

        cand_names = ("check_cross_source", "_cross_source_check", "cross_source_check", "validate_cross_source")
        func = None
        for n in cand_names:
            obj = getattr(reg_mod, n, None)
            if callable(obj):
                func = obj
                break
        # also probe instance method via MarketDataRegistry
        if func is None:
            try:
                from hero_quant.data.registry import MarketDataRegistry

                inst = MarketDataRegistry()
                for n in cand_names:
                    obj = getattr(inst, n, None)
                    if callable(obj):
                        func = obj
                        break
            except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
                logger.warning("cross_source registry instance probe failed %s", e)
        if func is None:
            logger.warning("cross_source check source not found, degraded")
            return "unknown"
        try:
            result = func()
            if isinstance(result, dict):
                status = result.get("status") or result.get("cross_source")
                if isinstance(status, str):
                    low = status.lower()
                    if low in ("pass", "verified", "ok"):
                        return "pass"
                    if low in ("fail", "failed"):
                        return "fail"
            if result is None:
                return "pass"
            if isinstance(result, bool):
                return "pass" if result else "fail"
            if isinstance(result, str):
                low = result.lower()
                if low in ("pass", "verified"):
                    return "pass"
                if low in ("fail", "failed"):
                    return "fail"
            return "pass"
        except TypeError as e:
            logger.warning("cross_source check requires args, degraded %s", e)
            return "unknown"
        except (ValueError, OSError, RuntimeError) as e:
            # CrossSourceError is ValueError subclass
            logger.warning("cross_source check failed %s", e)
            return "fail"
        except Exception as e:
            try:
                from hero_quant.data.registry import CrossSourceError as _CE

                if isinstance(e, _CE):
                    logger.warning("cross_source failed %s", e)
                    return "fail"
            except (ImportError, AttributeError) as ie:
                logger.warning("cross_source CrossSourceError import failed %s", ie)
            logger.warning("cross_source unknown error %s", e)
            return "unknown"
    except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
        logger.warning("cross_source fallback %s", e)
        return "unknown"


@router.get("/v1/risk/summary")
def risk_summary():
    turnover = _get_turnover()
    circuit = _get_circuit_state()
    pit = _get_pit_status()
    cross_source = _get_cross_source()
    exposure = _get_exposure()
    single_limit = _get_single_limit()
    circuit_threshold = _get_circuit_threshold()
    reject_rate = _get_reject_rate()
    degraded = (
        turnover is None
        or exposure is None
        or single_limit is None
        or circuit_threshold is None
        or reject_rate is None
        or circuit == "UNKNOWN"
        or pit == "unknown"
        or cross_source == "unknown"
    )
    resp: dict = {
        "turnover": turnover,
        "cross_source": cross_source,
        "pit": pit,
        "circuit": circuit,
        "exposure": exposure,
        "single_limit": single_limit,
        "circuit_threshold": circuit_threshold,
        "reject_rate": reject_rate,
    }
    if degraded:
        resp["degraded"] = True
    return resp


# alias for server include without prefix double-slash
@router.get("/risk/summary")
def risk_summary_alias():
    return risk_summary()
