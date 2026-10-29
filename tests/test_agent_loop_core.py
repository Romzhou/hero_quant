"""Task16 Wave4 Top7 - loop core TDD tests"""
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
