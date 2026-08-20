import json
import hashlib
import os
from pathlib import Path


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

    def append(self, record: dict):
        entries = self._read_all()
        seq = len(entries) + 1
        prev_hash = entries[-1]["record_hash"] if entries else "0" * 64
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
        record_hash = hashlib.sha256(f"{seq}:{prev_hash}:{payload}".encode()).hexdigest()
        line = json.dumps(
            {"seq": seq, "prev_hash": prev_hash, "record_hash": record_hash, "record": record},
            ensure_ascii=False,
        )
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

    def verify(self) -> bool:
        entries = self._read_all()
        prev = "0" * 64
        for idx, entry in enumerate(entries, start=1):
            # basic corruption check
            if "_raw" in entry:
                return False
            if entry.get("seq") != idx:
                return False
            if entry.get("prev_hash") != prev:
                return False
            record = entry.get("record")
            if record is None:
                return False
            payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
            expected = hashlib.sha256(f"{idx}:{prev}:{payload}".encode()).hexdigest()
            if entry.get("record_hash") != expected:
                return False
            prev = entry.get("record_hash")
        return True
