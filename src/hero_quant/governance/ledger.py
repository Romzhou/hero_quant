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

# T2-2: legacy豁免告警计数（进程内单调，用于可观测；verify命中旧式hash时+1并warning）
LEGACY_HASH_WARNING = "legacy hash accepted via explicit exemption"

GENESIS_PREV_HASH = "sha256:genesis"
_LEGACY_GENESIS = "0" * 64
EXPORT_FORMAT = "hero-quant-governance-ledger-export/v1"
DEFAULT_ROTATE_BYTES: int = 64 * 1024 * 1024
ARCHIVE_SUFFIX_WIDTH: int = 4
# T2-2: 新链 hash 版本标记；verify 默认仅接受新式 envelope hash（tenant/price 全字段），
# 旧式 hash 须调用方显式 allow_legacy=True 豁免（历史区间）并记 warning 告警。
HASH_VERSION: int = 2
_CHAIN_FIELDS = frozenset({"seq", "tenant_seq", "tenant", "prev_hash", "record_hash", "record"})

_fsync_warned = False

# PR2-E: 追加前校验的增量缓存 —— path -> (content_sha256, size, count, tail_hash, tenants)，tenants 为 {tenant: [count, tail_hash]}；
# 命中（内容哈希一致 + O(1) 尾自检）跳过全扫；内容变化时必须先证明前缀字节未变（append-only）才可仅验新增段，否则回落全扫
_tail_verify_cache: dict[str, tuple[float, int, int, str, dict[str, list]]] = {}

__all__ = [
    "GENESIS_PREV_HASH",
    "EXPORT_FORMAT",
    "DEFAULT_ROTATE_BYTES",
    "ARCHIVE_SUFFIX_WIDTH",
    "HASH_VERSION",
    "LEGACY_HASH_WARNING",
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
    # 中文：新写入一律走全字段 envelope（含 tenant/price 默认值），单路径无分支
    hex_part = _tenant_payload_hash(seq, prev_record_hash, payload, tenant=tenant, price=price)
    return f"sha256:{hex_part}"


def _check_hash(
    stored: Any, tenant_seq: int, prev_hash: str, record: Mapping[str, Any], tenant: str, price: float | None, *, allow_legacy: bool
) -> tuple[bool, bool]:
    """T2-2: 按版本严格匹配。新链（allow_legacy=False）仅接受全字段 envelope hash；
    allow_legacy=True 时旧式 hash 亦可（历史区间显式豁免，命中记 warning）。返回 (ok, used_legacy)。"""
    new_hex = _tenant_payload_hash(tenant_seq, prev_hash, record, tenant=tenant, price=price)
    if stored in (new_hex, f"sha256:{new_hex}"):
        return True, False
    if allow_legacy:
        leg_hex = _tenant_payload_hash_legacy(tenant_seq, prev_hash, record)
        if stored in (leg_hex, f"sha256:{leg_hex}"):
            logger.warning("%s tenant=%r tenant_seq=%r", LEGACY_HASH_WARNING, tenant, tenant_seq)
            return True, True
    return False, False


def _check_hash_alt_genesis(
    stored: Any, tenant_seq: int, alt_prev: str, record: Mapping[str, Any], tenant: str, price: float | None, *, allow_legacy: bool
) -> bool:
    """T2-2: 首条 GENESIS/legacy-genesis 等价形态的严格版本匹配。"""
    alt_new_hex = _tenant_payload_hash(tenant_seq, alt_prev, record, tenant=tenant, price=price)
    if stored in (alt_new_hex, f"sha256:{alt_new_hex}"):
        return True
    if allow_legacy:
        alt_leg_hex = _tenant_payload_hash_legacy(tenant_seq, alt_prev, record)
        if stored in (alt_leg_hex, f"sha256:{alt_leg_hex}"):
            logger.warning("%s tenant=%r tenant_seq=%r (alt-genesis)", LEGACY_HASH_WARNING, tenant, tenant_seq)
            return True
    return False


def _expected_hashes(
    tenant_seq: int, prev_hash: str, record: Mapping[str, Any], tenant: str, price: float | None
) -> tuple[str, str, str, str]:
    """返回 (new_hex, new_prefixed, legacy_hex, legacy_prefixed) 供校验双试（遗留调用兼容；新路径经 _check_hash 严格匹配）。"""
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


class LedgerCorruptionError(RuntimeError, ValueError):
    """追加/对账时发现历史已断裂，拒绝扩展与误导性 zero_diff 以防止分叉污染。

    中文：T2-2 同时继承 ValueError——伪造/坏 JSONL 路径旧调用方多按 ValueError 捕获（如坏行 fail-closed），
    新调用方按 LedgerCorruptionError 捕获；双重身份保持两边兼容。
    """

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
            # 中文：记住加锁长度，解锁必须用同一区域（文件在锁内变大后重算会 mismatch）
            try:
                handle._ledger_lock_len = lock_len  # type: ignore[attr-defined]
            except Exception:
                pass
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
            try:
                handle._ledger_lock_len = lock_len  # type: ignore[attr-defined]
            except Exception:
                pass
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
            # 中文：优先用加锁时记录的长度；回退重算（只读路径未改文件时一致）
            lock_len = getattr(handle, "_ledger_lock_len", None)
            if lock_len is None:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(0)
                lock_len = size if size > 0 else 1
            else:
                try:
                    handle.seek(0)
                except Exception:
                    pass
            if lock_len > 0:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, lock_len)
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


