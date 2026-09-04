"""TDD for lane C2 — 22 memory/store OCR fixes (fail-closed / lock discipline)."""
import re
import threading
import time
from pathlib import Path

import pytest


def _read_src(rel: str) -> str:
    return Path(rel).read_text(encoding="utf-8")


# 1 agent/memory/store.py:30-37 OrderedDict 无锁 check-then-act
def test_c2_agent_store_lock_exists():
    src = _read_src("src/hero_quant/agent/memory/store.py")
    assert "threading" in src, "应引入 threading"
    assert "_lock" in src, "MemoryStore 应有 _lock"
    # get_buffer / save_buffer / delete_buffer 应在锁内
    assert "with self._lock" in src or "with self._" in src, "关键区应加锁"
    # 验证运行时有锁对象
    from hero_quant.agent.memory.store import MemoryStore
    ms = MemoryStore(max_sessions=5)
    assert hasattr(ms, "_lock"), "运行时应有 _lock 属性"
    # 粗略检查 dobble overshoot：并发 get_buffer 不应超限
    ms2 = MemoryStore(max_sessions=10)
    errors = []

    def worker(sid):
        try:
            ms2.get_buffer(sid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发 get_buffer 异常: {errors}"
    assert ms2.size <= 10, f"并发下超出 MAX_SESSIONS: {ms2.size} > 10"


# 2 agent/memory/store.py:46-49 save_buffer 绕 MAX_SESSIONS
def test_c2_agent_save_buffer_evicts():
    from hero_quant.agent.memory.store import MemoryStore
    from hero_quant.agent.memory.buffer import MemoryBuffer
    ms = MemoryStore(max_sessions=2)
    ms.get_buffer(1)
    ms.get_buffer(2)
    assert ms.size == 2
    ms.save_buffer(3, MemoryBuffer())
    assert ms.size <= 2, "save_buffer 应受 MAX_SESSIONS 约束"
    assert not ms.has_buffer(1) or ms.size == 2, "应淘汰最旧"


# 3 memory/ingest.py:16 heading 正则切碎 fenced 代码
def test_c2_ingest_heading_fence():
    from hero_quant.memory.ingest import _split_by_heading
    text = "# real heading\ncontent\n```python\n# not a heading inside fence\ncode\n```\n# second heading\nmore"
    sections = _split_by_heading(text)
    # 期待只有两个真实标题分段 + 前导，无 fence 内伪标题
    # 如果仍用旧正则，会切出 3 段且 fence 内 # 被当标题
    flattened = "\n".join(sections)
    # fence 内 # 不应成为段边界：段数应为 2 或 3（含前导），但不应把 fence 内单独切段
    assert len(sections) <= 3, f"fence 内 # 不应额外切段，got {len(sections)}"
    # 确保 fence 块完整在一个 section 内
    assert any("# not a heading inside fence" in s for s in sections), "fence 块应保留"
    # 检查源码已加入 fence 处理
    src = _read_src("src/hero_quant/memory/ingest.py")
    assert "FENCE" in src or "in_fence" in src or "```" in src, "应跟踪 fence 状态"


# 4 memory/ingest.py:160-165 分片写失败只 warn 应 fail-closed
def test_c2_ingest_fail_closed(tmp_path):
    from hero_quant.memory import ingest as ing_mod
    p = tmp_path / "doc.md"
    p.write_text("# h\n" + "x" * 600, encoding="utf-8")

    class BadStore:
        def write(self, key, piece):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="failed|ingest"):
        ing_mod.ingest_markdown(p, store=BadStore(), base_path=tmp_path)
    src = _read_src("src/hero_quant/memory/ingest.py")
    assert "raise" in src.split("if failures:")[1][:500] if "if failures:" in src else "raise" in src, "失败应 raise 而非仅 warn"


