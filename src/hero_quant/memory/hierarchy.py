"""层次化目录路由：按 memory_type 分流与检索。

职责：为 MemoryStore 提供文件分目录落盘、扫描与索引能力；上游由 store 的写入/检索调用，下游映射到文件系统。
设计要点：类别固定为 user/feedback/project/reference，未知类型回落到 base；扫描跳过 archive 等系统文件；索引为轻量 yaml 统计，关键词上限 10。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)

CATEGORIES = ("user", "feedback", "project", "reference")

_SKIP_NAMES = frozenset({"MEMORY.md", ".hierarchy.yaml", ".lock", "archive", "gc.log"})


@dataclass
class CategorySummary:
    count: int = 0
    keywords: List[str] = field(default_factory=list)


class MemoryHierarchy:
    """管理记忆的层次化目录结构，兼容扁平历史数据。

    职责：按 memory_type 路由读写，维护目录与索引的一致性；不变量为 CATEGORIES 固定集合与 archive 跳过规则。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._base_dir / ".hierarchy.yaml"

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _ensure_category_dir(self, category: str) -> Path:
        cat_dir = self._base_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        return cat_dir

    def _validate_filename(self, filename: str) -> Path:
        """校验 filename 不含路径穿越字符，返回 Path 对象，非法则抛 ValueError。"""
        # P2: 增加 ":" 校验，阻断 Windows ADS（Alternate Data Stream）如 "a:stream" 绕过
        if "/" in filename or "\\" in filename or ".." in filename or ":" in filename:
            raise ValueError(f"Invalid filename: {filename!r}")
        p = Path(filename)
        if p.is_absolute():
            raise ValueError(f"Invalid filename: {filename!r}")
        if ".." in p.parts:
            raise ValueError(f"Invalid filename: {filename!r}")
        return p

    def route_entry(self, memory_type: str, filename: str) -> Path:
        """确定存储路径：命中类别则落到 ``base_dir/{memory_type}/{filename}``，否则回落到 base。"""
        p = self._validate_filename(filename)
        base_resolved = self._base_dir.resolve()
        if memory_type in CATEGORIES:
            cat_dir = self._ensure_category_dir(memory_type)
            target = (cat_dir / p.name).resolve()
            try:
                is_inside = target.is_relative_to(base_resolved)
            except AttributeError:
                # Python <3.9 fallback
                try:
                    target.relative_to(base_resolved)
                    is_inside = True
                except ValueError:
                    is_inside = False
            if not is_inside:
                raise ValueError(f"Path escapes base_dir: {target}")
            return target
        if memory_type:
            logger.warning("Unknown memory_type '%s', routing to base dir", memory_type)
        target = (self._base_dir / p.name).resolve()
        try:
            is_inside = target.is_relative_to(base_resolved)
        except AttributeError:
            try:
                target.relative_to(base_resolved)
                is_inside = True
            except ValueError:
                is_inside = False
        if not is_inside:
            raise ValueError(f"Path escapes base_dir: {target}")
        return target

    def recover_extensionless_entries(self) -> List[Path]:
        """修复无后缀的历史条目，符合 frontmatter 的补为 .md。"""
        recovered: List[Path] = []
        for category in CATEGORIES:
            cat_dir = self._base_dir / category
            if not cat_dir.is_dir():
                continue
            for item in sorted(cat_dir.iterdir()):
                if item.name in _SKIP_NAMES or not item.is_file():
                    continue
                if item.suffix:
                    continue
                target = item.with_suffix(".md")
                if target.exists():
                    logger.debug("skip recover %s: target exists %s", item, target)
                    continue
                try:
                    with item.open("r", encoding="utf-8") as f:
                        head = f.read(512).lstrip("\ufeff").lstrip()
                except OSError as e:
                    logger.warning("recover failed for %s: %s", item, e)
                    continue
                if not head.startswith("---"):
                    continue
                try:
                    item.rename(target)
                    recovered.append(target)
                except FileExistsError:
                    logger.debug("race: target appeared %s", target)
                    continue
                except OSError as e:
                    logger.warning("rename %s -> %s failed: %s", item, target, e)
                    continue
        return recovered

    def scan_all(self) -> List[Path]:
        """扫描 base 与各类别子目录下的 ``*.md``，跳过归档与系统文件。"""
        # NOTE: intentionally not calling recover_extensionless_entries() here
        # to keep scan read-only; callers should invoke recover explicitly if needed.
        # Retained comment for compatibility: previous implicit recover removed per Task21.
        results: List[Path] = []
        if self._base_dir.is_dir():
            for item in self._base_dir.iterdir():
                if item.name in _SKIP_NAMES:
                    continue
                if item.is_file() and item.suffix == ".md":
                    results.append(item)
        for category in CATEGORIES:
            cat_dir = self._base_dir / category
            if cat_dir.is_dir():
                for item in cat_dir.iterdir():
                    if item.is_file() and item.suffix == ".md":
                        results.append(item)
        results.sort(key=lambda p: p.name)
        return results

    def scan_category(self, category: str) -> List[Path]:
        """扫描指定类别目录下的记忆文件。"""
        cat_dir = self._base_dir / category
        results: List[Path] = []
        if not cat_dir.is_dir():
            return results
        for item in cat_dir.iterdir():
            if item.is_file() and item.suffix == ".md":
                results.append(item)
        results.sort(key=lambda p: p.name)
        return results

    def rebuild_index(self, entries: list) -> None:
        """按条目重建 ``.hierarchy.yaml`` 轻量索引。"""
        cat_data: Dict[str, CategorySummary] = {cat: CategorySummary() for cat in CATEGORIES}
        for entry in entries:
            mtype = entry.get("memory_type", "") if isinstance(entry, dict) else getattr(entry, "memory_type", "")
            if mtype not in cat_data:
                continue
            cat_data[mtype].count += 1
            keywords = entry.get("keywords", []) if isinstance(entry, dict) else []
            if isinstance(keywords, list):
                cat_data[mtype].keywords.extend(keywords)
        max_keywords = 10
        for summary in cat_data.values():
            seen: Set[str] = set()
            unique: List[str] = []
            for kw in summary.keywords:
                kw_lower = kw.lower().strip()
                if kw_lower and kw_lower not in seen:
                    seen.add(kw_lower)
                    unique.append(kw_lower)
            summary.keywords = unique[:max_keywords]
        rebuilt_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        data: Dict[str, object] = {
            "rebuilt_at": rebuilt_at,
            "categories": {cat: {"count": cat_data[cat].count, "keywords": cat_data[cat].keywords} for cat in CATEGORIES},
        }
        self._base_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._index_path.with_suffix(".tmp")
        # use tmp in same dir for atomicity; yaml.safe_dump ensures proper quoting/escaping
        tmp_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            with open(tmp_path, "rb") as _f:
                os.fsync(_f.fileno())
        except OSError as _e:
            logger.warning("hierarchy index fsync file failed: %s", _e)
        tmp_path.replace(self._index_path)
        # fsync directory to durably persist rename
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            _dfd = os.open(self._base_dir, flags)
            try:
                os.fsync(_dfd)
            finally:
                os.close(_dfd)
        except OSError as _e:
            logger.warning("hierarchy index fsync dir failed: %s", _e)

    def _parse_index_keywords(self) -> Dict[str, List[str]]:
        """解析索引中的类别关键词，失败则返回空。"""
        if not self._index_path.is_file():
            return {}
        try:
            text = self._index_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        # 首选用 yaml 解析，兼容安全转义与新旧格式
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            cats = data.get("categories")
            if isinstance(cats, dict):
                result: Dict[str, List[str]] = {}
                for cat in CATEGORIES:
                    entry = cats.get(cat)
                    if isinstance(entry, dict):
                        kws = entry.get("keywords", [])
                        if isinstance(kws, list):
                            result[cat] = [str(k).strip() for k in kws if str(k).strip()]
                        else:
                            result[cat] = []
                    else:
                        result[cat] = []
                # 仅当存在非空关键词时认为索引有效，避免旧空索引误判
                # 但保留至少一个非空列表时返回，否则保持旧行为回退 scan_all 需上层判断
                return result
        # 回退：兼容旧手写解析（inline [a, b]）
        result = {}
        current_cat: Optional[str] = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and not stripped.startswith("#") and not stripped.startswith("categories") and not stripped.startswith("rebuilt_at"):
                cat_name = stripped.rstrip(":")
                if cat_name in CATEGORIES:
                    current_cat = cat_name
                    if current_cat not in result:
                        result[current_cat] = []
                else:
                    current_cat = None
            elif stripped.startswith("keywords:") and current_cat:
                bracket_start = stripped.find("[")
                bracket_end = stripped.find("]")
                if bracket_start != -1 and bracket_end != -1:
                    inner = stripped[bracket_start + 1 : bracket_end]
                    keywords = [k.strip() for k in inner.split(",") if k.strip()]
                    result[current_cat] = keywords
                else:
                    # block 样式已由 yaml 解析处理，此处忽略
                    pass
        return result

    def prune_search_scope(self, query_tokens: Set[str], category_filter: str = "") -> List[Path]:
        """按 query token 与类别过滤缩小检索范围。"""
        if category_filter:
            if category_filter in CATEGORIES:
                return self.scan_category(category_filter)
            logger.warning("Unknown category_filter '%s', returning empty scope", category_filter)
            return []
        index_keywords = self._parse_index_keywords()
        if not index_keywords:
            return self.scan_all()
        scored: List[tuple] = []
        for cat in CATEGORIES:
            cat_kws = set(index_keywords.get(cat, []))
            overlap = len(query_tokens & cat_kws)
            scored.append((overlap, cat))
        scored.sort(key=lambda x: x[0], reverse=True)
        filtered_cats = [cat for score, cat in scored if score > 0]
        if not filtered_cats:
            # P2: 原先直接 return [] 会导致无关键词交集时召回率为 0；
            # 回落到全量扫描保证召回不丢失，上层可再做重排而非截断。
            logger.debug("no keyword overlap for tokens %s, falling back to scan_all", query_tokens)
            return self.scan_all()
        results: List[Path] = []
        seen: Set[Path] = set()
        for cat in filtered_cats:
            for p in self.scan_category(cat):
                if p not in seen:
                    results.append(p)
                    seen.add(p)
        return results

    def migrate_flat_entry(self, file_path: Path, memory_type: str) -> Optional[Path]:
        """将扁平历史文件迁移到对应类别子目录。"""
        if not file_path.is_file():
            return None
        # 校验文件名穿越
        try:
            self._validate_filename(file_path.name)
        except ValueError:
            logger.warning("migrate rejected invalid filename %s", file_path.name)
            return None
        # 校验 parent 在 base 内（resolve）
        try:
            if file_path.resolve().parent != self._base_dir.resolve():
                return None
        except OSError as e:
            logger.warning("migrate resolve failed for %s: %s", file_path, e)
            return None
        if memory_type not in CATEGORIES:
            return None
        dest_dir = self._ensure_category_dir(memory_type)
        dest_path = dest_dir / file_path.name
        # 校验 dest 不逃逸
        try:
            base_resolved = self._base_dir.resolve()
            dest_resolved = dest_path.resolve()
            try:
                is_inside = dest_resolved.is_relative_to(base_resolved)
            except AttributeError:
                try:
                    dest_resolved.relative_to(base_resolved)
                    is_inside = True
                except ValueError:
                    is_inside = False
            if not is_inside:
                logger.warning("migrate dest escapes base_dir: %s", dest_path)
                return None
        except OSError as e:
            logger.warning("migrate dest resolve failed: %s", e)
            return None
        if dest_path.exists():
            logger.debug("migrate skipped, dest exists: %s", dest_path)
            return None
        try:
            file_path.rename(dest_path)
            return dest_path
        except FileExistsError:
            logger.debug("migrate race, dest already exists: %s", dest_path)
            return None
        except OSError as e:
            logger.warning("migrate %s -> %s failed: %s", file_path, dest_path, e)
            return None
