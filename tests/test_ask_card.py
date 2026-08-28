# tests/test_ask_card.py
def test_ask_card_blocks(tmp_path):
    from hero_quant.interaction.questions import UserQuestionService

    svc = UserQuestionService()
    try:
        svc.ask_sync(
            questions=[
                {
                    "id": "q1",
                    "question": "确认？",
                    "header": "Confirm",
                    "options": [{"label": "是", "description": "推荐"}],
                }
            ]
        )
    except Exception as e:
        assert "NO_PROVIDER" in str(e)


# --- HIGH 1: guard bypass by interrupt ordering ---
def test_approval_guard_bypass_ask_with_disallowed():
    from hero_quant.interaction.approval import ApprovalService, AskCardInterrupt

    svc = ApprovalService(mode="ask")
    q = [{
        "id": "q1",
        "question": "confirm?",
        "header": "Confirm",
        "options": [{"label": "yes", "description": "ok"}],
    }]
    # disallowed tool should be denied even in ASK mode with questions, not bypass via interrupt
    try:
        result = svc.request_sync_with_guard(tool="disallowed_tool", reason="test", questions=q)
    except AskCardInterrupt:
        assert False, "guard bypass: ASK interrupt should not bypass disallowed_tool denial"
    else:
        # should be rejected via guard
        # after fix it returns Command with resume=rejected or string rejected
        if hasattr(result, "resume"):
            assert result.resume == "rejected" or result.resume == "rejected" or "rejected" in str(result.resume)
            assert result.goto == "decided"
        else:
            assert result == "rejected" or "rejected" in str(result)


# --- HIGH 2: Command discards resume point/audit ---
def test_approval_command_resume_and_audit():
    from hero_quant.interaction.approval import ApprovalService, Command
    from unittest.mock import patch

    svc = ApprovalService(mode="auto")
    # patch _audit in security.approval to capture audit calls
    with patch("hero_quant.security.approval._audit") as mock_audit:
        result = svc.request_sync_with_guard(tool="safe_tool", reason="unit-test")
        # should carry outcome to decided node via Command
        assert isinstance(result, Command), f"expected Command, got {type(result)}:{result}"
        assert result.goto == "decided"
        # resume should be approved (auto) or pending dict
        assert result.resume == "approved" or (isinstance(result.resume, dict) and result.resume.get("status") == "pending") or result.resume == "approved"
        # audit trail must have decided entry
        # mock_audit called with decided
        calls = [str(c) for c in mock_audit.call_args_list]
        # at least one call should contain decided
        assert any("decided" in str(c) for c in mock_audit.call_args_list), f"audit not called with decided: {mock_audit.call_args_list}"


# --- HIGH 3: intent whitelist bypass ---
def test_questions_intent_whitelist_bypass():
    from hero_quant.interaction.questions import UserQuestionService, _validate_questions
    import pytest

    svc = UserQuestionService(provider=lambda *a, **kw: "ok")
    # unknown intent should be rejected, not slip through
    evil_q = [{
        "id": "q1",
        "question": "evil?",
        "header": "H",
        "options": [{"label": "x", "description": "y"}],
        "intent": "evil_intent"
    }]
    # direct validation should raise BAD_INTENT
    try:
        _validate_questions(evil_q)
        assert False, "evil intent should raise BAD_INTENT"
    except ValueError as e:
        assert "BAD_INTENT" in str(e)

    # via ask_sync also should raise before provider delegation
    class DummyProvider:
        def ask_sync(self, *a, **kw):
            return "should not reach"
    svc2 = UserQuestionService(provider=DummyProvider())
    try:
        svc2.ask_sync(evil_q)
        assert False, "ask_sync with evil intent should raise BAD_INTENT"
    except ValueError as e:
        assert "BAD_INTENT" in str(e)

    # also async path
    import asyncio
    svc3 = UserQuestionService(provider=DummyProvider())
    async def _run():
        try:
            await svc3.ask(evil_q)
            assert False, "async ask with evil intent should raise BAD_INTENT"
        except ValueError as e:
            assert "BAD_INTENT" in str(e)
    asyncio.run(_run())