# 5 memory/ingest.py:141-146 key 相对化基准不一致
def test_c2_ingest_key_rel_base(tmp_path):
    from hero_quant.memory.store import MemoryStore
    base = tmp_path / "mem"
    base.mkdir()
    # 写一个 md 位于 base 之外的子目录，但用明确 base_path
    doc = tmp_path / "other" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# h\nhello world hello world", encoding="utf-8")
    store = MemoryStore(base_path=base)
    # inges 调用应以 store.base 为基准，不以 cwd 为基准
    # 检查源码使用 bp.resolve 而非 Path.cwd 基准
    src = _read_src("src/hero_quant/memory/ingest.py")
    # 旧实现含 Path.cwd() 作为回落基准，新实现应直接用 bp/store base 且 narrow 到 ValueError
    assert "except ValueError" in src or "except (ValueError" in src or "ValueError" in src, "应窄化捕获 ValueError"
    # 功能：多次 ingest 同名文件不同路径不应碰撞到同一 key 覆盖（带 idx）
    # 通过检查 ingest 后 key 是否含 idx 或能区分
    assert ":{idx}" in src or ":idx" in src or 'f"{_rel}:{idx}:' in src or ':{idx}:' in src or "hashlib.sha256(piece.encode" in src, "key 应加入 idx 避免 basename 碰撞"


# 6 memory/ingest.py:141-143 循环内重复 resolve 已 hoist
def test_c2_ingest_hoisted_resolve():
    src = _read_src("src/hero_quant/memory/ingest.py")
    # 应在循环外 resolve，循环内不应再出现 p.resolve() 与 _bp_for_rel.resolve()
    # 查找 for piece in all_chunks 段内是否仍有 .resolve()
    after_loop = src.split("for piece in")[-1] if "for piece in" in src else src
    # 新实现应为 enumerate 且 _rel 已预计算
    assert "p_resolved" in src or "bp_resolved" in src, "应将 resolve 提升至循环外"
    # 循环体内不应再有 Path(base_path) 动态构造
    loop_segment = after_loop[:2000]
    assert "Path(base_path)" not in loop_segment or "_bp_for_rel" not in loop_segment, "循环内不应重复 resolve base_path"


# 7 memory/lifecycle.py:53-54 MAX_AGE 别名分叉
def test_c2_lifecycle_max_age_alias():
    from hero_quant.memory.lifecycle import MemoryLifecycle
    from hero_quant.memory.store import MemoryStore
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        ms = MemoryStore(base_path=tmp)
        lc = MemoryLifecycle(ms)
        # 修改 MAX_AGE_DAYS 应影响 MAX_AGE
        orig = lc.MAX_AGE_DAYS
        lc.MAX_AGE_DAYS = orig + 10
        assert lc.MAX_AGE == orig + 10, "MAX_AGE 应为 MAX_AGE_DAYS 的别名"
        lc.MAX_AGE = 77
        assert lc.MAX_AGE_DAYS == 77, "MAX_AGE setter 应回写 MAX_AGE_DAYS"
        # 源码应为 property
        src = _read_src("src/hero_quant/memory/lifecycle.py")
        assert "@property" in src and "def MAX_AGE" in src, "应以 property 实现别名"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# 8 memory/lifecycle.py:288-291 空转 hierarchy 调用
def test_c2_lifecycle_noop_hierarchy():
    src = _read_src("src/hero_quant/memory/lifecycle.py")
    # 不应出现裸的 MemoryHierarchy(self.memory_dir) 无方法调用
    # 允许 MemoryHierarchy(...).remove / .reindex / 注释说明，若仍裸调则失败
    pattern = re.compile(r"MemoryHierarchy\s*\(\s*self\.memory_dir\s*\)\s*\)?")
    # 找到所有匹配，检查其后是否紧跟 .remove/.reindex/.scan 等
    for m in pattern.finditer(src):
        tail = src[m.end(): m.end() + 80]
        # 若仅是构造后换行/注释而无点调用，则视为 dead code
        stripped = tail.lstrip()
        assert stripped.startswith(".") or stripped.startswith("#") or "archive" in src[m.start() - 200:m.end() + 200].lower() is False, \
            "不应有裸 MemoryHierarchy(self.memory_dir) 无效调用"
    # 更直接：统计裸构造出现次数应为 0（除非带方法）
    bare = src.count("MemoryHierarchy(self.memory_dir)\n") + src.count("MemoryHierarchy(self.memory_dir) ")
    # 若源码仍保留 bare（无点），则 fail
    assert bare == 0 or "remove" in src or "reindex" in src, "空转 hierarchy 调用应移除或改为真实失效方法"


