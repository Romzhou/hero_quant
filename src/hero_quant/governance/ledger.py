import json
import hashlib
import os
from pathlib import Path
from collections import defaultdict


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ensure file exists
        if not self.path.exists():
            # create empty file
            self.path.touch(exist_ok=True)
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
            # dir fsync
            try:
                dir_fd = os.open(str(self.path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        else:
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass

    def _read_all(self):
        if not self.path.exists():
            return []
        entries = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # corrupted line -> return entries so verify will fail
                # keep raw to cause mismatch
                entries.append({"_raw": line})
        return entries

    def append(self, record: dict, tenant: str = "default", price: float | None = None):
        entries = self._read_all()
        seq = len(entries) + 1
        # per-tenant chain
        tenant_entries = [e for e in entries if e.get("tenant", "default") == tenant]
        tenant_seq = len(tenant_entries) + 1
        prev_hash = tenant_entries[-1]["record_hash"] if tenant_entries else "0" * 64
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
        record_hash = hashlib.sha256(f"{tenant_seq}:{prev_hash}:{payload}".encode()).hexdigest()
        obj = {
            "seq": seq,
            "tenant_seq": tenant_seq,
            "tenant": tenant,
            "prev_hash": prev_hash,
            "record_hash": record_hash,
            "record": record,
        }
        if price is not None:
            obj["price"] = price
        line = json.dumps(obj, ensure_ascii=False)
        # atomic append
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass
        # dir fsync
        try:
            dir_fd = os.open(str(self.path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
        return obj

    def verify(self, tenant: str | None = None) -> bool:
        entries = self._read_all()
        # quick corruption check
        for e in entries:
            if "_raw" in e:
                return False
        if tenant is not None:
            # filtered verification: only tenant's chain
            filtered = [e for e in entries if e.get("tenant", "default") == tenant]
            # sort by tenant_seq (fallback to order)
            filtered_sorted = sorted(filtered, key=lambda x: x.get("tenant_seq", 0) or 0)
            # if tenant_seq missing, reconstruct order by seq
            if any("tenant_seq" not in e for e in filtered_sorted):
                # fallback to seq order
                filtered_sorted = sorted(filtered, key=lambda x: x.get("seq", 0))
            prev = "0" * 64
            for idx, entry in enumerate(filtered_sorted, start=1):
                # tenant_seq check
                ts = entry.get("tenant_seq")
                if ts is not None and ts != idx:
                    # allow gap if legacy missing? enforce sequential
                    return False
                # for legacy entries without tenant_seq, idx is position
                effective_seq = ts if ts is not None else idx
                if entry.get("prev_hash") != prev:
                    return False
                record = entry.get("record")
                if record is None:
                    return False
                payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
                expected = hashlib.sha256(f"{effective_seq}:{prev}:{payload}".encode()).hexdigest()
                if entry.get("record_hash") != expected:
                    return False
                prev = entry.get("record_hash")
            return True
        else:
            # global verify: check all per-tenant chains individually + global seq
            # 1) global seq monotonic 1..N
            for idx, entry in enumerate(entries, start=1):
                if entry.get("seq") != idx:
                    return False
            # 2) group by tenant and verify each chain
            groups = defaultdict(list)
            for e in entries:
                t = e.get("tenant", "default")
                groups[t].append(e)
            for t, group in groups.items():
                # sort by tenant_seq or seq fallback
                if any("tenant_seq" not in e for e in group):
                    group_sorted = sorted(group, key=lambda x: x.get("seq", 0))
                else:
                    group_sorted = sorted(group, key=lambda x: x.get("tenant_seq", 0))
                prev = "0" * 64
                for idx, entry in enumerate(group_sorted, start=1):
                    ts = entry.get("tenant_seq")
                    effective_seq = ts if ts is not None else idx
                    if ts is not None and ts != idx:
                        return False
                    if entry.get("prev_hash") != prev:
                        return False
                    record = entry.get("record")
                    if record is None:
                        return False
                    payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
                    expected = hashlib.sha256(f"{effective_seq}:{prev}:{payload}".encode()).hexdigest()
                    if entry.get("record_hash") != expected:
                        return False
                    prev = entry.get("record_hash")
            return True

    # RLS isolation simple where tenant=...
    def query(self, tenant: str):
        """RLS isolation: return entries where tenant == ..."""
        entries = self._read_all()
        return [e for e in entries if e.get("tenant", "default") == tenant]

    # aliases for test flexibility
    def query_by_tenant(self, tenant: str):
        return self.query(tenant)

    def list_records(self, tenant: str):
        return self.query(tenant)

    def list_tenants(self):
        entries = self._read_all()
        return sorted({e.get("tenant", "default") for e in entries})
