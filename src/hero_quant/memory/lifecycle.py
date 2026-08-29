"""记忆生命周期：Ebbinghaus 衰减与 GC 回收。

职责：为 MemoryStore 提供重要性评估与过期归档能力；上游由 store/调度触发，下游落盘到文件归档与 gc.log。
设计要点：半衰期 14 天（λ=ln2/14），重要性=quality*(exp(-λ*days)+min(0.3,access*0.1))；GC 阈值 archive 0.15 / delete 0.05，年龄门限 7 天，上限 500，默认启用足龄删除。
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from pathlib import Path
from types import MappingProxyType

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 14.0
_DECAY_LAMBDA = math.log(2) / HALF_LIFE_DAYS  # 衰减系数 λ，决定半衰期
_ACCESS_BOOST = 0.1  # 每次访问的增益，封顶 0.3

# GC 阈值：archive 0.15 为归档线，delete 0.05 为删除线。
ARCHIVE_THRESHOLD = 0.15
DELETE_THRESHOLD = 0.05
MIN_AGE_DAYS = 7  # 仅对 7 天以上条目做 GC，避免新记忆被误回收
MAX_AGE_DAYS = 30  # 删除线还需要达到最大保留年龄
MAX_AGE = MAX_AGE_DAYS  # 兼容计划与调用方使用的简短名称
MAX_MEMORY_COUNT = 500  # 容量上限，触发上游限流/归档
ENABLE_DELETE = True  # 默认启用删除；实例可覆写，仍受年龄门禁保护


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
    MAX_AGE_DAYS = MAX_AGE_DAYS
    MAX_AGE = MAX_AGE
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
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: lifecycle optional", exc_info=_exc)  # intentional: offline-safe: lifecycle optional
            pass  # intentional offline-safe: lifecycle optional
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

    def _resolve_meta(self, file_path: Path, _meta_lookup: dict | None = None) -> tuple[float, int, float]:
        """按文件反查 _meta，拿不到则回退到 frontmatter 或默认值。"""
        qs, ac, last = 0.5, 0, time.time()

        def apply_meta(value) -> None:
            nonlocal qs, ac, last
            if not isinstance(value, dict):
                return
            try:
                qs = float(value.get("quality_score", qs))
            except (TypeError, ValueError):
                pass
            try:
                ac = int(value.get("access_count", ac))
            except (TypeError, ValueError):
                pass
            try:
                last = float(value.get("last_accessed", last))
            except (TypeError, ValueError):
                pass

        # P2: 若上层已预计算 safe_filename -> meta 映射则直接 O(1) 命中，避免每文件 O(N) 遍历
        if _meta_lookup is not None:
            fname = file_path.name
            if fname in _meta_lookup:
                apply_meta(_meta_lookup[fname])
                return qs, ac, last
            stem = file_path.stem
            if stem in _meta_lookup:
                apply_meta(_meta_lookup[stem])
                return qs, ac, last
            # 预计算未命中则回退 frontmatter
        else:
            meta_dict = getattr(self._memory, "_meta", None)
            if isinstance(meta_dict, dict) and meta_dict:
                # 单次构建 safe 映射，避免双重遍历；仍为 O(N) 但仅一次，且上层 run_gc 会复用预计算
                lookup: dict[str, object] = {}
                stem_lookup: dict[str, object] = {}
                for k, v in meta_dict.items():
                    try:
                        safe = self._memory._safe_filename(k)  # type: ignore
                    except Exception:
                        safe = f"{k}.md"
                    lookup[safe] = v
                    stem_lookup[k] = v
                fname = file_path.name
                if fname in lookup:
                    apply_meta(lookup[fname])  # type: ignore[arg-type]
                    return qs, ac, last
                # 精确 stem 匹配（去 fuzzy endswith）
                stem = file_path.stem
                if stem in stem_lookup:
                    apply_meta(stem_lookup[stem])  # type: ignore[arg-type]
                    return qs, ac, last
                # 层次文件需处理 namespace 前缀替换，未命中则保留默认值
        # 回退解析 frontmatter 中的质量分
        try:
            text = file_path.read_text(encoding="utf-8")
            if text.lstrip().startswith("---"):
                lines = text.lstrip().splitlines()
                for line in lines[1:11]:
                    stripped = line.lstrip()
                    if stripped.startswith("quality_score:"):
                        qs = float(stripped.split(":", 1)[1].strip())
                    elif stripped.startswith("access_count:"):
                        ac = int(stripped.split(":", 1)[1].strip())
                    elif stripped.startswith("last_accessed:"):
                        # 兼容 ISO 与时间戳两种写法，支持缩进
                        val = stripped.split(":", 1)[1].strip()
                        try:
                            last = float(val)
                        except ValueError:
                            try:
                                from datetime import datetime

                                iso = val.replace("Z", "+00:00")
                                last = datetime.fromisoformat(iso).timestamp()
                            except Exception:
                                pass
                    if stripped == "---":
                        break
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: lifecycle optional", exc_info=_exc)  # intentional: offline-safe: lifecycle optional
            pass  # intentional offline-safe: lifecycle optional
        return qs, ac, last

    def run_gc(self, dry_run: bool = True) -> list[dict]:
        """执行 GC：对重要性 <0.15 且年龄 ≥7 天的条目归档，返回动作列表。"""
        entries = self._scan_entries()
        now = time.time()
        # P2: 预计算 safe_filename/stem -> meta 映射，避免每文件 O(N) 遍历导致 O(N^2)
        _meta_lookup: dict[str, object] | None = None
        _stem_lookup: dict[str, object] | None = None
        try:
            _meta_dict = getattr(self._memory, "_meta", None)
            if isinstance(_meta_dict, dict) and _meta_dict:
                _meta_lookup = {}
                _stem_lookup = {}
                for _k, _v in _meta_dict.items():
                    try:
                        _safe = self._memory._safe_filename(_k)  # type: ignore
                    except Exception:
                        _safe = f"{_k}.md"
                    _meta_lookup[_safe] = _v
                    _stem_lookup[_k] = _v
                # 合并供 _resolve_meta 一次查询：stem 覆盖同名冲突以 safe 优先
                _merged = dict(_meta_lookup)
                for _kk, _vv in _stem_lookup.items():
                    _merged.setdefault(_kk, _vv)
                _meta_lookup = _merged
        except Exception:
            _meta_lookup = None
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
            qs, ac, last_accessed = self._resolve_meta(file_path, _meta_lookup=_meta_lookup)
            days_since = (now - last_accessed) / 86400.0
            imp = compute_importance(qs, ac, days_since)
            action = None
            reason = ""
            if imp < self.DELETE_THRESHOLD and self.ENABLE_DELETE and age_days >= self.MAX_AGE:
                action = "delete"
                reason = f"importance {imp:.3f} < delete threshold and age {age_days:.1f}d >= max age"
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
                # 目标冲突计数版本重试，重试 rename 原子
                counter = 1
                stem = file_path.stem
                suffix = file_path.suffix
                while dest.exists():
                    dest = archive_dir / f"{stem}.{counter}{suffix}"
                    counter += 1
                while True:
                    try:
                        file_path.rename(dest)
                        break
                    except FileExistsError as exc:
                        logger.warning("GC archive collision FileExistsError for %s -> %s: %s", file_path, dest, exc)
                        dest = archive_dir / f"{stem}.{counter}{suffix}"
                        counter += 1
                        continue
                    except (OSError, IOError) as exc:
                        if dest.exists():
                            logger.warning("GC archive dest exists versioning %s: %s", dest, exc)
                            dest = archive_dir / f"{stem}.{counter}{suffix}"
                            counter += 1
                            continue
                        logger.warning("GC archive failed for %s: %s", file_path, exc)
                        return
                # 归档后保留 SQLite 行，搜索回退仍可见，仅文件态视为已回收
                try:
                    from .hierarchy import MemoryHierarchy

                    MemoryHierarchy(self.memory_dir)
                    # 归档后保留 SQLite 行，搜索回退仍可见，仅文件态视为已回收
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: lifecycle optional", exc_info=_exc)  # intentional: offline-safe: lifecycle optional
                    pass  # intentional offline-safe: lifecycle optional
            elif action == "delete":
                dest = archive_dir / file_path.name
                # dest 冲突版本化 dest.stem.{n}.suffix
                if dest.exists():
                    base_stem = dest.stem
                    suffix = dest.suffix
                    counter = 1
                    while dest.exists():
                        dest = archive_dir / f"{base_stem}.{counter}{suffix}"
                        counter += 1
                # 备份写 tmp+rename 原子，读/写包 try 失败 logger.warning+return 不 unlink
                tmp = dest.with_name(dest.name + ".tmp")
                try:
                    content = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError, IOError) as exc:
                    logger.warning("GC delete backup read failed for %s: %s", file_path, exc)
                    return
                try:
                    tmp.write_text(content, encoding="utf-8")
                    tmp.rename(dest)
                except (OSError, IOError) as exc:
                    logger.warning("GC delete backup write failed for %s -> %s: %s", file_path, dest, exc)
                    try:
                        if tmp.exists():
                            tmp.unlink()
                    except OSError:
                        pass
                    return
                try:
                    file_path.unlink()
                except (OSError, IOError) as exc:
                    logger.warning("GC delete unlink failed for %s: %s", file_path, exc)
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

    @staticmethod
    def _read_compressible(file_path: Path) -> str | None:
        """Read one text record; malformed and empty records are not compressible."""
        try:
            content = file_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return content or None

    @staticmethod
    def _summary_sentences(content: str) -> list[str]:
        """Split a record into short sentence-like units without NLP dependencies."""
        sentences = re.split(r"(?:\r?\n+|[.!?。！？]+\s*)", content.strip())
        return [sentence.strip() for sentence in sentences if sentence.strip()]

    @staticmethod
    def _tokens(sentence: str) -> list[str]:
        return re.findall(r"\w+", sentence.lower(), flags=re.UNICODE)

    @classmethod
    def _tfidf_summary(cls, records: list[tuple[str, str]], limit: int = 3) -> str:
        """Return an extractive TF-IDF summary, retaining source order for readability."""
        candidates: list[tuple[int, str, list[str]]] = []
        document_frequency: Counter[str] = Counter()
        for source, content in records:
            for sentence in cls._summary_sentences(content):
                tokens = cls._tokens(sentence)
                if not tokens:
                    continue
                candidates.append((len(candidates), source, sentence))
                document_frequency.update(set(tokens))
        if not candidates:
            return ""

        total = len(candidates)
        scored: list[tuple[float, int, str, str]] = []
        for index, source, sentence in candidates:
            tokens = cls._tokens(sentence)
            score = sum(math.log((1 + total) / (1 + document_frequency[token])) + 1 for token in tokens)
            score /= len(tokens)
            scored.append((score, index, source, sentence))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = sorted(scored[: max(1, limit)], key=lambda item: item[1])
        return "\n".join(f"- {sentence} [{source}]" for _, _, source, sentence in selected)

    def _compressed_archive(self, file_path: Path, stage: str) -> None:
        """Keep compressed sources out of active scans while preserving their contents."""
        archive_dir = self.memory_dir / "archive" / "compressed" / stage
        archive_dir.mkdir(parents=True, exist_ok=True)
        relative = file_path.relative_to(self.memory_dir)
        safe_name = "__".join(relative.parts)
        destination = archive_dir / safe_name
        counter = 1
        while destination.exists():
            destination = archive_dir / f"{safe_name}.{counter}"
            counter += 1
        try:
            file_path.rename(destination)
        except OSError as exc:
            logger.warning("Compression source archive failed for %s: %s", file_path, exc)

    def _index_compression(self, stage: str, period: str, summary: str) -> None:
        """Best-effort index of a written compression summary."""
        try:
            index_external = getattr(self._memory, "index_external", None)
            if not callable(index_external):
                return
            index_external(f"compression:{stage}:{period}", summary)
        except Exception as exc:
            logger.warning("Compression external index failed for %s:%s: %s", stage, period, exc)

    def _compression_sources(self, now: float) -> list[tuple[str, str, list[Path]]]:
        """Collect eligible raw and daily groups as ``(stage, period, sources)``."""
        groups: dict[tuple[str, str], list[Path]] = {}
        for file_path in self._scan_entries():
            if file_path.parent.name in {"archive", "daily", "digest"}:
                continue
            try:
                age_days = (now - file_path.stat().st_mtime) / 86400.0
            except OSError:
                continue
            if age_days < self.MIN_AGE_DAYS or self._read_compressible(file_path) is None:
                continue
            stage = "digest" if age_days >= self.MAX_AGE else "daily"
            period = time.strftime("%Y-%m" if stage == "digest" else "%Y-%m-%d", time.gmtime(file_path.stat().st_mtime))
            groups.setdefault((stage, period), []).append(file_path)

        daily_dir = self.memory_dir / "daily"
        if daily_dir.is_dir():
            for file_path in sorted(daily_dir.glob("*.md")):
                try:
                    age_days = (now - file_path.stat().st_mtime) / 86400.0
                except OSError:
                    continue
                if age_days < self.MAX_AGE or self._read_compressible(file_path) is None:
                    continue
                period = time.strftime("%Y-%m", time.gmtime(file_path.stat().st_mtime))
                groups.setdefault(("digest", period), []).append(file_path)

        return [(stage, period, sources) for (stage, period), sources in sorted(groups.items())]

    def compress(self, dry_run: bool = True, now: float | None = None) -> list[dict]:
        """Compress eligible raw records to daily summaries and daily records to digests."""
        current_time = time.time() if now is None else now
        actions: list[dict] = []
        for stage, period, sources in self._compression_sources(current_time):
            target = self.memory_dir / stage / f"{period}.md"
            # 源只读一次复用
            content_map: dict[Path, str] = {}
            records: list[tuple[str, str]] = []
            for source in sources:
                content = self._read_compressible(source)
                if content is not None:
                    content_map[source] = content
                    records.append((source.stem, content))
            summary = self._tfidf_summary(records)
            if not summary:
                continue
            action = {
                "name": target.stem,
                "action": "compress",
                "stage": stage,
                "source_count": len(records),
                "target": str(target),
            }
            actions.append(action)
            if dry_run:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # target 存在则 merge(去重已存在跳过)或版本化，tmp+replace 原子
                final_text: str | None = summary + "\n"
                if target.exists():
                    try:
                        existing = target.read_text(encoding="utf-8")
                    except OSError as exc:
                        logger.warning("Compression read existing failed for %s: %s", target, exc)
                        existing = ""
                    existing_set = {line.strip() for line in existing.splitlines() if line.strip()}
                    new_lines = [line for line in summary.splitlines() if line.strip() not in existing_set]
                    if not new_lines:
                        # 去重已存在跳过写，但仍归档源
                        final_text = None
                    else:
                        if existing.strip():
                            final_text = existing.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
                        else:
                            final_text = "\n".join(new_lines) + "\n"
                if final_text is not None:
                    tmp = target.with_name(target.name + ".tmp")
                    try:
                        tmp.write_text(final_text, encoding="utf-8")
                        tmp.replace(target)
                    except OSError as exc:
                        logger.warning("Compression write failed for %s: %s", target, exc)
                        try:
                            if tmp.exists():
                                tmp.unlink()
                        except OSError:
                            pass
                        continue
            except OSError as exc:
                logger.warning("Compression write failed for %s: %s", target, exc)
                continue
            self._index_compression(stage, period, summary)
            for source in content_map:
                self._compressed_archive(source, stage)
        return actions

    # 预留接口：与上游事件体系对齐
    def reinforce(self, name: str, event: str, source: str = "system") -> bool:
        """按事件增量强化记忆，未实现时返回 False。"""
        if event not in self._EVENT_DELTAS:
            return False
        return False

    def track_access(self, entry) -> None:
        """记录访问，当前为占位实现。"""
        return None
