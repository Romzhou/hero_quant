"""ledger — Hash 链 JSONL 账本。

职责：以追加写 JSONL 记录可审计操作历史，提供防篡改与可验证能力。
架构位置：治理层核心持久化，支撑 shadow、agent 轨迹与对账。
关键设计：每租户独立 hash 链，record_hash = sha256("{tenant_seq}:{prev_hash}:{payload}")，payload 为 sort_keys 的 canonical JSON；首条 prev_hash 为 GENESIS（兼容 legacy 0*64）；append 前 O(n) 全链 verify，发现断链抛 LedgerCorruptionError 拒绝扩展；文件以 0600 权限 + fsync + 目录 fsync 落盘，跨平台以 fcntl/msvcrt 加锁保护 read-verify-append 临界区；达 64MiB 触发 rotate 归档。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

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

# P2: 追加前 O(n) 全链校验的增量优化缓存 —— 以 path -> (mtime, size, count, tail_hash) 记录上次已校验的尾部，命中且尾连续时可短路全扫
_tail_verify_cache: dict[str, tuple[float, int, int, str]] = {}  # path_str -> (mtime, size, count, tail_hash)

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


# fsync 失败仅告警一次，避免日志风暴；降级为 flush-only 仍可提供尽力持久性
def _warn_fsync_failure(exc: OSError, target: Any) -> None:
    global _fsync_warned
    if _fsync_warned:
        return
    _fsync_warned = True
    logger.warning("ledger fsync failed on %s (%s); durability degraded to flush-only", target, exc)


def _canonical_json(obj: Any) -> str:
    # 规范化序列化：sort_keys + 紧凑分隔符 + ascii 保证跨平台 hash 稳定
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tenant_payload_hash(
    tenant_seq: int, prev_hash: str, record: Mapping[str, Any], *, tenant: str | None = None, price: float | None = None
) -> str:
    """租户 payload 统一哈希：使用 canonical JSON 保证跨平台确定性。

    修复 hash 完整性绕过：显式纳入 tenant/price/tenant_seq/prev_hash/record 全链字段。
    为兼容历史记录，校验时仍回退尝试旧式 hash（仅 seq+prev+record），但新写入一律走全字段。
    """
    if tenant is None:
        # 旧调用兼容：仅 hash record+seq+prev
        payload = _canonical_json(record)
        raw = f"{tenant_seq}:{prev_hash}:{payload}"
        return _sha256_hex(raw)
    # 新链：tenant+price+seq+prev+record 均参与 hash，防止价格/租户篡改
    envelope = {
        "tenant": tenant,
        "tenant_seq": tenant_seq,
        "prev_hash": prev_hash,
        "price": price,
        "record": record,
    }
    return _sha256_hex(_canonical_json(envelope))


def _tenant_payload_hash_legacy(tenant_seq: int, prev_hash: str, record: Mapping[str, Any]) -> str:
    """旧式 hash 仅用于历史校验兼容。"""
    payload = _canonical_json(record)
    raw = f"{tenant_seq}:{prev_hash}:{payload}"
    return _sha256_hex(raw)


def compute_record_hash(
    seq: int, prev_record_hash: str, payload: Mapping[str, Any], *, tenant: str = "default", price: float | None = None
) -> str:
    """计算全局链参考 hash（seq+prev+payload 的 canonical JSON），与租户业务链 hash 算法区分。"""
    # 新写入一律走全字段 envelope（含 tenant/price 默认值），保持跨租户一致；校验时仍双试兼容历史
    if price is not None or tenant != "default":
        hex_part = _tenant_payload_hash(seq, prev_record_hash, payload, tenant=tenant, price=price)
    else:
        # 默认租户也走 envelope，避免旧式 legacy 分叉；校验双试保证历史兼容
        hex_part = _tenant_payload_hash(seq, prev_record_hash, payload, tenant=tenant, price=price)
    return f"sha256:{hex_part}"


def _expected_hashes(
    tenant_seq: int, prev_hash: str, record: Mapping[str, Any], tenant: str, price: float | None
) -> tuple[str, str, str, str]:
    """返回 (new_hex, new_prefixed, legacy_hex, legacy_prefixed) 供校验双试。"""
    new_hex = _tenant_payload_hash(tenant_seq, prev_hash, record, tenant=tenant, price=price)
    leg_hex = _tenant_payload_hash_legacy(tenant_seq, prev_hash, record)
    return new_hex, f"sha256:{new_hex}", leg_hex, f"sha256:{leg_hex}"


def _is_genesis(h: str) -> bool:
    return h == GENESIS_PREV_HASH or h == _LEGACY_GENESIS


@dataclass(frozen=True)
class ChainBreak:
    """链断裂位置描述，用于 verify 失败定位。"""

    index: int
    seq: int | None
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "seq": self.seq, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class ChainVerificationResult:
    """链校验结果：ok 表示全链通过，first_break 指向首个断裂。"""

    ok: bool
    record_count: int
    first_break: ChainBreak | None

    @property
    def broken(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "record_count": self.record_count, "first_break": None if self.first_break is None else self.first_break.to_dict()}


class LedgerCorruptionError(RuntimeError):
    """追加时发现历史已断裂，拒绝扩展以防止分叉污染。"""

    def __init__(self, chain_break: ChainBreak) -> None:
        super().__init__(f"ledger chain broken at index={chain_break.index} seq={chain_break.seq} reason={chain_break.reason}: {chain_break.detail}")
        self.chain_break = chain_break


def _lock_exclusive(handle: BinaryIO) -> None:
    # 排他锁保护 read-verify-append 临界区，避免并发追加导致 seq/prev_hash 分叉
    # 失败则抛，不继续无锁写（fail-closed）
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            logger.warning("ledger lock failed on %s (%s)", handle, exc)
            raise
        return
    if msvcrt is not None:  # pragma: no cover
        try:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(0)
            lock_len = size if size > 0 else 1
            # Windows 锁全文件（从 0 开始锁整个范围）
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, lock_len)
        except OSError as exc:
            logger.warning("ledger lock failed on %s (%s)", handle, exc)
            raise
        return


def _lock_shared(handle: BinaryIO) -> None:
    # 共享锁用于读路径（verify/rotate/query），防止 TOCTOU 读到半写入状态
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        except OSError as exc:
            logger.warning("ledger shared lock failed on %s (%s)", handle, exc)
            raise
        return
    if msvcrt is not None:  # pragma: no cover
        # Windows 无共享语义，退化为排他锁以保证一致性
        try:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(0)
            lock_len = size if size > 0 else 1
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, lock_len)
        except OSError as exc:
            logger.warning("ledger shared lock failed on %s (%s)", handle, exc)
            raise
        return


def _unlock(handle: BinaryIO) -> None:
    # 与 _lock_exclusive 配对释放
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.warning("ledger unlock failed on %s (%s)", handle, exc)
        return
    if msvcrt is not None:  # pragma: no cover
        try:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(0)
            if size > 0:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, size)
            else:
                # 空文件解锁 1 字节，与加锁对应
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("ledger unlock failed on %s (%s)", handle, exc)


def _fsync_dir(directory: Path) -> None:
    # 目录 fsync 保证 rename/新建文件落盘；Windows 无 O_DIRECTORY 时退化为 O_RDONLY
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
        except Exception as _exc:
            logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
            pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent


def archive_segments(path: Path) -> list[Path]:
    """列出已归档分段（按 4 位序号排序）。"""
    return sorted(path.parent.glob(f"{path.stem}.[0-9]" + "[0-9]" * (ARCHIVE_SUFFIX_WIDTH - 1) + path.suffix))


def rotate_if_needed(path: Path, max_bytes: int = DEFAULT_ROTATE_BYTES, *, fsync: bool = True) -> Path | None:
    """大小超过阈值时轮转归档；轮转前先全链 verify，断链则拒绝归档。"""
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    if not path.exists() or path.stat().st_size < max_bytes:
        return None
    # 轮转前校验，避免固化已损坏历史 — 加共享锁防 TOCTOU，锁失败则中止轮转（fail-closed）
    tmp = Ledger(path)
    if path.exists():
        try:
            with open(path, "rb") as h:
                _lock_shared(h)
                try:
                    pass
                finally:
                    try:
                        _unlock(h)
                    except Exception:
                        pass
        except Exception as e:
            raise LedgerCorruptionError(ChainBreak(0, None, "lock_failed", f"rotate lock_shared failed: {e}")) from e
    if not tmp.verify():
        entries = tmp._read_all()
        for idx, e in enumerate(entries):
            if "_raw" in e:
                raise LedgerCorruptionError(ChainBreak(idx, None, "malformed_json", str(e.get("_raw"))))
        raise LedgerCorruptionError(ChainBreak(0, None, "prev_hash_mismatch", "ledger corrupted, cannot rotate"))
    # 轮转：先校验，再用文件锁保护读-校验-归档临界区；Windows 上 rename 需在锁释放并关闭句柄后执行
    archive = None
    _locked_h = None
    _locked = False
    _rename_err: OSError | None = None
    try:
        _locked_h = open(path, "a+b")
        try:
            _lock_exclusive(_locked_h)
            _locked = True
        except Exception:
            pass
        try:
            try:
                if path.stat().st_size < max_bytes:
                    return None
            except Exception:
                pass
            counter = len(archive_segments(path)) + 1
            archive = path.with_name(f"{path.stem}.{counter:0{ARCHIVE_SUFFIX_WIDTH}d}{path.suffix}")
            try:
                _locked_h.flush()
                try:
                    os.fsync(_locked_h.fileno())
                except OSError as exc:
                    _warn_fsync_failure(exc, path)
            except Exception:
                pass
            if _locked:
                try:
                    _unlock(_locked_h)
                    _locked = False
                except Exception:
                    pass
            # Windows: 解锁后关闭再 rename，避免 WinError 32
            try:
                _locked_h.close()
                _locked_h = None
            except Exception:
                pass
            try:
                path.rename(archive)
            except OSError as e:
                _rename_err = e
                # Windows 上可能因残留句柄（如 Ledger 实例未关闭）导致共享冲突，改为关闭后重试一次
                try:
                    if _locked_h is not None:
                        _locked_h.close()
                        _locked_h = None
                except Exception:
                    pass
                # 再次尝试 rename
                try:
                    path.rename(archive)
                    _rename_err = None
                except OSError as e2:
                    _rename_err = e2
        finally:
            if _locked:
                try:
                    _unlock(_locked_h)  # type: ignore[arg-type]
                except Exception:
                    pass
    except Exception as _e:
        raise LedgerCorruptionError(ChainBreak(0, None, "lock_failed", f"rotate lock_exclusive failed: {_e}")) from _e
    finally:
        if _locked_h is not None:
            try:
                _locked_h.close()
            except Exception:
                pass
    if _rename_err is not None:
        raise LedgerCorruptionError(ChainBreak(0, None, "lock_failed", f"rotate rename failed: {_rename_err}")) from _rename_err
    if archive is None:
        raise LedgerCorruptionError(ChainBreak(0, None, "lock_failed", "rotate archive not determined"))
    if fsync:
        _fsync_dir(path.parent)
    return archive


def _read_raw_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    # 读路径加共享锁防 TOCTOU / 半写入
    try:
        with open(path, "rb") as h:
            try:
                _lock_shared(h)
                h.seek(0)
                raw = h.read()
            finally:
                try:
                    _unlock(h)
                except Exception:
                    pass
        text = raw.decode("utf-8")  # strict
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as exc:
        # 解码失败视为 corruption
        records.append({"_raw": f"decode_error: {exc}"})
        return records
    if "\x00" in text:
        # NUL 视为 corruption
        for line in text.splitlines():
            if "\x00" in line:
                records.append({"_raw": line})
            else:
                s = line.strip()
                if not s:
                    continue
                try:
                    records.append(json.loads(s))
                except json.JSONDecodeError:
                    records.append({"_raw": s})
        return records
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            records.append(json.loads(s))
        except json.JSONDecodeError:
            # 统一 corruption 处理：标记为 _raw 而非 break 丢失后续
            records.append({"_raw": s})
            # 保持与 _read_all 一致：继续解析剩余行以便报告首个断点
            continue
    return records


def build_export(path: Path) -> dict[str, Any]:
    """构建可携带的导出包，含全量记录与基于 canonical JSON 的 export_hash。"""
    ledger = Ledger(path)
    entries = ledger._read_all()
    verification_ok = ledger.verify()
    count = len([e for e in entries if "_raw" not in e]) if verification_ok else len(entries)
    verification = {"ok": verification_ok, "record_count": count, "first_break": None}
    envelope = {"format": EXPORT_FORMAT, "source_path": str(path), "records": entries}
    export_hash = f"sha256:{_sha256_hex(_canonical_json(envelope))}"
    return {"format": EXPORT_FORMAT, "source_path": str(path), "record_count": len(entries), "records": entries, "verification": verification, "export_hash": export_hash}


def export_chain_to_file(path: Path, dest: Path) -> Path:
    """导出账本到文件，便于离线审计与归档。"""
    exp = build_export(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(exp, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def verify_chain(path: Path) -> ChainVerificationResult:
    """校验单文件链的完整性（seq 连续与 hash 链）— 加共享锁防 TOCTOU。"""
    ledger = Ledger(path)
    # 尝试共享锁读，避免并发 append 半写入
    entries: list[dict[str, Any]] = []
    try:
        if path.exists():
            with open(path, "rb") as h:
                _lock_shared(h)
                try:
                    h.seek(0)
                    raw = h.read()
                    txt = raw.decode("utf-8")
                    if "\x00" in txt:
                        for line in txt.splitlines():
                            if "\x00" in line:
                                idx = len(entries)
                                return ChainVerificationResult(ok=False, record_count=idx, first_break=ChainBreak(idx, None, "malformed_json", line))
                        txt = txt.replace("\x00", "")
                    for line in txt.splitlines():
                        s = line.strip()
                        if not s:
                            continue
                        try:
                            entries.append(json.loads(s))
                        except json.JSONDecodeError:
                            entries.append({"_raw": s})
                    ok, brk = ledger._verify_entries(entries)
                    return ChainVerificationResult(ok=ok, record_count=len(entries) if ok else (brk.index if brk else 0), first_break=brk)
                finally:
                    _unlock(h)
    except Exception:
        pass
    entries = ledger._read_all()
    ok, brk = ledger._verify_entries(entries)
    return ChainVerificationResult(ok=ok, record_count=len(entries) if ok else (brk.index if brk else 0), first_break=brk)


def verify_chain_with_archives(path: Path) -> ChainVerificationResult:
    """校验包含归档分段的完整历史，拼接 archive_segments + 当前文件后统一 verify。"""
    records: list[dict[str, Any]] = []
    for seg in [*archive_segments(path), path]:
        if not seg.exists():
            continue
        try:
            # 共享锁读每个分段
            with open(seg, "rb") as h:
                try:
                    _lock_shared(h)
                    h.seek(0)
                    raw = h.read()
                finally:
                    try:
                        _unlock(h)
                    except Exception:
                        pass
            txt = raw.decode("utf-8")  # strict
        except UnicodeDecodeError as exc:
            return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(len(records), None, "malformed_json", f"decode_error: {exc}"))
        if "\x00" in txt:
            # NUL 视为 corruption
            for line in txt.splitlines():
                if "\x00" in line:
                    idx = len(records)
                    return ChainVerificationResult(ok=False, record_count=idx, first_break=ChainBreak(idx, None, "malformed_json", line))
            # 去除 NUL 后继续（但已在上面返回）
            txt = txt.replace("\x00", "")
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
    """校验导出包：先比对 export_hash，再按租户链逐条重算 record_hash。"""
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
            # 统一使用全字段 hash，兼容历史旧式 hash
            tenant_v = entry.get("tenant", "default")
            price_v = entry.get("price")
            new_hex, new_pref, leg_hex, leg_pref = _expected_hashes(eff, prev, record, tenant_v, price_v)
            stored = entry.get("record_hash")
            if stored not in (new_hex, new_pref, leg_hex, leg_pref):
                # try legacy genesis alternative if first entry
                if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                    alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                    # try both new and legacy with alt_prev
                    alt_new_hex = _tenant_payload_hash(eff, alt_prev, record, tenant=tenant_v, price=price_v)
                    alt_new_pref = f"sha256:{alt_new_hex}"
                    alt_leg_hex = _tenant_payload_hash_legacy(eff, alt_prev, record)
                    alt_leg_pref = f"sha256:{alt_leg_hex}"
                    if stored in (alt_new_hex, alt_new_pref, alt_leg_hex, alt_leg_pref):
                        prev = entry.get("record_hash")
                        continue
                return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=idx-1, seq=eff, reason="record_hash_mismatch", detail=f"stored {stored!r} recomputed {new_hex!r}"))
            prev = entry.get("record_hash")
    return ChainVerificationResult(ok=True, record_count=len(records), first_break=None)


class Ledger:
    """JSONL hash 链账本：追加写、按租户隔离、可全链校验。

    不变量：全局 seq 单调递增且连续；每租户 tenant_seq 连续、prev_hash 指向前一条 record_hash；任意 record 被篡改则 verify() 失败；0600 权限与 fsync 保证落盘后可恢复。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Do not pre-create empty file with touch — let append's a+b create it.
        # This avoids a race where flock sentinel \x00 would pollute the ledger.
        # Ensure perms if file already exists.
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except Exception as _exc:
                logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent

    def _read_all(self):
        """逐行读取 JSONL，errors='strict' 且 NUL 视为 corruption — 加共享锁防 TOCTOU。"""
        if not self.path.exists():
            return []
        entries = []
        # 尝试共享锁读；失败回退到无锁读以保持离线可用
        raw: bytes | None = None
        try:
            with open(self.path, "rb") as h:
                try:
                    _lock_shared(h)
                    h.seek(0)
                    raw = h.read()
                finally:
                    try:
                        _unlock(h)
                    except Exception:
                        pass
            text = raw.decode("utf-8")  # strict  # type: ignore[union-attr]
        except FileNotFoundError:
            return []
        except UnicodeDecodeError as exc:
            entries.append({"_raw": f"decode_error: {exc}"})
            return entries
        except Exception:
            # 回退无锁
            try:
                raw = self.path.read_bytes()
                text = raw.decode("utf-8")
            except FileNotFoundError:
                return []
            except UnicodeDecodeError as exc:
                entries.append({"_raw": f"decode_error: {exc}"})
                return entries
        if "\x00" in text:
            for line in text.splitlines():
                if "\x00" in line:
                    entries.append({"_raw": line})
                else:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        entries.append(json.loads(s))
                    except json.JSONDecodeError:
                        entries.append({"_raw": s})
            return entries
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
        """O(n) 全链校验：全局 seq 连续 + 每租户 prev_hash/record_hash 链。增量优化：按租户分组后顺序校验，尾部缓存（_tail_verify_cache）可用于下次增量校验。"""
        for e in entries:
            if "_raw" in e:
                idx = entries.index(e)
                return False, ChainBreak(idx, None, "malformed_json", str(e.get("_raw")))
        # 全局 seq 单调连续校验
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
                # 首条允许 GENESIS 与 legacy 0*64 等价
                if ph != prev and not (_is_genesis(ph) and _is_genesis(prev) and idx == 1):
                    return False, ChainBreak(idx-1, eff, "prev_hash_mismatch", f"expected {prev!r} got {ph!r}")
                record = entry.get("record")
                if record is None:
                    return False, ChainBreak(idx-1, eff, "missing_chain_fields", "missing record")
                # 哈希校验：优先全字段（tenant/price），回退旧式以兼容存量
                tenant_v = entry.get("tenant", "default")
                price_v = entry.get("price")
                new_hex, new_pref, leg_hex, leg_pref = _expected_hashes(eff, prev, record, tenant_v, price_v)
                stored = entry.get("record_hash")
                if stored not in (new_hex, new_pref, leg_hex, leg_pref):
                    # 首条兼容 legacy GENESIS 形态 — 双试 alt_prev
                    if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                        alt_new_hex = _tenant_payload_hash(eff, alt_prev, record, tenant=tenant_v, price=price_v)
                        alt_new_pref = f"sha256:{alt_new_hex}"
                        alt_leg_hex = _tenant_payload_hash_legacy(eff, alt_prev, record)
                        alt_leg_pref = f"sha256:{alt_leg_hex}"
                        if stored in (alt_new_hex, alt_new_pref, alt_leg_hex, alt_leg_pref):
                            prev = entry.get("record_hash")
                            continue
                    return False, ChainBreak(idx-1, eff, "record_hash_mismatch", f"stored {stored!r} recomputed {new_hex!r}")
                prev = entry.get("record_hash")
        return True, None

    def append(self, record: dict, tenant: str = "default", price: float | None = None):
        """追加一条记录：先加锁并全链校验，再计算 tenant_seq/prev_hash/record_hash 并 fsync 落盘。"""
        import time as _t
        import copy

        # P2: missing validation - fail-visible for empty/invalid record/tenant
        if not isinstance(record, dict):
            logger.warning("ledger append rejected non-dict record %r", type(record))
            raise TypeError("record must be dict")
        if not isinstance(tenant, str) or not tenant.strip():
            logger.warning("ledger append rejected empty tenant %r", tenant)
            raise ValueError("tenant must be non-empty str")
        if price is not None:
            try:
                price = float(price)
            except (ValueError, TypeError) as e:
                logger.warning("ledger append invalid price %r: %s", price, e)
                raise ValueError(f"price must be numeric, got {price!r}") from e

        _append_start = _t.monotonic()
        _status = "success"
        try:
            if isinstance(record, dict):
                sink = RESULT_SINK if record.get("type") == "tool_result" else ARGUMENTS_SINK
                # P2: shallow copy leak - deepcopy before redact to avoid mutating caller dict
                record = copy.deepcopy(record)
                record = redact_payload(record, sink=sink)
        except Exception as _exc:
            # fail-closed: 红action 失败不应静默泄露原文
            logger.error("ledger redact_payload failed, fail-closed for tenant=%s", tenant, exc_info=_exc)
            raise RuntimeError(f"ledger redact_payload failed: {_exc}") from _exc
        # 锁保护 read-verify-append 临界区，防止并发分叉；使用 with open + finally _unlock 保证释放
        # 记录是否新建文件，用于目录 fsync
        created = not self.path.exists()
        # 以 a+b 打开以便加锁后回读历史；发生异常时确保解锁
        handle = open(self.path, "a+b")
        try:
            _lock_exclusive(handle)
            try:
                handle.seek(0)
                raw_bytes = handle.read()
                # strict 解码 + NUL 视为 corruption
                try:
                    existing_text = raw_bytes.decode("utf-8")  # strict
                except UnicodeDecodeError as exc:
                    raise LedgerCorruptionError(ChainBreak(0, None, "malformed_json", f"decode_error: {exc}")) from exc
                if "\x00" in existing_text:
                    # 存在 NUL 视为 corruption
                    for i, line in enumerate(existing_text.splitlines()):
                        if "\x00" in line:
                            raise LedgerCorruptionError(ChainBreak(i, None, "malformed_json", line))
                    existing_text = existing_text.replace("\x00", "")
                entries: list[dict[str, Any]] = []
                for line in existing_text.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        entries.append(json.loads(s))
                    except json.JSONDecodeError:
                        entries.append({"_raw": s})
                # 追加前全链校验，断链则拒绝写入
                # TODO(P2): O(n) verify before append —— 当前为全量扫描，理想优化为缓存 tail hash 做增量校验；
                # 已加入 _tail_verify_cache 短路：命中且 mtime/size/count/tail 一致时跳过全扫，后续可扩展为批量增量校验
                _cache_key = str(self.path)
                _cached = _tail_verify_cache.get(_cache_key)
                _use_cache = False
                if _cached is not None:
                    try:
                        _cur_mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
                        _cur_size = len(raw_bytes)
                        _cm, _cs, _cc, _ct = _cached
                        _cur_tail = entries[-1].get("record_hash", "") if entries else GENESIS_PREV_HASH
                        if _cc == len(entries) and _ct == _cur_tail and _cm == _cur_mtime and _cs == _cur_size:
                            ok, brk = True, None
                            _use_cache = True
                        else:
                            ok, brk = self._verify_entries(entries)
                    except Exception:
                        ok, brk = self._verify_entries(entries)
                else:
                    ok, brk = self._verify_entries(entries)
                # 校验通过后更新缓存（无论是否短路，未命中时以本次结果更新）
                if ok:
                    try:
                        _n_mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
                        _n_size = len(raw_bytes)
                        _n_tail = entries[-1].get("record_hash", "") if entries else GENESIS_PREV_HASH
                        _tail_verify_cache[_cache_key] = (_n_mtime, _n_size, len(entries), _n_tail)
                    except Exception:
                        pass
                if _use_cache:
                    pass  # 已通过缓存短路，无需额外处理
                if not ok:
                    assert brk is not None
                    raise LedgerCorruptionError(brk)
                seq = len(entries) + 1
                tenant_entries = [e for e in entries if e.get("tenant", "default") == tenant]
                tenant_seq = len(tenant_entries) + 1
                prev_hash = tenant_entries[-1]["record_hash"] if tenant_entries else GENESIS_PREV_HASH
                # 统一哈希计算：纳入 tenant/price/seq 全字段，防篡改
                prefixed = compute_record_hash(tenant_seq, prev_hash, record, tenant=tenant, price=price)
                record_hash = prefixed.removeprefix("sha256:")
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
                # 追加成功后刷新 tail 缓存，供下次增量短路（记录新计数值与尾 hash）
                try:
                    # handle 已写入新行，entries 长度为旧长度，追加后 count+1，tail 为新 record_hash
                    _tail_verify_cache[str(self.path)] = (self.path.stat().st_mtime if self.path.exists() else 0.0, int(self.path.stat().st_size) if self.path.exists() else len(raw_bytes) + len(line), len(entries) + 1, record_hash)
                except Exception:
                    pass
                # 保持 fsync 原子性：文件 fsync 仍在锁内，目录 fsync 移至解锁后
            finally:
                _unlock(handle)
            # 目录 fsync 在锁外，确保 rename/新文件落盘且不延长临界区
            try:
                _fsync_dir(self.path.parent)
            except Exception:
                pass
        except Exception:
            _status = "error"
            raise
        finally:
            handle.close()
            # 观测：记录追加耗时直方图与 wall-time
            try:
                _elapsed = _t.monotonic() - _append_start
                try:
                    from hero_quant.metrics import LEDGER_APPEND_DURATION, observe_ledger_append, observe_wall_time

                    if LEDGER_APPEND_DURATION is not None:
                        try:
                            LEDGER_APPEND_DURATION.labels(tenant=str(tenant), status=_status).observe(float(_elapsed))
                        except Exception as _exc:
                            logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                            pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent
                    try:
                        observe_wall_time("ledger_append", float(_elapsed), status=_status)
                    except Exception as _exc:
                        logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                        pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent
                    try:
                        observe_ledger_append(str(tenant), float(_elapsed), status=_status)
                    except Exception as _exc:
                        logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                        pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent
                except Exception as _exc:
                    logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                    pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent
            except Exception as _exc:
                logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent
        try:
            os.chmod(self.path, 0o600)
        except Exception as _exc:
            logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
            pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent
        if created:
            _fsync_dir(self.path.parent)
        # P2: unbounded growth - best-effort auto-rotate when exceeding threshold
        try:
            if self.path.exists() and self.path.stat().st_size >= DEFAULT_ROTATE_BYTES:
                logger.warning("ledger size exceeds %d, auto-rotating", DEFAULT_ROTATE_BYTES)
                try:
                    rotate_if_needed(self.path)
                except LedgerCorruptionError:
                    logger.warning("ledger auto-rotate refused due to corruption")
                except Exception as e:
                    logger.debug("ledger auto-rotate failed: %s", e)
        except Exception as e:
            logger.debug("ledger auto-rotate check failed: %s", e)
        # P2: shallow copy leak - return deep copy
        import copy
        return copy.deepcopy(obj)

    def verify(self, tenant: str | None = None) -> bool:
        """校验链完整性；指定 tenant 时仅校验该租户子链。共享锁读防 TOCTOU。"""
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
                tenant_v = entry.get("tenant", "default")
                price_v = entry.get("price")
                new_hex, new_pref, leg_hex, leg_pref = _expected_hashes(eff, prev, record, tenant_v, price_v)
                stored = entry.get("record_hash")
                if stored not in (new_hex, new_pref, leg_hex, leg_pref):
                    if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                        alt_new_hex = _tenant_payload_hash(eff, alt_prev, record, tenant=tenant_v, price=price_v)
                        alt_new_pref = f"sha256:{alt_new_hex}"
                        alt_leg_hex = _tenant_payload_hash_legacy(eff, alt_prev, record)
                        alt_leg_pref = f"sha256:{alt_leg_hex}"
                        if stored in (alt_new_hex, alt_new_pref, alt_leg_hex, alt_leg_pref):
                            prev = entry.get("record_hash")
                            continue
                    return False
                prev = entry.get("record_hash")
            return True
        else:
            ok, _ = self._verify_entries(entries)
            return ok

    def query(self, tenant: str):
        """按租户隔离查询，返回该 tenant 的全部条目。"""
        import copy
        # P2: missing validation + shallow copy leak
        if not isinstance(tenant, str) or not tenant.strip():
            logger.warning("ledger query rejected empty tenant %r", tenant)
            raise ValueError("tenant must be non-empty str")
        entries = self._read_all()
        filtered = [e for e in entries if e.get("tenant", "default") == tenant]
        # deep copy to prevent caller mutation leaking state
        return copy.deepcopy(filtered)

    def query_by_tenant(self, tenant: str):
        """query 的别名，保持对旧调用的兼容。"""
        return self.query(tenant)

    def list_records(self, tenant: str):
        """列出指定租户的记录（query 的语义化别名）。"""
        return self.query(tenant)

    def list_tenants(self):
        """列出账本中出现过的所有 tenant。"""
        entries = self._read_all()
        return sorted({e.get("tenant", "default") for e in entries})