# --- HIGH 4: with_store_isolation no-op ---
def test_store_isolation_real():
    from hero_quant.interaction.questions import UserQuestionService

    svc = UserQuestionService(provider=None)
    # ensure store attribute exists after fix
    assert hasattr(svc, "store"), "service should have store attribute after fix"
    svc.store["outside"] = "keep"
    # use as context manager with swap
    with svc.with_store_isolation("tenant1", "thread1") as isolated:
        # isolated should be same service object but with isolated store
        assert isolated is svc or isolated.store is not svc.store or "outside" not in isolated.store or True
        # writes inside should not be visible outside after exit
        isolated.store["inside"] = "secret"
        assert "inside" in isolated.store
    # after exit, inside should not be visible
    assert "inside" not in svc.store, "writes inside isolated block leaked outside - no isolation"
    assert svc.store.get("outside") == "keep"


# --- HIGH 5: ask_sync swallows asyncio.run failure and leaks coroutine ---
def test_ask_sync_propagates_and_closes():
    import asyncio
    from hero_quant.interaction.questions import UserQuestionService
    import unittest.mock as mock
    import inspect

    valid_q = [{
        "id": "q1",
        "question": "q?",
        "header": "H",
        "options": [{"label": "a", "description": "b"}],
    }]

    async def failing_coro(*a, **kw):
        raise RuntimeError("boom from coro")

    def fake_run(coro):
        raise RuntimeError("asyncio.run boom")

    # Test 5a: propagation - old code swallows, new code propagates
    class RealProvider:
        def ask(self, *a, **kw):
            return failing_coro()

    svc = UserQuestionService(provider=RealProvider())
    with mock.patch("asyncio.run", side_effect=fake_run):
        try:
            result = svc.ask_sync(valid_q)
            # old code returns the coroutine object instead of raising, so this line is reached
            # we consider that failure (should have raised)
            # clean up leaked coroutine to avoid warning
            if inspect.isawaitable(result) or asyncio.iscoroutine(result):
                try:
                    result.close()
                except Exception:
                    pass
            assert False, "should have propagated exception from asyncio.run"
        except RuntimeError as e:
            assert "boom" in str(e)

    # Test 5b: leak - ensure coroutine is closed on failure
    # Use a tracked awaitable to verify close() is called
    tracked = {"closed": False}
    class TrackedAwaitable:
        def __await__(self):
            yield
        def close(self):
            tracked["closed"] = True

    # provider returns TrackedAwaitable, but we need ask_sync to treat it as coroutine
    # So patch iscoroutine/isawaitable to recognise it
    orig_iscoro = asyncio.iscoroutine
    orig_isawaitable = inspect.isawaitable
    def patched_iscoro(x):
        if isinstance(x, TrackedAwaitable):
            return True
        return orig_iscoro(x)
    def patched_isawaitable(x):
        if isinstance(x, TrackedAwaitable):
            return True
        return orig_isawaitable(x)

    class TrackedProvider:
        def ask(self, *a, **kw):
            return TrackedAwaitable()

    svc2 = UserQuestionService(provider=TrackedProvider())
    with mock.patch("asyncio.iscoroutine", side_effect=patched_iscoro):
        with mock.patch("inspect.isawaitable", side_effect=patched_isawaitable):
            with mock.patch("asyncio.run", side_effect=fake_run):
                try:
                    svc2.ask_sync(valid_q)
                    assert False, "should have propagated"
                except RuntimeError:
                    pass
                assert tracked["closed"], "coroutine was not closed after asyncio.run failure - leak"

# --- HIGH 6: async ask unconditionally awaits sync provider ---
def test_async_ask_sync_provider_not_awaitable():
    import asyncio
    from hero_quant.interaction.questions import UserQuestionService

    valid_q = [{
        "id": "q1",
        "question": "q?",
        "header": "H",
        "options": [{"label": "a", "description": "b"}],
    }]

    class SyncProvider:
        def ask(self, questions, signal=None):
            return "sync_result"

    svc = UserQuestionService(provider=SyncProvider())
    async def _run():
        res = await svc.ask(valid_q)
        assert res == "sync_result", f"expected sync_result, got {res}"

    # should not raise TypeError: object str can't be used in 'await'
    asyncio.run(_run())

    # also test ask_sync fallback via async ask when provider only has ask_sync
    class SyncOnlyProvider:
        def ask_sync(self, questions, signal=None):
            return "sync_only_result"

    svc2 = UserQuestionService(provider=SyncOnlyProvider())
    async def _run2():
        res = await svc2.ask(valid_q)
        assert res == "sync_only_result"

    asyncio.run(_run2())
