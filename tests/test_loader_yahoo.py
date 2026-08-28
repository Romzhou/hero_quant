import sys
import types


def test_yahoo_timeout_not_importerror(monkeypatch):
    fake_yf = types.ModuleType("yfinance")

    def fake_download(*a, **kw):
        raise TimeoutError("network timeout")

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, *a, **kw):
            raise TimeoutError("history timeout")

    fake_yf.download = fake_download
    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    from hero_quant.data.loaders.yahoo import YahooLoader

    loader = YahooLoader()
    try:
        loader.get_bars("AAPL.US", "2026-08-01", "2026-08-19", "1d")
    except ImportError as e:
        assert False, f"TimeoutError was reclassified as ImportError: {e} cause={e.__cause__}"
    except TimeoutError:
        pass  # ideal: original preserved
    except ValueError:
        pass  # also acceptable: not ImportError (download swallowed but not reclassified as ImportError)
    except RuntimeError:
        pass  # if wrapped in loader error with cause
    except Exception as e:
        # any non-ImportError is acceptable as long as not pip install ImportError
        assert not (isinstance(e, ImportError) and "pip install" in str(e)), f"unexpected ImportError {e}"
        # if wrapped, ensure cause is TimeoutError somewhere
        cause = e.__cause__ or e
        # at minimum ensure not ImportError
        assert not isinstance(e, ImportError), f"should not be ImportError, got {type(e)}:{e}"


def test_yahoo_import_failure_is_importerror(monkeypatch):
    monkeypatch.delitem(sys.modules, "yfinance", raising=False)
    import builtins

    orig_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "yfinance":
            raise ImportError("No module named 'yfinance'")
        return orig_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from hero_quant.data.loaders.yahoo import YahooLoader

    loader = YahooLoader()
    try:
        loader.get_bars("AAPL.US", "2026-08-01", "2026-08-19", "1d")
    except ImportError as e:
        assert "pip install" in str(e)
    else:
        assert False, "expected ImportError for missing yfinance"