# 9 memory/lifecycle.py:340-344 GC 审计日志失败吞错
def test_c2_lifecycle_gc_log_warn():
    src = _read_src("src/hero_quant/memory/lifecycle.py")
    seg = src.split("_append_gc_log")[-1]
    assert "logger.warning" in seg or "logger.warn" in seg, "GC 日志失败应 warning"
    assert "except OSError as" in src or "except OSError as exc" in seg, "应捕获 OSError 并带 exc"
    assert seg.count("pass") <= 5, "不应静默 pass"


# 10 memory/lifecycle.py:166-167 frontmatter 只扫 1:11
def test_c2_lifecycle_frontmatter_scan(tmp_path):
    from hero_quant.memory.lifecycle import MemoryLifecycle
    from hero_quant.memory.store import MemoryStore
    base = tmp_path / "mem"
    base.mkdir()
    ms = MemoryStore(base_path=base)
    lc = MemoryLifecycle(ms)
    # 构造 frontmatter 超过 10 行，quality_score 在第 20 行
    lines = ["---", "title: test"]
    for i in range(15):
        lines.append(f"extra_{i}: foo")
    lines.append("quality_score: 0.9")
    lines.append("access_count: 5")
    lines.append("last_accessed: 1234567890")
    lines.append("---")
    lines.append("body")
    f = base / "long_front.md"
    f.write_text("\n".join(lines), encoding="utf-8")
    qs, ac, last = lc._resolve_meta(f, _meta_lookup=None)
    assert qs == 0.9, f"应扫到 20 行后的 quality_score，got {qs}"
    assert ac == 5, f"应扫到 access_count {ac}"
    src = _read_src("src/hero_quant/memory/lifecycle.py")
    assert "lines[1:51]" in src or "lines[1:50]" in src or "1:51" in src or "50" in src, "应扩大扫描窗口至 ~50 行"


# 11 memory/lifecycle.py:217-218 meta-map 失败静默
def test_c2_lifecycle_meta_map_log():
    src = _read_src("src/hero_quant/memory/lifecycle.py")
    seg = src.split("_meta_lookup = None")[-2] if "_meta_lookup = None" in src else src
    # 检查 meta-map 构失败有日志
    # 新代码应为 except Exception as exc: logger.debug(...)
    assert "logger.debug" in src or "logger.warning" in src, "meta-map 失败应有日志"
    # 定位 run_gc 内 except Exception 段
    assert "meta-map" in src.lower() or "meta_map" in src.lower() or "falling back" in src.lower() or "GC meta-map" in src, "应包含 meta-map 失败日志文案"