def _create_secure(path: Path):
    """T2-2: 原子安全创建（O_CREAT|O_EXCL, 0o600）消除 0644 窗口；已存在则返回 None。

    中文：创建后 chmod 有 TOCTOU 窗口（umask 022 下短暂 0644 可读）；必须 os.open 原子指定 mode。
    Windows 下 chmod 无 ACL 意义，此处额外经 os.open 指定权限 + 后续 chmod best-effort 收紧。
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    except OSError:
        return None
    try:
        try:
            os.fchmod(fd, 0o600)
        except (OSError, AttributeError):
            pass
        f = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    try:
        f.write("")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError as exc:
            _warn_fsync_failure(exc, path)
    finally:
        try:
            f.close()
        except Exception:
            pass
    try:
        _fsync_dir(path.parent)
    except Exception:
        pass
    return path


def rotate_if_needed(path: Path, max_bytes: int = DEFAULT_ROTATE_BYTES, *, fsync: bool = True, allow_legacy: bool = False) -> Path | None:
    """大小超过阈值时轮转归档；轮转前先全链 verify，断链则拒绝归档。

    中文：T2-2 全程持排他锁（verify→rename 不提前解锁），消除并发 append 丢失窗口；默认拒绝旧式 hash。
    """
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    if not path.exists() or path.stat().st_size < max_bytes:
        return None
    # 轮转前校验，避免固化已损坏历史 — 在排他锁内 verify，消除预检与归档间 TOCTOU
    tmp = Ledger(path)
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
        except Exception as exc:
            # 中文：独占锁失败必须 fail-closed，不可无锁裸奔（TOCTOU/分叉）
            try:
                _locked_h.close()
            except Exception:
                pass
            _locked_h = None
            raise LedgerCorruptionError(ChainBreak(0, None, "lock_failed", f"rotate lock_exclusive failed: {exc}")) from exc
        try:
            # 中文：verify 收拢进排他锁后、rename 前，消除空临界区 TOCTOU。
            # Windows msvcrt 是强制锁：锁区内另开句柄读写会被系统拒绝，
            # 故复用已加锁句柄读（同 append 路径），不用 tmp.verify() 另开句柄。
            _locked_h.seek(0)
            _rot_raw = _locked_h.read()
            try:
                _rot_text = _rot_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LedgerCorruptionError(ChainBreak(0, None, "malformed_json", f"decode_error: {exc}")) from exc
            _rot_entries: list[dict[str, Any]] = []
            for _line in _rot_text.splitlines():
                _s = _line.strip()
                if not _s:
                    continue
                try:
                    _rot_entries.append(json.loads(_s))
                except json.JSONDecodeError:
                    _rot_entries.append({"_raw": _s})
            _rot_ok, _rot_brk = tmp._verify_entries(_rot_entries, allow_legacy=allow_legacy)
            if not _rot_ok:
                for _idx, _e in enumerate(_rot_entries):
                    if "_raw" in _e:
                        raise LedgerCorruptionError(ChainBreak(_idx, None, "malformed_json", str(_e.get("_raw"))))
                raise LedgerCorruptionError(
                    ChainBreak(
                        _rot_brk.index if _rot_brk else 0,
                        _rot_brk.seq if _rot_brk else None,
                        _rot_brk.reason if _rot_brk else "prev_hash_mismatch",
                        _rot_brk.detail if _rot_brk else "ledger corrupted, cannot rotate",
                    )
                )
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
                # T2-2: 全程持排他锁直至 rename 完成（POSIX 原子 rename），消除先解锁后 rename 的并发 append 丢失窗口。
                # Windows 上 rename 需无打开句柄：改为 dup 句柄后关闭原句柄前保持锁语义——
                # POSIX 直接锁内 rename；Windows 先刷盘、解锁、关闭再 rename（强制锁下 rename 需关闭）。
                if os.name == "nt":
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
                else:
                    # POSIX: 锁内原子 rename，并发 append 被排他锁挡在临界区外，不丢失
                    try:
                        os.rename(path, archive)
                    except OSError as e:
                        _rename_err = e
            finally:
                if _locked:
                    try:
                        _unlock(_locked_h)  # type: ignore[arg-type]
                    except Exception:
                        pass
        except LedgerCorruptionError:
            raise
        except Exception as _e:
            raise LedgerCorruptionError(ChainBreak(0, None, "lock_failed", f"rotate rename/archive failed: {_e}")) from _e
    except LedgerCorruptionError:
        raise
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
    """原始行读取（共享锁防 TOCTOU/半写）：返回逐行 dict，坏行标记为 {"_raw": ...}。

    与 Ledger._read_all 同语义的模块级入口，供 verify_chain 等审计路径复用；
    调用方以 _verify_entries 判定 chain break（缺链字段经 _CHAIN_FIELDS 校验）。
    """
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


def verify_chain(path: Path, *, allow_legacy: bool = False) -> ChainVerificationResult:
    """校验单文件链的完整性（seq 连续与 hash 链）— 加共享锁防 TOCTOU。

    锁获取失败必须 LOUD（抛错），绝不静默回退到无锁读（否则重引入 TOCTOU/半写竞态）。
    中文：T2-2 默认拒绝旧式 hash；历史区间须显式 allow_legacy=True 豁免并记 warning。
    """
    ledger = Ledger(path)
    # 读经共享锁保护的 _read_raw_records，失败 LOUD（锁/IO 错误直接抛，不无锁重读）
    entries = _read_raw_records(path)
    for _ln, e in enumerate(entries):
        if "_raw" in e:
            return ChainVerificationResult(ok=False, record_count=_ln, first_break=ChainBreak(_ln, None, "malformed_json", str(e.get("_raw"))))
        # 中文：缺链字段经 _CHAIN_FIELDS 校验（与 _verify_entries 的 missing_chain_fields 一致）
        if not _CHAIN_FIELDS.issubset(e.keys()):
            missing = sorted(_CHAIN_FIELDS - set(e.keys()))
            return ChainVerificationResult(ok=False, record_count=_ln, first_break=ChainBreak(_ln, e.get("seq"), "missing_chain_fields", f"missing {missing}"))
    # T2-2: 透传 allow_legacy；兼容 monkeypatch 旧签名（无 allow_legacy 形参）/Ledger 替身（无 _verify_entries，回退其 verify()，异常 LOUD 透出）
    _ve = getattr(ledger, "_verify_entries", None)
    if _ve is None:
        # 替身 Ledger（如 FakeLedger）无 _verify_entries：回退其 verify() 语义，异常 LOUD 透出
        _vv = getattr(ledger, "verify", None)
        if _vv is None:
            raise TypeError("ledger double lacks _verify_entries/verify")
        try:
            _ok2 = _vv(allow_legacy=allow_legacy)
        except TypeError:
            _ok2 = _vv()
        if _ok2:
            return ChainVerificationResult(ok=True, record_count=len(entries), first_break=None)
        return ChainVerificationResult(
            ok=False, record_count=0, first_break=ChainBreak(0, None, "record_hash_mismatch", f"ledger verify failed for {path}")
        )
    try:
        ok, brk = _ve(entries, allow_legacy=allow_legacy)
    except TypeError:
        ok, brk = _ve(entries)
    return ChainVerificationResult(ok=ok, record_count=len(entries) if ok else (brk.index if brk else 0), first_break=brk)


def verify_chain_with_archives(path: Path, *, allow_legacy: bool = False) -> ChainVerificationResult:
    """校验包含归档分段的完整历史，拼接 archive_segments + 当前文件后统一 verify。

    中文：T2-2 默认拒绝旧式 hash；历史区间须显式 allow_legacy=True 豁免并记 warning。
    """
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
            # 中文：跨归档全局下标（len(records)+段内偏移），不用段内偏移（错位）
            for _off, line in enumerate(txt.splitlines()):
                if "\x00" in line:
                    _g = len(records) + _off
                    return ChainVerificationResult(ok=False, record_count=_g, first_break=ChainBreak(_g, None, "malformed_json", line))
            # 去除 NUL 后继续（但已在上面返回）
            txt = txt.replace("\x00", "")
        for line in txt.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                records.append(json.loads(s))
            except json.JSONDecodeError:
                # 中文：全局位置索引（enumerate），不用 list.index（重复记录错位）
                _ln = len(records)
                return ChainVerificationResult(ok=False, record_count=_ln, first_break=ChainBreak(_ln, None, "malformed_json", s))
    if not records:
        return ChainVerificationResult(ok=True, record_count=0, first_break=None)
    # reuse Ledger._verify_entries logic on concatenated records
    # T2-2: 透传 allow_legacy；仅 allow_legacy 旧签名 TypeError 做回退；替身 verify 异常 LOUD 透出。
    # 中文：verify 自身抛错（锁/IO/篡改 LOUD）必须包成 LedgerCorruptionError 透出，不得让 RuntimeError 直透
    # （b2b_06 契约同步：调用方只捕获 LedgerCorruptionError）。
    tmp = Ledger(path)
    _tve = getattr(tmp, "_verify_entries", None)
    if _tve is None:
        _tvv = getattr(tmp, "verify", None)
        if _tvv is None:
            raise TypeError("ledger double lacks _verify_entries/verify")
        try:
            try:
                _tok = _tvv(allow_legacy=allow_legacy)
            except TypeError as _tte:
                if "allow_legacy" in str(_tte):
                    _tok = _tvv()
                else:
                    raise
        except LedgerCorruptionError:
            raise
        except Exception as _ve:
            raise LedgerCorruptionError(
                ChainBreak(0, None, "record_hash_mismatch", f"ledger verify raised: {_ve}")
            ) from _ve
        if _tok:
            return ChainVerificationResult(ok=True, record_count=len(records), first_break=None)
        return ChainVerificationResult(
            ok=False, record_count=0, first_break=ChainBreak(0, None, "record_hash_mismatch", f"ledger verify failed for {path}")
        )
    try:
        try:
            ok, brk = _tve(records, allow_legacy=allow_legacy)
        except TypeError:
            ok, brk = _tve(records)
    except LedgerCorruptionError:
        raise
    except Exception as _ve2:
        raise LedgerCorruptionError(
            ChainBreak(0, None, "record_hash_mismatch", f"ledger verify raised: {_ve2}")
        ) from _ve2
    return ChainVerificationResult(ok=ok, record_count=len(records), first_break=brk)


def verify_export(export: Mapping[str, Any] | str | Path, *, allow_legacy: bool = False) -> ChainVerificationResult:
    """校验导出包：先比对 export_hash，再按租户链逐条重算 record_hash。

    中文：默认仅接受新式全字段 hash（tenant/price 防篡改）；历史区间须显式 allow_legacy=True 豁免并记 warning。
    """
    if isinstance(export, Path):
        data: Mapping[str, Any] = json.loads(export.read_text(encoding="utf-8"))
    elif isinstance(export, str):
        data = json.loads(export)
    else:
        data = export
    records = list(data.get("records", []))
    # 中文：全局位置索引（enumerate），不用 list.index（重复记录错位）
    pos_by_id = {id(r): i for i, r in enumerate(records)}
    envelope = {"format": data.get("format", EXPORT_FORMAT), "source_path": data.get("source_path", ""), "records": records}
    expected = f"sha256:{_sha256_hex(_canonical_json(envelope))}"
    if expected != data.get("export_hash"):
        return ChainVerificationResult(ok=False, record_count=0, first_break=ChainBreak(index=-1, seq=None, reason="export_hash_mismatch", detail=f"expected {expected!r} found {data.get('export_hash')!r}"))
    # check tenant chains reuse same logic as Ledger.verify on list
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if "_raw" in r:
            return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=pos_by_id.get(id(r), 0), seq=None, reason="malformed_json", detail=str(r.get("_raw"))))
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
            # T2-2: 严格版本匹配，默认拒绝旧式 hash（防 tenant/price 篡改后按旧式重算过 verify）
            tenant_v = entry.get("tenant", "default")
            price_v = entry.get("price")
            ok_h, _used_leg = _check_hash(entry.get("record_hash"), eff, prev, record, tenant_v, price_v, allow_legacy=allow_legacy)
            stored = entry.get("record_hash")
            if not ok_h:
                # try legacy genesis alternative if first entry
                if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                    alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                    # T2-2: alt-genesis 同样严格版本匹配
                    if _check_hash_alt_genesis(stored, eff, alt_prev, record, tenant_v, price_v, allow_legacy=allow_legacy):
                        prev = entry.get("record_hash")
                        continue
                new_hex = _tenant_payload_hash(eff, prev, record, tenant=tenant_v, price=price_v)
                return ChainVerificationResult(ok=False, record_count=len(records), first_break=ChainBreak(index=idx-1, seq=eff, reason="record_hash_mismatch", detail=f"stored {stored!r} recomputed {new_hex!r}"))
            prev = entry.get("record_hash")
    return ChainVerificationResult(ok=True, record_count=len(records), first_break=None)


def _tenant_tail_snapshot(entries: list[dict[str, Any]]) -> dict[str, list]:
    """PR2-E: 按文件顺序统计每租户 [count, tail_hash]，作为增量校验的可信锚点。"""
    tenants: dict[str, list] = {}
    for e in entries:
        if "_raw" in e:
            continue
        t = e.get("tenant", "default")
        slot = tenants.get(t)
        if slot is None:
            tenants[t] = [1, e.get("record_hash", "")]
        else:
            slot[0] += 1
            slot[1] = e.get("record_hash", "")
    return tenants


def _tail_self_check(entry: dict[str, Any]) -> bool:
    """PR2-E: O(1) 尾记录自复核 —— 用其自身 prev_hash 重算 record_hash。

    检出同长度篡改尾部 payload + 伪造 mtime/size 回缓存值的场景（此时四元组命中，必须看记录内容本身）。
    """
    if "_raw" in entry:
        return False
    ts = entry.get("tenant_seq")
    if ts is None:
        return False  # 无法定位，回落全扫
    t = entry.get("tenant", "default")
    prev = entry.get("prev_hash", "")
    record = entry.get("record")
    if record is None:
        return False
    # T2-2: 尾自检同样严格版本匹配（默认拒绝 legacy）
    ok_h, _ = _check_hash(entry.get("record_hash"), ts, prev, record, t, entry.get("price"), allow_legacy=False)
    if ok_h:
        return True
    if _is_genesis(prev):
        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
        if _check_hash_alt_genesis(entry.get("record_hash"), ts, alt_prev, record, t, entry.get("price"), allow_legacy=False):
            return True
    return False


def _verify_suffix_incremental(
    suffix: list[dict[str, Any]],
    *,
    start_seq: int,
    tenant_state: dict[str, list],
) -> tuple[bool, ChainBreak | None, dict[str, list]]:
    """PR2-E: 仅校验新增段 O(k)，前缀以后缀起点锚定已校验的 tenant_state（{tenant: [count, tail]}）。

    调用方须保证 suffix 非空、逐条含 tenant_seq 且无 _raw；返回 (ok, break, new_state)。
    """
    state = {t: [c, h] for t, (c, h) in tenant_state.items()}
    for off, entry in enumerate(suffix):
        gidx = start_seq - 1 + off  # 0-based 全局下标
        exp_seq = start_seq + off
        if entry.get("seq") != exp_seq:
            return False, ChainBreak(gidx, entry.get("seq"), "seq_gap", f"expected seq={exp_seq} found {entry.get('seq')!r}"), state
        t = entry.get("tenant", "default")
        slot = state.get(t)
        if slot is None:
            exp_ts, prev = 1, GENESIS_PREV_HASH
        else:
            exp_ts, prev = slot[0] + 1, slot[1]
        ts = entry.get("tenant_seq")
        if ts != exp_ts:
            return False, ChainBreak(gidx, ts, "seq_gap", f"tenant {t} expected tenant_seq={exp_ts} got {ts!r}"), state
        ph = entry.get("prev_hash")
        first_of_tenant = exp_ts == 1
        if ph != prev and not (first_of_tenant and _is_genesis(ph) and _is_genesis(prev)):
            return False, ChainBreak(gidx, ts, "prev_hash_mismatch", f"expected {prev!r} got {ph!r}"), state
        record = entry.get("record")
        if record is None:
            return False, ChainBreak(gidx, ts, "missing_chain_fields", "missing record"), state
        # T2-2: 后缀增量同样严格版本匹配（默认拒绝 legacy）
        ok_h, _ = _check_hash(entry.get("record_hash"), ts, prev, record, t, entry.get("price"), allow_legacy=False)
        stored = entry.get("record_hash")
        if not ok_h:
            if first_of_tenant and _is_genesis(prev) and _is_genesis(ph):
                alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                if _check_hash_alt_genesis(stored, ts, alt_prev, record, t, entry.get("price"), allow_legacy=False):
                    state[t] = [exp_ts, entry.get("record_hash")]
                    continue
            new_hex = _tenant_payload_hash(ts, prev, record, tenant=t, price=entry.get("price"))
            return False, ChainBreak(gidx, ts, "record_hash_mismatch", f"stored {stored!r} recomputed {new_hex!r}"), state
        state[t] = [exp_ts, entry.get("record_hash")]
    return True, None, state


class Ledger:
    """JSONL hash 链账本：追加写、按租户隔离、可全链校验。

    不变量：全局 seq 单调递增且连续；每租户 tenant_seq 连续、prev_hash 指向前一条 record_hash；任意 record 被篡改则 verify() 失败；0600 权限与 fsync 保证落盘后可恢复。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # T2-2: 原子安全创建 O_CREAT|O_EXCL 0o600，消除创建后 chmod 的 0644 窗口；
        # 已存在文件 best-effort 收紧权限（Windows 下 chmod 无 ACL 意义，仅尽力）。
        if not self.path.exists():
            try:
                _create_secure(self.path)
            except Exception as _exc:
                logger.warning("silent handled: governance: ledger secure-create best-effort", exc_info=_exc)  # intentional: governance: ledger secure-create best-effort
                pass  # intentional governance: ledger secure-create best-effort
        else:
            try:
                os.chmod(self.path, 0o600)
            except Exception as _exc:
                logger.warning("silent handled: governance: ledger fsync/lock best-effort, durability degraded but not silent", exc_info=_exc)  # intentional: governance: ledger fsync/lock best-effort, durability degraded but not silent
                pass  # intentional governance: ledger fsync/lock best-effort, durability degraded but not silent

    def _read_all(self, *, lock: bool = True):
        """逐行读取 JSONL，errors='strict' 且 NUL 视为 corruption — 加共享锁防 TOCTOU。

        lock=False 跳过加锁，仅供外层已持排他锁的调用方（如 rotate 内 verify）使用，
        避免 Windows msvcrt 同进程嵌套加锁自死锁。
        锁获取失败必须 LOUD（抛错），绝不静默回退到无锁读（TOCTOU/半写）。
        """
        if not self.path.exists():
            return []
        entries = []
        # 共享锁读；锁失败 LOUD（抛错），绝不回退无锁读（TOCTOU/半写）
        raw: bytes | None = None
        try:
            with open(self.path, "rb") as h:
                if lock:
                    try:
                        _lock_shared(h)
                        h.seek(0)
                        raw = h.read()
                    finally:
                        try:
                            _unlock(h)
                        except Exception:
                            pass
                else:
                    raw = h.read()
            text = raw.decode("utf-8")  # strict  # type: ignore[union-attr]
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

    def _verify_entries(
        self, entries: list[dict[str, Any]], *, allow_legacy: bool = False
    ) -> tuple[bool, ChainBreak | None]:
        """O(n) 全链校验：全局 seq 连续 + 每租户 prev_hash/record_hash 链。增量优化：按租户分组后顺序校验，尾部缓存（_tail_verify_cache）可用于下次增量校验。

        中文：T2-2 默认拒绝旧式 hash；历史区间须显式 allow_legacy=True 豁免并记 warning 告警。
        """
        for e in entries:
            if "_raw" in e:
                # 中文：用全局下标（enumerate），不用 list.index（O(n²)+重复行错位）
                idx = next(i for i, x in enumerate(entries) if x is e)
                return False, ChainBreak(idx, None, "malformed_json", str(e.get("_raw")))
        # 全局 seq 单调连续校验
        for idx, entry in enumerate(entries, start=1):
            if entry.get("seq") != idx:
                return False, ChainBreak(idx-1, entry.get("seq"), "seq_gap", f"expected seq={idx} found {entry.get('seq')!r}")
        # 中文：全局下标索引，供租户分组校验映射回全局位置
        pos_by_id = {id(e): i for i, e in enumerate(entries)}
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
                # 中文：断裂上报全局下标（pos_by_id），不用租户内序号
                gidx = pos_by_id.get(id(entry), idx - 1)
                ts = entry.get("tenant_seq")
                eff = ts if ts is not None else idx
                if ts is not None and ts != idx:
                    return False, ChainBreak(gidx, ts, "seq_gap", f"tenant {t} expected tenant_seq={idx} got {ts!r}")
                ph = entry.get("prev_hash")
                # 首条允许 GENESIS 与 legacy 0*64 等价
                if ph != prev and not (_is_genesis(ph) and _is_genesis(prev) and idx == 1):
                    return False, ChainBreak(gidx, eff, "prev_hash_mismatch", f"expected {prev!r} got {ph!r}")
                record = entry.get("record")
                if record is None:
                    return False, ChainBreak(gidx, eff, "missing_chain_fields", "missing record")
                # T2-2：严格版本匹配，默认拒绝旧式 hash（防 tenant/price 篡改后按旧式重算过 verify）
                tenant_v = entry.get("tenant", "default")
                price_v = entry.get("price")
                ok_h, _used_leg = _check_hash(entry.get("record_hash"), eff, prev, record, tenant_v, price_v, allow_legacy=allow_legacy)
                stored = entry.get("record_hash")
                if not ok_h:
                    # 首条兼容 legacy GENESIS 形态 — alt_prev 同样严格版本匹配
                    if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                        if _check_hash_alt_genesis(stored, eff, alt_prev, record, tenant_v, price_v, allow_legacy=allow_legacy):
                            prev = entry.get("record_hash")
                            continue
                    new_hex = _tenant_payload_hash(eff, prev, record, tenant=tenant_v, price=price_v)
                    return False, ChainBreak(gidx, eff, "record_hash_mismatch", f"stored {stored!r} recomputed {new_hex!r}")
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
        # T2-2: 新文件原子 O_CREAT|O_EXCL 0o600 创建（无 0644 窗口）；open 失败回退 secure-create 后重试
        # 记录是否新建文件，用于目录 fsync
        created = not self.path.exists()
        if created:
            try:
                _create_secure(self.path)
                created = True
            except Exception:
                pass
        # 以 a+b 打开以便加锁后回读历史；发生异常时确保解锁
        try:
            handle = open(self.path, "a+b")
        except FileNotFoundError:
            # 并发 rotate 刚搬走文件：安全重建后重试一次
            _create_secure(self.path)
            handle = open(self.path, "a+b")
            created = True
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
                # 追加前校验，断链则拒绝写入 —— PR2-E 批量增量校验（A2b 加固版）：
                # 1) 缓存命中（count/tail/内容hash一致）+ O(1) 尾自检通过 → 跳过全扫；
                #    短路条件用 sha256(raw_bytes) 内容哈希，不用 mtime/size（外部可伪造）；
                # 2) 未命中但 count 未回退，且缓存锚点 tail 与新增段起点 prev 连续 → 仅校验新增段 O(k)；
                # 3) 否则（无缓存/收缩/锚点断裂/内容hash不符）→ 回落 O(n) 全扫。
                _cache_key = str(self.path)
                _cached = _tail_verify_cache.get(_cache_key)
                _new_tenants: dict[str, list] | None = None
                if _cached is not None and len(_cached) == 4:
                    # 兼容旧版 4 元缓存：无内容哈希，不可信 → 视为未命中（全扫重建）
                    _cached = None
                if _cached is not None and len(_cached) == 5 and not isinstance(_cached[0], str):
                    # 兼容 5 元旧缓存（mtime,size,count,tail,tenants）：无内容哈希 → 未命中
                    _cached = None
                if _cached is not None:
                    try:
                        import hashlib as _hl

                        _cur_content = _hl.sha256(raw_bytes).hexdigest()
                        _ch, _cs, _cc, _ct, _ctenants = _cached[0], _cached[1], _cached[2], _cached[3], _cached[4]
                        _cur_tail = entries[-1].get("record_hash", "") if entries else GENESIS_PREV_HASH
                        if _cc == len(entries) and _ct == _cur_tail and _ch == _cur_content:
                            # 中文：内容哈希一致证明文件逐字节未变，短路可信；
                            # 仍做 O(1) 尾自检防缓存本身被污染
                            if not entries or _tail_self_check(entries[-1]):
                                ok, brk = True, None
                                _new_tenants = {t: [c, h] for t, (c, h) in _ctenants.items()}
                            else:
                                ok, brk = self._verify_entries(entries)
                        elif _cc <= len(entries) and not any("_raw" in e for e in entries):
                            # 增量分支前必须先证明前缀字节未变：账本是 append-only，
                            # 合法增长意味着旧文件内容是当前文件的字节前缀。
                            # 仅锚点连续+边界自检不足——攻击者可篡改前缀中间记录后
                            # 再追加一条锚定正确的后缀，使后缀校验通过而前缀篡改漏检。
                            # 故先验旧长度前缀的 sha256 是否等于缓存内容哈希，否则回落全扫。
                            _prefix_ok = False
                            try:
                                if isinstance(_cs, int) and len(raw_bytes) >= _cs:
                                    _prefix_ok = _hl.sha256(raw_bytes[:_cs]).hexdigest() == _ch
                            except Exception:
                                _prefix_ok = False
                            if not _prefix_ok:
                                ok, brk = self._verify_entries(entries)
                            else:
                                # 前缀字节已证未变：从缓存 count 处切分新增段
                                _suffix = entries[_cc:]
                                _anchor_ok = True
                                if _cc == 0:
                                    _exp_prev_map: dict[str, str] = {}
                                else:
                                    if len(_suffix) == 0:
                                        _anchor_ok = False
                                    else:
                                        _exp_prev_map = {}
                                        for _e in _suffix:
                                            _t = _e.get("tenant", "default")
                                            if _t not in _exp_prev_map:
                                                _slot = _ctenants.get(_t)
                                                _exp_prev_map[_t] = _slot[1] if _slot is not None else GENESIS_PREV_HASH
                                        _first = _suffix[0]
                                        _ft = _first.get("tenant", "default")
                                        _fph = _first.get("prev_hash")
                                        _fprev = _exp_prev_map[_ft]
                                        _anchor_ok = _fph == _fprev or (
                                            _ctenants.get(_ft) is None and _is_genesis(_fph) and _is_genesis(_fprev)
                                        )
                                if _anchor_ok and _suffix and all(e.get("tenant_seq") is not None and "_raw" not in e for e in _suffix):
                                    # 锚点记录本身 O(1) 自检：确认缓存边界条目未被替换（深层前缀以前次全量/增量校验结论为信任基础；
                                    # 带外篡改的最终兜底仍是 verify()/verify_chain 全扫审计路径）
                                    if _cc > 0 and not _tail_self_check(entries[_cc - 1]):
                                        ok, brk = self._verify_entries(entries)
                                    else:
                                        ok, brk, _new_tenants = _verify_suffix_incremental(
                                            _suffix, start_seq=_cc + 1, tenant_state=_ctenants
                                        )
                                        if not ok:
                                            _new_tenants = None
                                else:
                                    ok, brk = self._verify_entries(entries)
                        else:
                            ok, brk = self._verify_entries(entries)
                    except LedgerCorruptionError:
                        raise
                    except Exception:
                        ok, brk = self._verify_entries(entries)
                else:
                    ok, brk = self._verify_entries(entries)
                # 校验通过后更新缓存（内容哈希, size, count, tail, tenants 快照；
                # 增量分支复用后缀校验结果，全扫分支重算快照）
                if ok:
                    try:
                        import hashlib as _hl2

                        _n_content = _hl2.sha256(raw_bytes).hexdigest()
                        _n_size = len(raw_bytes)
                        _n_tail = entries[-1].get("record_hash", "") if entries else GENESIS_PREV_HASH
                        if _new_tenants is None:
                            _new_tenants = _tenant_tail_snapshot(entries)
                        _tail_verify_cache[_cache_key] = (_n_content, _n_size, len(entries), _n_tail, _new_tenants)
                    except Exception:
                        pass
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
                # 追加成功后刷新 tail 缓存，供下次增量短路（记录内容哈希、新计数值、尾 hash 与 tenants 快照）
                try:
                    import hashlib as _hl3

                    # handle 已写入新行，entries 长度为旧长度，追加后 count+1，tail 为新 record_hash
                    _post_tenants = {t: [c, h] for t, (c, h) in (_new_tenants or {}).items()}
                    _slot = _post_tenants.get(tenant)
                    if _slot is None:
                        _post_tenants[tenant] = [1, record_hash]
                    else:
                        _slot[0] += 1
                        _slot[1] = record_hash
                    handle.seek(0)
                    _post_content = _hl3.sha256(handle.read()).hexdigest()
                    _tail_verify_cache[str(self.path)] = (_post_content, len(raw_bytes) + len(line), len(entries) + 1, record_hash, _post_tenants)
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

    def verify(self, tenant: str | None = None, *, lock: bool = True, allow_legacy: bool = False) -> bool:
        """校验链完整性；指定 tenant 时仅校验该租户子链。共享锁读防 TOCTOU。

        lock=False 跳过读锁，仅供外层已持排他锁时使用（rotate 内 verify）。
        中文：T2-2 默认拒绝旧式 hash；历史区间须显式 allow_legacy=True 豁免并记 warning。
        """
        entries = self._read_all(lock=lock)
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
                # T2-2: 租户子链同样严格版本匹配（默认拒绝 legacy）
                ok_h, _ = _check_hash(entry.get("record_hash"), eff, prev, record, tenant_v, price_v, allow_legacy=allow_legacy)
                stored = entry.get("record_hash")
                if not ok_h:
                    if idx == 1 and _is_genesis(prev) and _is_genesis(ph):
                        alt_prev = _LEGACY_GENESIS if prev == GENESIS_PREV_HASH else GENESIS_PREV_HASH
                        if _check_hash_alt_genesis(stored, eff, alt_prev, record, tenant_v, price_v, allow_legacy=allow_legacy):
                            prev = entry.get("record_hash")
                            continue
                    return False
                prev = entry.get("record_hash")
            return True
        else:
            # T2-2: 透传 allow_legacy；兼容 monkeypatch 旧签名 counting(self, entries)（无 allow_legacy 形参）
            try:
                ok, _ = self._verify_entries(entries, allow_legacy=allow_legacy)
            except TypeError:
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
        # 中文：_raw 腐蚀标记无 tenant 键，不可默认归入 default 租户（隔离击穿）
        filtered = [e for e in entries if "_raw" not in e and e.get("tenant", "default") == tenant]
        # deep copy to prevent caller mutation leaking state
        return copy.deepcopy(filtered)

    def query_by_tenant(self, tenant: str):
        """query 的别名，保持对旧调用的兼容。"""
        return self.query(tenant)

    def list_records(self, tenant: str):
        """列出指定租户的记录（query 的语义化别名）。"""
        return self.query(tenant)

    def list_tenants(self):
        """列出账本中出现过的所有 tenant（_raw 腐蚀标记不计入）。"""
        entries = self._read_all()
        return sorted({e.get("tenant", "default") for e in entries if "_raw" not in e})
