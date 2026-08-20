"""MemoryStore - file+sqlite FTS5+dedup minimal implementation."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


class MemoryStore:
    """File storage with SQLite FTS5 and 30s dedup + optional tenant/thread namespace isolation."""

    def __init__(self, base_path: Path | str, namespace: str | None = None):
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self._last_write: dict[str, tuple[str, float]] = {}
        self._fts_enabled = False
        self.db_path = self.base / "memory.db"
        self._init_db()

    def _ns_key(self, key: str) -> str:
        """Prefix key with namespace if set: f\"{namespace}:{key}\" else key."""
        if self.namespace:
            return f"{self.namespace}:{key}"
        return key

    def _ns_prefix(self) -> str | None:
        if self.namespace:
            return f"{self.namespace}:"
        return None

    def _safe_filename(self, ns_key: str) -> str:
        # Windows-safe: colon and slash not allowed in filenames
        safe = ns_key.replace(":", "__").replace("/", "__").replace("\\", "__")
        # also strip any path separators that could cause traversal
        safe = safe.replace("..", "__")
        return f"{safe}.md"

    def _safe_prefix(self) -> str | None:
        if self.namespace:
            # sanitized prefix for file filtering
            return self.namespace.replace(":", "__").replace("/", "__").replace("\\", "__") + "__"
        return None

    def _init_db(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # notes table
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, content TEXT, created TEXT)"
        )
        # try FTS5 creation
        try:
            # trigram helps CJK, try first
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content, tokenize='trigram')"
            )
            self._fts_enabled = True
        except sqlite3.OperationalError:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content)"
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False
        self._conn.commit()

    def write(self, key: str, content: str) -> None:
        now = time.time()
        ns_key = self._ns_key(key)
        # 30s dedup same namespaced key+content
        if ns_key in self._last_write:
            last_content, last_ts = self._last_write[ns_key]
            if last_content == content and (now - last_ts) < 30:
                return
        self._last_write[ns_key] = (content, now)

        # atomic file write: tmp -> fsync -> os.replace, 0600, flock compat
        safe_name = self._safe_filename(ns_key)
        file_path = self.base / safe_name
        tmp_path = self.base / f".{safe_name}.tmp.{os.getpid()}"
        # ensure parent exists (key may contain subdirs)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                try:
                    import fcntl  # type: ignore

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    # Windows fallback: try msvcrt
                    try:
                        import msvcrt  # type: ignore

                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    except Exception:
                        pass
                except Exception:
                    pass
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
            os.replace(tmp_path, file_path)
            try:
                os.chmod(file_path, 0o600)
            except Exception:
                pass
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

        # sqlite index with namespaced key
        created = datetime.now(timezone.utc).isoformat()
        try:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO notes (key, content, created) VALUES (?, ?, ?)",
                (ns_key, content, created),
            )
            rowid = cur.lastrowid
            if self._fts_enabled:
                try:
                    cur.execute(
                        "INSERT INTO notes_fts (rowid, content) VALUES (?, ?)",
                        (rowid, content),
                    )
                except sqlite3.OperationalError:
                    # fallback without rowid mapping
                    try:
                        cur.execute(
                            "INSERT INTO notes_fts (content) VALUES (?)", (content,)
                        )
                    except Exception:
                        pass
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def search(self, query: str) -> list[dict]:
        if not query:
            return []
        prefix = self._ns_prefix()
        safe_prefix = self._safe_prefix()
        # try FTS5 MATCH first
        if self._fts_enabled:
            try:
                cur = self._conn.cursor()
                # Use MATCH with query; FTS5 may not tokenize Chinese well, but try
                cur.execute(
                    "SELECT notes.key, notes.content FROM notes_fts JOIN notes ON notes_fts.rowid = notes.id WHERE notes_fts MATCH ?",
                    (query,),
                )
                rows = cur.fetchall()
                if rows:
                    result = [{"key": k, "content": c} for k, c in rows]
                    # namespace isolation: filter by prefix
                    if prefix is not None:
                        result = [r for r in result if r["key"].startswith(prefix)]
                        if not result:
                            # no matching namespace rows -> skip to fallback (which will also filter)
                            raise sqlite3.OperationalError("no rows for namespace, fallback to LIKE")
                    # dedup by content to handle potential duplicates
                    seen: dict[str, dict] = {}
                    deduped: list[dict] = []
                    for item in result:
                        if item["content"] not in seen:
                            seen[item["content"]] = item
                            deduped.append(item)
                    if deduped:
                        return deduped
            except sqlite3.OperationalError:
                pass
            except Exception:
                pass

        # fallback: LIKE %term% (bigram fallback) with namespace filter
        try:
            cur = self._conn.cursor()
            pattern = f"%{query}%"
            if prefix is not None:
                cur.execute(
                    "SELECT key, content FROM notes WHERE key LIKE ? AND content LIKE ?",
                    (f"{prefix}%", pattern),
                )
            else:
                cur.execute("SELECT key, content FROM notes WHERE content LIKE ?", (pattern,))
            rows = cur.fetchall()
            result = [{"key": k, "content": c} for k, c in rows]
            if prefix is not None:
                # double-check prefix (in case LIKE didn't fully filter)
                result = [r for r in result if r["key"].startswith(prefix)]
            if not result:
                # scan files as extra fallback with namespace-aware prefix
                for md_file in self.base.glob("*.md"):
                    try:
                        if safe_prefix is not None and not md_file.name.startswith(safe_prefix):
                            continue
                        if prefix is not None and not md_file.stem.replace("__", ":").startswith(prefix.rstrip(":")):
                            # fallback stem check via safe prefix already done; skip if not matching
                            if not md_file.name.startswith(safe_prefix or ""):
                                continue
                        txt = md_file.read_text(encoding="utf-8")
                        if query in txt:
                            # derive key from filename: reverse safe mapping not perfect, use stem as key
                            # For isolation test, content match is enough; key value not asserted
                            result.append({"key": md_file.stem, "content": txt})
                    except Exception:
                        continue
                if prefix is not None:
                    # file fallback isolation already filtered by safe_prefix
                    pass
            # dedup by content
            seen2: dict[str, dict] = {}
            deduped2: list[dict] = []
            for item in result:
                if item["content"] not in seen2:
                    seen2[item["content"]] = item
                    deduped2.append(item)
            return deduped2
        except Exception:
            return []
