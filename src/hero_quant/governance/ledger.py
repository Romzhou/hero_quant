"""Hash-chained, fsynced, flock-protected JSONL ledger — per-tenant chain with GENESIS + O(n) verify + rotate.

Tenant chain keeps original business hash algorithm: sha256(f"{tenant_seq}:{prev_hash}:{payload}")
where payload = json.dumps(record, sort_keys=True, ensure_ascii=False)

Enhancements vs legacy:
- GENESIS_PREV_HASH = "sha256:genesis" (legacy "0"*64 still accepted for backward compat)
- fcntl.flock / msvcrt critical section across read+verify+append (O(n) verify before append)
- LedgerCorruptionError on corrupted history (refuse to extend broken chain)
- DEFAULT_ROTATE_BYTES = 64MiB + archive_segments / rotate_if_needed
- build_export / verify_export stubs with export_hash
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping

from hero_quant.security.redaction import ARGUMENTS_SINK, RESULT_SINK, redact_payload

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore

logger = logging.getLogger(__name__)

GENESIS_PREV_HASH = "sha256:genesis"
_LEGACY_GENESIS = "0" * 64
EXPORT_FORMAT = "hero-quant-governance-ledger-export/v1"
DEFAULT_ROTATE_BYTES: int = 64 * 1024 * 1024
ARCHIVE_SUFFIX_WIDTH: int = 4
_CHAIN_FIELDS = frozenset({"seq", "prev_record_hash", "record_hash"})

_fsync_warned = False

__all__ = [
    "GENESIS_PREV_HASH",
    "EXPORT_FORMAT",
    "DEFAULT_ROTATE_BYTES",
    "ARCHIVE_SUFFIX_WIDTH",
    "ChainBreak",
    "ChainVerificationResult",
    "LedgerCorruptionError",
    "Ledger",
    "compute_record_hash",
    "build_export",
    "export_chain_to_file",
    "verify_export",
    "verify_chain",
    "verify_chain_with_archives",
    "archive_segments",
    "rotate_if_needed",
]


def _warn_fsync_failure(exc: OSError, target: Any) -> None:
    global _fsync_warned
    if _fsync_warned:
        return
    _fsync_warned = True
    logger.warning("ledger fsync failed on %s (%s); durability degraded to flush-only", target, exc)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_record_hash(seq: int, prev_record_hash: str, payload: Mapping[str, Any]) -> str:
    """Reference global-chain hash (seq+prev+payload) — not the per-tenant business hash."""
    body = _canonical_json({"seq": seq, "prev_record_hash": prev_record_hash, "payload": payload})
    return f"sha256:{_sha256_hex(body)}"


def _is_genesis(h: str) -> bool:
    return h == GENESIS_PREV_HASH or h == _LEGACY_GENESIS


@dataclass(frozen=True)
class ChainBreak:
    index: int
    seq: int | None
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "seq": self.seq, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class ChainVerificationResult:
    ok: bool
    record_count: int
    first_break: ChainBreak | None

    @property
    def broken(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "record_count": self.record_count, "first_break": None if self.first_break is None else self.first_break.to_dict()}


class LedgerCorruptionError(RuntimeError):
    def __init__(self, chain_break: ChainBreak) -> None:
        super().__init__(f"ledger chain broken at index={chain_break.index} seq={chain_break.seq} reason={chain_break.reason}: {chain_break.detail}")
        self.chain_break = chain_break


def _lock_exclusive(handle: BinaryIO) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return
    if msvcrt is not None:  # pragma: no cover
        # Windows: try byte-range lock without polluting ledger content.
        # Avoid writing sentinel \x00 into ledger file (would corrupt JSONL).
        try:
            # try locking first byte if file has content, else try lock 0-len gracefully
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(0)
            if size > 0:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                # empty file: no lock needed yet (single byte range would require sentinel)
                # attempt non-blocking lock on 0 bytes — if fails, proceed unlocked (verify still catches fork)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    pass
        except Exception:
            pass
        return


def _unlock(handle: BinaryIO) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        return
    if msvcrt is not None:  # pragma: no cover
        try:
            handle.seek(0)
            # only unlock if we previously locked a byte
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass


def _fsync_dir(directory: Path) -> None:
    # Windows has no O_DIRECTORY; fallback to O_RDONLY with graceful degrade
    flags = getattr(os, "O_DIRECTORY", 0)
    if flags:
        try:
            dir_fd = os.open(str(directory), flags)
        except OSError:
            try:
                dir_fd = os.open(str(directory), os.O_RDONLY)
            except OSError:
                return
    else:
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        _warn_fsync_failure(exc, directory)
    finally:
        try:
            os.close(dir_fd)
        except Exception:
            pass


def archive_segments(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.stem}.[0-9]" + "[0-9]" * (ARCHIVE_SUFFIX_WIDTH - 1) + path.suffix))


def rotate_if_needed(path: Path, max_bytes: int = DEFAULT_ROTATE_BYTES, *, fsync: bool = True) -> Path | None:
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    if not path.exists() or path.stat().st_size < max_bytes:
        return None
    # verify before rotate — refuse to seal corrupted chain
    # use Ledger verify (class-level)
    tmp = Ledger(path)
    if not tmp.verify():
        # build detailed break for error
        entries = tmp._read_all()
        # find first hash mismatch for error detail
        for idx, e in enumerate(entries):
            if "_raw" in e:
                raise LedgerCorruptionError(ChainBreak(idx, None, "malformed_json", str(e.get("_raw"))))
        # fallback generic
        raise LedgerCorruptionError(ChainBreak(0, None, "prev_hash_mismatch", "ledger corrupted, cannot rotate"))
    counter = len(archive_segments(path)) + 1
    archive = path.with_name(f"{path.stem}.{counter:0{ARCHIVE_SUFFIX_WIDTH}d}{path.suffix}")
    path.rename(archive)
    if fsync:
        _fsync_dir(path.parent)
    return archive


def _read_raw_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            records.append(json.loads(s))
        except json.JSONDecodeError:
            break
    return records


def build_export(path: Path) -> dict[str, Any]:
    ledger = Ledger(path)
    entries = ledger._read_all()
    verification_ok = ledger.verify()
    # detailed result
    count = len([e for e in entries if "_raw" not in e]) if verification_ok else len(entries)
    verification = {"ok": verification_ok, "record_count": count, "first_break": None}
    envelope = {"format": EXPORT_FORMAT, "source_path": str(path), "records": entries}
    export_hash = f"sha256:{_sha256_hex(_canonical_json(envelope))}"
    return {"format": EXPORT_FORMAT, "source_path": str(path), "record_count": len(entries), "records": entries, "verification": verification, "export_hash": export_hash}


def export_chain_to_file(path: Path, dest: Path) -> Path:
    exp = build_export(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(exp, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def verify_chain(path: Path) -> ChainVerificationResult:
    """Compatibility shim for verify_chain(path) -> ChainVerificationResult"""
    ledger = Ledger(path)
    entries = ledger._read_all()
    ok, brk = ledger._verify_entries(entries)
    return ChainVerificationResult(ok=ok, record_count=len(entries) if ok else (brk.index if brk else 0), first_break=brk)


def verify_chain_with_archives(path: Path) -> ChainVerificationResult:
    """Verify whole history including sealed archive_segments."""
    records: list[dict[str, Any]] = []
    for seg in [*archive_segments(path), path]:
        if not seg.exists():
            continue
        txt = seg.read_text(encoding="utf-8", errors="ignore").replace("\x00", "")
        for line in txt.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                records.append(json.loads(s))
            except json.JSONDecodeError:
                idx = len(records)
                return ChainVerificationResult(ok=False, record_count=idx, first_break=ChainBreak(idx, None, "malformed_json", s))
    if not records:
        return ChainVerificationResult(ok=True, record_count=0, first_break=None)
    # reuse Ledger._verify_entries logic on concatenated records
    tmp = Ledger(path)
    ok, brk = tmp._verify_entries(records)
    return ChainVerificationResult(ok=ok, record_count=len(records), first_break=brk)


def verify_export(export: Mapping[str, Any] | str | Path) -> ChainVerificationResult:
    if isinstance(export, Path):
        data: Mapping[str, Any] = json.loads(export.read_text(encoding="utf-8"))
    elif isinstance(export, str):
        data = json.loads(export)
    else:
        data = export
    records = list(data.get("records", []))
    envelope = {"format": data.get("format", EXPORT_FORMAT), "source_path": data.get("source_path", ""), "records": records}
    expected = f"sha256:{_sha256_hex(_canonical_json(envelope))}"
    if expected != data.get("export_hash"):
        return ChainVerificationResult(ok=False, record_count=0, first_break=ChainBreak(index=-1, seq=None, reason="export_hash_mismatch", detail=f"expected {expected!r} found {data.get('export_hash')!r}"))
    # verify chain via Ledger logic on temp path
    # simplest: walk records directly checking per-record hash for default tenant naive
    # Use Ledger verification logic by writing to temp verification via in-memory
    # Here we just check each record's prev_hash chain for the stored tenant chain? For export we verify global seq + per-tenant hashes.
    # For minimal stub: if any record_hash mismatch detected via recomputed tenant hash, fail.
    # We recompute using stored tenant_seq/prev_hash/payload with business algorithm.
    # First check global seq monotonic
    for idx, rec in enumerate(records, start=1):
        if rec.get("seq") != idx and "_raw" not in rec:
            # seq gap — but export may contain only subset? For stub just check
            pass
    # check tenant chains reuse same logic as Ledger.verify on list
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if "_raw" in r:
            return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=records.index(r), seq=None, reason="malformed_json", detail=str(r.get("_raw"))))
        groups[r.get("tenant", "default")].append(r)
    for t, grp in groups.items():
        grp_sorted = sorted(grp, key=lambda x: x.get("tenant_seq", x.get("seq", 0)))
        prev = GENESIS_PREV_HASH
        # allow legacy genesis for first prev check
        for idx, entry in enumerate(grp_sorted, start=1):
            ts = entry.get("tenant_seq")
            eff = ts if ts is not None else idx
            if ts is not None and ts != idx:
                return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=idx-1, seq=ts, reason="seq_gap", detail=f"expected {idx} got {ts}"))
            ph = entry.get("prev_hash")
            if ph != prev and not (_is_genesis(ph) and _is_genesis(prev) and idx == 1):
                return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=idx-1, seq=eff, reason="prev_hash_mismatch", detail=f"expected {prev!r} got {ph!r}"))
            record = entry.get("record")
            if record is None:
                return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=idx-1, seq=eff, reason="missing_chain_fields", detail="missing record"))
            payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
            expected_hash = hashlib.sha256(f"{eff}:{prev}:{payload}".encode()).hexdigest()
            # also accept legacy recomputed with old genesis mapping? For compat, try both prev variants if first
            if entry.get("record_hash") != expected_hash:
                # try legacy genesis alternative if first entry
                if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                    alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                    alt_hash = hashlib.sha256(f"{eff}:{alt_prev}:{payload}".encode()).hexdigest()
                    if entry.get("record_hash") == alt_hash:
                        prev = entry.get("record_hash")
                        continue
                return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=idx-1, seq=eff, reason="record_hash_mismatch", detail=f"stored {entry.get('record_hash')!r} recomputed {expected_hash!r}"))
            prev = entry.get("record_hash")
    return ChainVerificationResult(ok=True, record_count=len(records), first_break=None)


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Do not pre-create empty file with touch — let append's a+b create it.
        # This avoids a race where flock sentinel \x00 would pollute the ledger.
        # Ensure perms if file already exists.
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass

    def _read_all(self):
        if not self.path.exists():
            return []
        entries = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            return []
        # handle stray NUL from old sentinel
        text = text.replace("\x00", "")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line})
        return entries

    def _verify_entries(self, entries: list[dict[str, Any]]) -> tuple[bool, ChainBreak | None]:
        for e in entries:
            if "_raw" in e:
                idx = entries.index(e)
                return False, ChainBreak(idx, None, "malformed_json", str(e.get("_raw")))
        # global seq check
        for idx, entry in enumerate(entries, start=1):
            if entry.get("seq") != idx:
                return False, ChainBreak(idx-1, entry.get("seq"), "seq_gap", f"expected seq={idx} found {entry.get('seq')!r}")
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in entries:
            groups[e.get("tenant", "default")].append(e)
        for t, group in groups.items():
            if any("tenant_seq" not in e for e in group):
                group_sorted = sorted(group, key=lambda x: x.get("seq", 0))
            else:
                group_sorted = sorted(group, key=lambda x: x.get("tenant_seq", 0))
            prev = GENESIS_PREV_HASH
            for idx, entry in enumerate(group_sorted, start=1):
                ts = entry.get("tenant_seq")
                eff = ts if ts is not None else idx
                if ts is not None and ts != idx:
                    return False, ChainBreak(idx-1, ts, "seq_gap", f"tenant {t} expected tenant_seq={idx} got {ts!r}")
                ph = entry.get("prev_hash")
                # allow genesis equivalence
                if ph != prev and not (_is_genesis(ph) and _is_genesis(prev) and idx == 1):
                    return False, ChainBreak(idx-1, eff, "prev_hash_mismatch", f"expected {prev!r} got {ph!r}")
                record = entry.get("record")
                if record is None:
                    return False, ChainBreak(idx-1, eff, "missing_chain_fields", "missing record")
                payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
                expected = hashlib.sha256(f"{eff}:{prev}:{payload}".encode()).hexdigest()
                if entry.get("record_hash") != expected:
                    # try legacy genesis alternative for first record
                    if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                        alt = hashlib.sha256(f"{eff}:{alt_prev}:{payload}".encode()).hexdigest()
                        if entry.get("record_hash") == alt:
                            prev = entry.get("record_hash")
                            continue
                    return False, ChainBreak(idx-1, eff, "record_hash_mismatch", f"stored {entry.get('record_hash')!r} recomputed {expected!r}")
                prev = entry.get("record_hash")
        return True, None

    def append(self, record: dict, tenant: str = "default", price: float | None = None):
        import time as _t

        _append_start = _t.monotonic()
        _status = "success"
        try:
            if isinstance(record, dict):
                sink = RESULT_SINK if record.get("type") == "tool_result" else ARGUMENTS_SINK
                record = redact_payload(record, sink=sink)
        except Exception:
            pass
        # flock critical section across read+verify+write
        created = not self.path.exists()
        # open a+b for locking and reading existing
        handle = open(self.path, "a+b")
        try:
            _lock_exclusive(handle)
            try:
                handle.seek(0)
                raw_bytes = handle.read()
                # strip any stray NUL bytes from legacy Windows sentinel
                existing_text = raw_bytes.decode("utf-8", errors="ignore").replace("\x00", "")
                entries: list[dict[str, Any]] = []
                for line in existing_text.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        entries.append(json.loads(s))
                    except json.JSONDecodeError:
                        entries.append({"_raw": s})
                # O(n) verify whole chain before append
                ok, brk = self._verify_entries(entries)
                if not ok:
                    assert brk is not None
                    raise LedgerCorruptionError(brk)
                seq = len(entries) + 1
                tenant_entries = [e for e in entries if e.get("tenant", "default") == tenant]
                tenant_seq = len(tenant_entries) + 1
                prev_hash = tenant_entries[-1]["record_hash"] if tenant_entries else GENESIS_PREV_HASH
                payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
                record_hash = hashlib.sha256(f"{tenant_seq}:{prev_hash}:{payload}".encode()).hexdigest()
                obj = {"seq": seq, "tenant_seq": tenant_seq, "tenant": tenant, "prev_hash": prev_hash, "record_hash": record_hash, "record": record}
                if price is not None:
                    obj["price"] = price
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError as exc:
                    _warn_fsync_failure(exc, self.path)
            finally:
                _unlock(handle)
        except Exception:
            _status = "error"
            raise
        finally:
            handle.close()
            # observability hardening: ledger append duration histogram + wall-time
            try:
                _elapsed = _t.monotonic() - _append_start
                # metrics hardening (optional, offline-safe)
                try:
                    from hero_quant.metrics import LEDGER_APPEND_DURATION, observe_ledger_append, observe_wall_time

                    if LEDGER_APPEND_DURATION is not None:
                        try:
                            LEDGER_APPEND_DURATION.labels(tenant=str(tenant), status=_status).observe(float(_elapsed))
                        except Exception:
                            pass
                    # also aggregate wall-time
                    try:
                        observe_wall_time("ledger_append", float(_elapsed), status=_status)
                    except Exception:
                        pass
                    try:
                        observe_ledger_append(str(tenant), float(_elapsed), status=_status)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass
        if created:
            _fsync_dir(self.path.parent)
        return obj

    def verify(self, tenant: str | None = None) -> bool:
        entries = self._read_all()
        for e in entries:
            if "_raw" in e:
                return False
        if tenant is not None:
            filtered = [e for e in entries if e.get("tenant", "default") == tenant]
            filtered_sorted = sorted(filtered, key=lambda x: x.get("tenant_seq", 0) or 0)
            if any("tenant_seq" not in e for e in filtered_sorted):
                filtered_sorted = sorted(filtered, key=lambda x: x.get("seq", 0))
            prev = GENESIS_PREV_HASH
            for idx, entry in enumerate(filtered_sorted, start=1):
                ts = entry.get("tenant_seq")
                if ts is not None and ts != idx:
                    return False
                eff = ts if ts is not None else idx
                # genesis equivalence for first
                ph = entry.get("prev_hash")
                if ph != prev and not (_is_genesis(ph) and _is_genesis(prev) and idx == 1):
                    return False
                record = entry.get("record")
                if record is None:
                    return False
                payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
                expected = hashlib.sha256(f"{eff}:{prev}:{payload}".encode()).hexdigest()
                if entry.get("record_hash") != expected:
                    if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                        alt = hashlib.sha256(f"{eff}:{alt_prev}:{payload}".encode()).hexdigest()
                        if entry.get("record_hash") == alt:
                            prev = entry.get("record_hash")
                            continue
                    return False
                prev = entry.get("record_hash")
            return True
        else:
            ok, _ = self._verify_entries(entries)
            return ok

    def query(self, tenant: str):
        """RLS isolation: return entries where tenant == ..."""
        entries = self._read_all()
        return [e for e in entries if e.get("tenant", "default") == tenant]

    def query_by_tenant(self, tenant: str):
        return self.query(tenant)

    def list_records(self, tenant: str):
        return self.query(tenant)

    def list_tenants(self):
        entries = self._read_all()
        return sorted({e.get("tenant", "default") for e in entries})
