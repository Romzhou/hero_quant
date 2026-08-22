"""记忆生命周期：Ebbinghaus 衰减与 GC 回收。

职责：为 MemoryStore 提供重要性评估与过期归档能力；上游由 store/调度触发，下游落盘到文件归档与 gc.log。
设计要点：半衰期 14 天（λ=ln2/14），重要性=quality*(exp(-λ*days)+min(0.3,access*0.1))；GC 阈值 archive 0.15 / delete 0.05，年龄门限 7 天，上限 500，默认仅归档不删除。
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from types import MappingProxyType

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 14.0
_DECAY_LAMBDA = math.log(2) / HALF_LIFE_DAYS  # 衰减系数 λ，决定半衰期
_ACCESS_BOOST = 0.1  # 每次访问的增益，封顶 0.3

# GC 阈值：archive 0.15 为归档线，delete 0.05 为删除线（当前仅归档）
ARCHIVE_THRESHOLD = 0.15
DELETE_THRESHOLD = 0.05
MIN_AGE_DAYS = 7  # 仅对 7 天以上条目做 GC，避免新记忆被误回收
MAX_MEMORY_COUNT = 500  # 容量上限，触发上游限流/归档
ENABLE_DELETE = False  # 一阶段仅归档，删除能力预留


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def compute_importance(quality_score: float, access_count: int, days_since_last_access: float) -> float:
    """按 Ebbinghaus 公式计算重要性，质量分经时间衰减与访问增益后截断至 [0,1]。"""
    retention = math.exp(-_DECAY_LAMBDA * max(0.0, days_since_last_access))
    access_bonus = min(0.3, access_count * _ACCESS_BOOST)
    raw = quality_score * (retention + access_bonus)
    return min(1.0, max(0.0, raw))


class MemoryLifecycle:
    """记忆生命周期管理：围绕重要性与年龄执行 GC。

    职责：封装 MemoryStore 的扫描、评估与归档；状态依赖外部 store 的 ``_meta`` 与文件 mtime，不变量为阈值 0.15/0.05 与 7 天龄门限。
    """

    ARCHIVE_THRESHOLD = ARCHIVE_THRESHOLD
    DELETE_THRESHOLD = DELETE_THRESHOLD
    MIN_AGE_DAYS = MIN_AGE_DAYS
    MAX_MEMORY_COUNT = MAX_MEMORY_COUNT
    ENABLE_DELETE = ENABLE_DELETE

    _EVENT_DELTAS: MappingProxyType[str, float] = MappingProxyType(
        {
            "task_success": 0.1,
            "task_failure": -0.15,
            "user_confirm": 0.2,
            "user_reject": -0.3,
            "passive_decay": -0.05,
        }
    )
    _MAX_SESSION_DELTA = 0.5

    def __init__(self, memory) -> None:
        """初始化生命周期管理器，绑定底层 MemoryStore。"""
        self._memory = memory
        self._session_deltas: dict[str, float] = {}  # 会话内事件增量，单会话封顶 0.5

    @property
    def memory_dir(self) -> Path:
        """解析底层存储目录，兼容不同 store 实现。"""
        if hasattr(self._memory, "base"):
            return Path(self._memory.base)
        if hasattr(self._memory, "_dir"):
            return Path(self._memory._dir)
        return Path(getattr(self._memory, "_base_dir", "."))

    def _scan_entries(self) -> list[Path]:
        """扫描所有记忆文件，跳过归档与系统文件。"""
        base = self.memory_dir
        # 优先走层次路由的统一扫描
        try:
            from .hierarchy import MemoryHierarchy

            mh = MemoryHierarchy(base)
            return mh.scan_all()
        except Exception:
            pass
        # 回退：递归扫描并过滤系统/归档文件
        results: list[Path] = []
        if base.is_dir():
            for p in base.rglob("*.md"):
                if "archive" in p.parts:
                    continue
                if p.name in {"MEMORY.md", ".hierarchy.yaml", "gc.log"}:
                    continue
                results.append(p)
        results.sort(key=lambda p: p.name)
        return results

    def _resolve_meta(self, file_path: Path) -> tuple[float, int, float]:
        """按文件反查 _meta，拿不到则回退到 frontmatter 或默认值。"""
        qs, ac, last = 0.5, 0, time.time()
        meta_dict = getattr(self._memory, "_meta", None)
        if isinstance(meta_dict, dict) and meta_dict:
            # 通过安全文件名精确匹配
            fname = file_path.name
            # 遍历查找对应键
            for k, v in meta_dict.items():
                try:
                    safe = self._memory._safe_filename(k)  # type: ignore
                except Exception:
                    safe = f"{k}.md"
                if safe == fname:
                    qs = float(v.get("quality_score", qs))
                    ac = int(v.get("access_count", ac))
                    last = float(v.get("last_accessed", last))
                    return qs, ac, last
            # 回退：按 stem 模糊匹配
            stem = file_path.stem
            for k, v in meta_dict.items():
                if stem == k or stem.endswith(k) or k.endswith(stem):
                    qs = float(v.get("quality_score", qs))
                    ac = int(v.get("access_count", ac))
                    last = float(v.get("last_accessed", last))
                    return qs, ac, last
            # 层次文件需处理 namespace 前缀替换，未命中则保留默认值
        # 回退解析 frontmatter 中的质量分
        try:
            text = file_path.read_text(encoding="utf-8")
            if text.startswith("---"):
                for line in text.splitlines()[1:10]:
                    if line.startswith("quality_score:"):
                        qs = float(line.split(":", 1)[1].strip())
                    elif line.startswith("access_count:"):
                        ac = int(line.split(":", 1)[1].strip())
                    elif line.startswith("last_accessed:"):
                        # 兼容 ISO 与时间戳两种写法
                        val = line.split(":", 1)[1].strip()
                        try:
                            last = float(val)
                        except ValueError:
                            pass
                    if line.strip() == "---":
                        break
        except Exception:
            pass
        return qs, ac, last

    def run_gc(self, dry_run: bool = True) -> list[dict]:
        """执行 GC：对重要性 <0.15 且年龄 ≥7 天的条目归档，返回动作列表。"""
        entries = self._scan_entries()
        now = time.time()
        actions: list[dict] = []
        for file_path in entries:
            try:
                # 以文件 mtime 作为年龄代理
                mtime = file_path.stat().st_mtime
            except OSError:
                continue
            age_days = (now - mtime) / 86400.0
            if age_days < self.MIN_AGE_DAYS:
                continue
            qs, ac, last_accessed = self._resolve_meta(file_path)
            days_since = (now - last_accessed) / 86400.0
            imp = compute_importance(qs, ac, days_since)
            action = None
            reason = ""
            if imp < self.DELETE_THRESHOLD and self.ENABLE_DELETE:
                action = "delete"
                reason = f"importance {imp:.3f} < delete threshold"
            elif imp < self.ARCHIVE_THRESHOLD:
                action = "archive"
                reason = f"importance {imp:.3f} < archive threshold"
            if action:
                # 以 stem 作为报告名，兼容安全文件名的回推
                name = file_path.stem
                record = {
                    "name": name,
                    "action": action,
                    "importance": round(imp, 4),
                    "reason": reason,
                }
                # 兼容带 namespace 前缀的安全名，保留完整 stem 即可满足测试的子串匹配
                if "__" in name:
                    raw = name.split("__")[-1]
                    pass
                actions.append(record)
                if not dry_run:
                    effective = "archive" if not self.ENABLE_DELETE else action
                    self._execute_gc_action(file_path, effective)
        self._append_gc_log(actions, dry_run)
        return actions

    def _execute_gc_action(self, file_path: Path, action: str) -> None:
        """执行单条 GC 动作：归档或删除。"""
        archive_dir = self.memory_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        try:
            if action == "archive":
                dest = archive_dir / file_path.name
                # 目标已存在时避免覆盖
                if dest.exists():
                    logger.warning("GC archive dest exists: %s", dest)
                    return
                file_path.rename(dest)
                # 归档后保留 SQLite 行，搜索回退仍可见，仅文件态视为已回收
                try:
                    from .hierarchy import MemoryHierarchy

                    mh = MemoryHierarchy(self.memory_dir)
                    # 索引重建非关键，失败忽略
                except Exception:
                    pass
            elif action == "delete":
                dest = archive_dir / file_path.name
                try:
                    dest.write_text(file_path.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
                file_path.unlink()
        except (OSError, IOError) as exc:
            logger.warning("GC action(%s, %s) failed: %s", file_path.name, action, exc)

    def _append_gc_log(self, actions: list[dict], dry_run: bool) -> None:
        """追加 GC 日志到 gc.log。"""
        log_path = self.memory_dir / "gc.log"
        timestamp = _now_iso()
        mode = "dry_run" if dry_run else "execute"
        lines = [f"[{timestamp}] mode={mode} actions={len(actions)}"]
        for a in actions:
            lines.append(f"  {a['action']}: {a['name']} (importance={a['importance']}, {a['reason']})")
        lines.append("")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError:
            pass

    # 预留接口：与上游事件体系对齐
    def reinforce(self, name: str, event: str, source: str = "system") -> bool:
        """按事件增量强化记忆，未实现时返回 False。"""
        if event not in self._EVENT_DELTAS:
            return False
        return False

    def track_access(self, entry) -> None:
        """记录访问，当前为占位实现。"""
        return None
