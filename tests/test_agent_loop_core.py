"""Task16 Wave4 Top7 - loop core TDD tests"""
import pathlib

def test_keyboard_interrupt_propagates():
    src = pathlib.Path("src/hero_quant/agent/loop.py").read_text(encoding="utf-8")
    # 必须显式放行中断异常：含 (KeyboardInterrupt,SystemExit,GeneratorExit): raise
    assert "(KeyboardInterrupt, SystemExit, GeneratorExit)" in src or "(KeyboardInterrupt,SystemExit,GeneratorExit)" in src, "must handle KeyboardInterrupt/SystemExit/GeneratorExit separately"
    assert "except (KeyboardInterrupt" in src and "raise" in src, "must re-raise interrupt"
    # 不再用 except BaseException 包 LLM 调用
    # 统计 BaseException 出现次数应为 0（或仅在类型注解）
    # 允许在注释中提及，但 except BaseException 不应存在
    assert "except BaseException" not in src, "should not use except BaseException around LLM calls"

def test_replay_path_traversal_rejected(tmp_path):
    import pathlib as _pl
    from hero_quant.agent.loop import AgentLoop

    # 建白名单目录 replays
    replays = tmp_path / "replays"
    replays.mkdir(parents=True)
    # 尝试目录穿越：tmp_path/.. /passwd 应被拒绝
    traversal = tmp_path / ".." / "passwd"
    try:
        # 测试可传 allow_root 或临时目录：尝试多种参数名兼容
        AgentLoop(llm=object(), replay_path=str(traversal), allow_root=str(replays))
        assert False, "traversal via allow_root should raise ValueError"
    except ValueError:
        pass
    except TypeError:
        # 若不支持 allow_root，则尝试 replay_root
        try:
            AgentLoop(llm=object(), replay_path=str(traversal), replay_root=str(replays))
            assert False, "traversal via replay_root should raise ValueError"
        except ValueError:
            pass

    # 绝对路径 /etc/passwd 也应拒绝（使用默认或显式 allow_root）
    try:
        AgentLoop(llm=object(), replay_path="/etc/passwd", allow_root=str(replays))
        assert False, "/etc/passwd should be rejected"
    except ValueError:
        pass
    except TypeError:
        try:
            AgentLoop(llm=object(), replay_path="/etc/passwd", replay_root=str(replays))
            assert False, "/etc/passwd should be rejected"
        except ValueError:
            pass

    # 合法路径应在白名单内通过（不抛）
    good = replays / "llm_usage.json"
    good.write_text("{}", encoding="utf-8")
    try:
        loop = AgentLoop(llm=object(), replay_path=str(good), allow_root=str(replays))
        assert loop._replay_path is not None
        # resolved path应在 allow 内
        assert _pl.Path(loop._replay_path).resolve().is_relative_to(replays.resolve())
    except TypeError:
        loop = AgentLoop(llm=object(), replay_path=str(good), replay_root=str(replays))
        assert loop._replay_path is not None

def test_token_limit_char_conversion():
    src = pathlib.Path("src/hero_quant/agent/loop.py").read_text(encoding="utf-8")
    # 截断用 char_limit=token_limit*4
    assert "token_limit" in src and "*4" in src, "token_limit should be converted via *4"
    # 至少有一处 buffer 切片使用 *4
    assert "*4" in src and "buffer" in src, "buffer truncation should use char_limit *4"
    # 检查存在 int(...)*4 或 * 4 模式
    assert "int(" in src and "*4" in src