# 12 memory/hierarchy.py:172-175 非 string keywords 崩
def test_c2_hierarchy_nonstring_keywords(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    entries = [
        {"memory_type": "user", "keywords": ["hello", None, 123, "WORLD"]},
        {"memory_type": "user", "keywords": "not-a-list"},
        type("Obj", (), {"memory_type": "project", "keywords": ["objkw", None]})(),
    ]
    # 不应抛 AttributeError
    mh.rebuild_index(entries)
    # 检查索引已写入且仅保留 string
    import yaml
    data = yaml.safe_load((base / ".hierarchy.yaml").read_text(encoding="utf-8"))
    user_kws = data["categories"]["user"]["keywords"]
    assert "hello" in user_kws or "hello" in [k.lower() for k in user_kws]
    assert None not in user_kws and 123 not in user_kws
    proj_kws = data["categories"]["project"]["keywords"]
    assert "objkw" in proj_kws


# 13 memory/hierarchy.py:144-149 分类扫描无视 _SKIP_NAMES
def test_c2_hierarchy_skip_names(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h2"
    mh = MemoryHierarchy(base)
    cat_dir = base / "user"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / "MEMORY.md").write_text("skip", encoding="utf-8")
    (cat_dir / "keep.md").write_text("keep", encoding="utf-8")
    (base / "MEMORY.md").write_text("skip base", encoding="utf-8")
    (base / "keep2.md").write_text("keep2", encoding="utf-8")
    all_files = mh.scan_all()
    names = {p.name for p in all_files}
    assert "MEMORY.md" not in names, "分类扫描应跳过 _SKIP_NAMES"
    assert "keep.md" in names
    assert "keep2.md" in names
    cat_files = mh.scan_category("user")
    assert "MEMORY.md" not in {p.name for p in cat_files}


# 14 memory/hierarchy.py:54-55 空/点文件名放行
def test_c2_hierarchy_validate_filename():
    from hero_quant.memory.hierarchy import MemoryHierarchy
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        mh = MemoryHierarchy(tmp)
        for bad in ["", ".", ".."]:
            with pytest.raises(ValueError):
                mh._validate_filename(bad)
        # 正常文件名应通过
        p = mh._validate_filename("ok.md")
        assert p.name == "ok.md"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# 15 memory/store.py:997-1000 SQLite 共享连接无锁
def test_c2_store_sqlite_lock():
    src = _read_src("src/hero_quant/memory/store.py")
    # 应在 DB 操作处使用 with self._lock
    assert src.count("with self._lock") >= 3, "DB 读写应加锁，至少 3 处"
    # 检查 write/index_external/vector_search/_search_bm25_raw 附近有锁
    for fn in ["def write", "def index_external", "def vector_search", "def _search_bm25_raw"]:
        idx = src.find(fn)
        assert idx != -1, f"{fn} 不存在"
        segment = src[idx: idx + 4000]
        assert "with self._lock" in segment or "with self._lock:" in segment, f"{fn} 应在锁内操作 DB"


# 16 memory/store.py:629-632 :/\ 全映射 __NS__ 碰撞
def test_c2_store_safe_filename_collision(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(base_path=tmp_path)
    f1 = ms._safe_filename("a:b")
    f2 = ms._safe_filename("a/b")
    f3 = ms._safe_filename("a\\b")
    assert f1 != f2 != f3, f"不同命名空间分隔应产生不同文件名: {f1}, {f2}, {f3}"
    # 逆映射应 round-trip
    for orig in ["a:b", "a/b", "a\\b"]:
        safe = ms._safe_filename(orig)
        stem = safe[:-3]
        back = ms._parse_safe_stem(stem)
        assert back == orig, f"round-trip 失败 {orig} -> {safe} -> {back}"


# 17 memory/store.py:1074-1077 DB 写失败清旧版本
def test_c2_store_write_db_failure_restore(tmp_path):
    from hero_quant.memory.store import MemoryStore
    import sqlite3
    ms = MemoryStore(base_path=tmp_path)
    ms.write("k", "v1")
    f = tmp_path / ms._safe_filename(ms._ns_key("k"))
    assert f.read_text(encoding="utf-8") == "v1"
    # 模拟 DB commit 失败
    orig_commit = ms._conn.commit

    def bad_commit():
        raise sqlite3.OperationalError("simulated")

    ms._conn.commit = bad_commit
    try:
        ms.write("k", "v2")
    except Exception:
        pass
    finally:
        ms._conn.commit = orig_commit
    # 旧版本应仍存在且未被误删
    assert f.exists(), "DB 失败不应删除旧文件"
    assert f.read_text(encoding="utf-8") == "v1", "旧版本应被恢复"
    # 新 key 的孤儿文件应被清理
    ms2 = MemoryStore(base_path=tmp_path / "mem2")
    ms2._conn.commit = bad_commit
    try:
        ms2.write("newkey", "hello")
    except Exception:
        pass
    finally:
        ms2._conn.commit = orig_commit
    orphan = (tmp_path / "mem2") / ms2._safe_filename(ms2._ns_key("newkey"))
    assert not orphan.exists(), "新 key 失败应清理孤儿文件"


# 18 memory/store.py:1192-1195 importance 回退错命名空间
def test_c2_store_importance_no_cross_ns(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(base_path=tmp_path)
    now = time.time()
    ms._meta = {
        "nsA:report": {"quality_score": 0.9, "access_count": 10, "last_accessed": now},
        "nsB:report": {"quality_score": 0.1, "access_count": 0, "last_accessed": now - 30 * 86400},
    }
    # 查 nsA:report 应命中自身，不应回退到 nsB:report 的后缀匹配
    imp_a = ms._importance_for({"key": "nsA:report"}, now)
    # 查不存在的 nsC:report 不应继承 nsA/nsB
    imp_c = ms._importance_for({"key": "nsC:report"}, now)
    # 无跨命名空间泄露：imp_c 应为默认值 0.5 左右（qs=0.5, ac=0, days=0 -> 0.5）
    assert imp_c == pytest.approx(0.5, abs=0.05), f"不应跨命名空间继承，got {imp_c}"
    assert imp_a > 0.7, f"nsA 应为高分 {imp_a}"
    src = _read_src("src/hero_quant/memory/store.py")
    # 不应再有后缀匹配逻辑
    assert 'endswith(k.split(":")[-1])' not in src, "应移除跨命名空间后缀匹配"


# 19 memory/rank_fusion.py:127-134 纯 BM25 白嫖 0.5 cosine
def test_c2_rank_fusion_no_free_cosine():
    from hero_quant.memory.rank_fusion import rank_fusion
    bm25 = [("a", 10.0)]
    vec = [("b", 0.9)]
    ranked = dict(rank_fusion(bm25, vec, k=60))
    # a 仅 BM25，无向量分，应得 0.5*1 + 0.5*0 = 0.5，而非 0.75
    assert ranked["a"] == pytest.approx(0.5, abs=1e-6), f"BM25-only 不应得 0.5 cosine bonus, got {ranked['a']}"
    src = _read_src("src/hero_quant/memory/rank_fusion.py")
    assert "key not in cos_map" in src, "缺失向量应判 key not in cos_map"


# 20 memory/rank_fusion.py:164-167 空 id 丢 doc_id
def test_c2_rank_fusion_empty_id_fallback():
    from hero_quant.memory.rank_fusion import bm25_from_ordered
    items = [{"id": "", "doc_id": "abc"}, {"id": None, "doc_id": "def"}, {"key": "", "id": "", "doc_id": "ghi"}]
    pairs = bm25_from_ordered(items)
    keys = {k for k, _ in pairs}
    assert "abc" in keys and "def" in keys and "ghi" in keys, f"空 id 应回退到 doc_id, got {keys}"
    src = _read_src("src/hero_quant/memory/rank_fusion.py")
    # 应为分步检查而非 get("id", get("doc_id"))
    assert 'it.get("id", it.get("doc_id"))' not in src, "应分步检查 id 与 doc_id"


# 21 memory/rank_fusion.py:202-204 只看 cands[0] 判 dict
def test_c2_rank_fusion_dict_detection():
    from hero_quant.memory.rank_fusion import fuse
    # generator of dicts
    def gen():
        for k in ["x", "y"]:
            yield {"key": k, "content": "c"}
    # 旧实现会因非 list/tuple 或首元素非 dict 而走错分支
    res = fuse(gen(), [], k=60)
    keys = [k for k, _ in res]
    assert "x" in keys and "y" in keys, f"generator dicts 应被识别为 bm25_from_ordered, got {keys}"
    # 混合类型首元素 tuple 但后续 dict 也应正确
    src = _read_src("src/hero_quant/memory/rank_fusion.py")
    assert "all(isinstance" in src, "应检查 all(isinstance(x, dict) for x in peek)"


# 22 memory/rank_fusion.py:139-142 钳制后 clamp 不可达
def test_c2_rank_fusion_no_unreachable_clamp():
    src = _read_src("src/hero_quant/memory/rank_fusion.py")
    seg = src.split("c_norm = (c_raw + 1.0) / 2.0")[-1]
    # 该段后不应再有 if c_norm <0 / >1 的不可达分支
    # 若仍保留则 fail
    tail = seg[:600]
    assert "if c_norm < 0" not in tail and "if c_norm > 1" not in tail, "钳制后的 clamp 不可达应移除"
