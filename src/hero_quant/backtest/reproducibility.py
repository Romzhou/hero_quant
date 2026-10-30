"""回测可复现指纹：config_hash / data_fingerprint / result_hash / code_commit。

职责：给「同数据 + 同代码 + 同配置」的重跑提供可对齐的指纹三元组——配置指纹（跑了什么）、
输入指纹（喂进去的价格序列）、产物指纹（算出来的指标/持仓/tearsheet），任一环变化都会让对应
hash 变；三者一致即「本次结果可被同一份输入重放出来」。

架构位置：`backtest` 包的纯函数层，**只依赖 stdlib**（不 import pandas/numpy/redis），
被 `api/server.py` 的回测产物段调用；传进来的 DataFrame 只经鸭子类型读 index/close 列。

关键设计：
- 归一化：所有浮点先 `round(x, 12)`（1e-12 归一）再序列化，避免末位抖动（不同平台/不同
  BLAS 的 ulp 差）把「同一个结果」判成不同 hash；验收比对口径用 1e-9 容差（见下）。
- 不带时间戳/租户/耗时/缓存标记：这些进 hash 会让同一份结果每次重跑都变，等于把可复现性
  证明成废话；`config_hash` 对易变键显式 raise，而不是静默塞进 hash。
- `code_commit()` 拿不到就返回 None，**绝不编一个假 sha**；调用方（server）负责把它翻成
  `code_commit_unknown` warning 让下游知道「代码版本未知」而非「代码版本是 X」。

**D1 决策（PR-124，重要边界）**：PR-122 的「全量 PIT 快照」仍被依赖审批（duckdb/pyarrow）
阻塞，所以 `data_fingerprint` **不是 PIT 快照指纹**：它绑定的是「本次实际喂给引擎的价格序列」
（index 日期 + close 逐行），只能证明「这两次跑吃的是同一份输入」，**不能**证明该序列与历史
某时点的 PIT 快照等价（幸存者偏差/复权/停牌口径均未校验）。PIT 快照绑定仍缺，等审批通过后
由 `data_fingerprint` 换成 / 追加 `snapshot_id` 才算闭环。

验收口径：
- config_hash / data_fingerprint / result_hash 都是 `sha256(...)[:32]`（32 hex 字符）；
- 浮点差异 <= 1e-12 视为同值（归一后同 hash），差异 > 1e-12 必然不同 hash；
- 指标数值比对用 1e-9 相对/绝对容差（round 12 的余量），**不要**用 == 比 float。
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
import subprocess
import threading
from collections.abc import Mapping

# 指纹语义版本：归一化规则/字段集合变化必须 +1（响应里透出，供消费方判定可比性）。
HASH_VERSION = 1

# 浮点归一的小数位：1e-12 位以下视为噪声（12 位十进制 ≈ 双精度相对精度 2.2e-16 的 4 个量级余量，
# 足够吃掉 cumsum/年化带来的 ulp 抖动，又不会把真实的第 13 位差异抹平）。
_ROUND_DIGITS = 12

# 必须出现在 config 里的「影响结果」的键：缺一个就说明调用方少传了决定结果的配置，
# 那种 config_hash 只是看起来像指纹（同一份结果配不同 hash，或反过来），必须显式报错。
CONFIG_KEYS: tuple[str, ...] = (
    "symbol",
    "interval",
    "bars",
    "adjust",
    "initial_capital",
    "weights",
    "engine",
    "engine_version",
)

# 易变键：时间戳/租户/请求 ID/耗时/缓存标记。它们进 hash 会让「同结果」每次算出不同指纹。
VOLATILE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "tenant",
        "tenant_id",
        "timestamp",
        "ts",
        "now",
        "created_at",
        "generated_at",
        "trace_id",
        "request_id",
        "elapsed_ms",
        "duration_ms",
        "cache_hit",
    }
)

# result_hash 里同样要剔除的易变指标键（耗时/缓存命中/生成时间），它们不描述结果本身。
VOLATILE_RESULT_KEYS: frozenset[str] = VOLATILE_CONFIG_KEYS

# 仓库根（src/hero_quant/backtest/reproducibility.py → parents[3]）：git 只能在这里跑。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# short sha 合法形态：7~40 位小写 hex。git 输出任何别的形状都视为「拿不到」，不解析成假 sha。
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")

_GIT_TIMEOUT_SECONDS = 2

# sentinel：区分「还没查过」（_UNSET）与「查过但拿不到」（None）——后者要缓存，避免每次请求都
# 起一个 subprocess；前者必须真的去查一次。
_UNSET = object()
_code_commit_cache: object = _UNSET
_code_commit_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 归一化 + canonical JSON
# ---------------------------------------------------------------------------
def _round_float(value: float) -> float | None:
    """浮点归一：非有限值（NaN/±inf）→ None，其余 round 到 12 位且把 -0.0 收敛成 0.0。

    中文：NaN 不是合法 JSON（`json.dumps` 默认会吐 `NaN` 这种非标准字面量），而 -0.0 与 0.0
    在 repr 上不同会让「数值相等」的两个结果 hash 不同 —— 两者都在这里抹平。
    """
    if not math.isfinite(value):
        return None
    # -0.0 与 0.0 的位模式不同、json 文本不同（"-0.0"/"0.0"），但数值相同：统一收敛到 0.0。
    if value == 0.0:
        return 0.0
    return round(value, _ROUND_DIGITS)


def _normalize(value):
    """递归归一化成「可确定性序列化」的纯 Python 结构（不隐式 str()，避免假归一）。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):  # bool 已在上面的分支截走
        return value
    if isinstance(value, float):
        return _round_float(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    # numpy/pandas 标量（np.float64 之外的 np.float32 / np.int64 / Timedelta 等）：鸭子类型 .item()
    # 还原成 Python 标量再归一；不做 float() 强转，避免把「不支持的类型」静默变成一个数。
    item_fn = getattr(value, "item", None)
    if callable(item_fn) and not isinstance(value, (bytes, bytearray)):
        try:
            raw = item_fn()
        except (TypeError, ValueError) as exc:
            raise TypeError(f"canonical_json 无法归一化 {type(value).__name__}: {exc}") from exc
        if raw is not value:
            return _normalize(raw)
    raise TypeError(
        f"canonical_json 不支持的类型 {type(value).__name__}："
        "请先显式转成 JSON 原生类型（不做隐式 str()，否则 hash 会悄悄绑上 repr）"
    )


def canonical_json(obj) -> str:
    """确定性 JSON 文本：键排序 + 紧凑分隔符 + 浮点 12 位归一。

    中文：`sort_keys` 抹掉 dict 迭代序、`separators=(",", ":")` 抹掉空格差异、浮点归一吃
    末位抖动；三者合起来保证「同一个语义对象两次序列化，字节相同」。
    `allow_nan=False` 是兜底：万一有 NaN/inf 绕过 `_round_float`，这里硬失败而不是写出非法 JSON。
    """
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest32(text: str) -> str:
    """sha256 前 32 hex（128 bit：碰撞概率对「同输入判定」足够，且比 64 位短一半便于透出）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 三件套指纹
# ---------------------------------------------------------------------------
def config_hash(config: Mapping) -> str:
    """配置指纹：`sha256(canonical_json(config))[:32]`。

    中文：只收「影响结果」的配置。缺 `CONFIG_KEYS` 里任何一个 → `ValueError`（少传字段的
    hash 不可比）；出现 `VOLATILE_CONFIG_KEYS`（租户/时间戳/耗时/缓存标记）→ 同样 `ValueError`
    （这类键会让同一份结果每次重跑都换指纹，等于主动破坏可复现性）。
    """
    if not isinstance(config, Mapping):
        raise TypeError(f"config_hash 需要 Mapping，收到 {type(config).__name__}")
    missing = [key for key in CONFIG_KEYS if key not in config]
    if missing:
        raise ValueError(f"config_hash 缺少影响结果的配置键: {missing}")
    volatile = sorted(set(config) & VOLATILE_CONFIG_KEYS)
    if volatile:
        raise ValueError(f"config_hash 不接受易变键（租户/时间戳/耗时）: {volatile}")
    return _digest32(canonical_json(dict(config)))


def _date_str(value) -> str:
    """日期归一：`Timestamp/date/datetime` → ISO 日期串；字符串取前 10 位（与 sample.start 同口径）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:10]
    # pandas.Timestamp / datetime.datetime / datetime.date 都有 date()（date 自身也有）。
    date_fn = getattr(value, "date", None)
    if callable(date_fn):
        try:
            got = date_fn()
        except (TypeError, ValueError):
            got = None
        iso = getattr(got, "isoformat", None)
        if callable(iso):
            return str(iso())
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    return str(value)


def _close_value(value) -> float:
    """收盘价归一：非数值直接 `ValueError`（不猜、不填 0——填 0 会造出一个假的指纹）。"""
    if isinstance(value, bool):  # bool 是 int 子类，价格里出现 True/False 一定是上游错
        raise ValueError(f"收盘价非法（bool）: {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"收盘价非法（非数值）: {value!r}") from exc


def _price_rows(prices) -> list[list]:
    """把价格序列折成 `[[日期, 收盘], ...]`；**顺序即语义**（时间序列换序 = 不同输入）。"""
    if prices is None:
        return []
    # DataFrame 路径（鸭子类型：有 index + columns，避免 import pandas）：只认 close 列。
    columns = getattr(prices, "columns", None)
    index = getattr(prices, "index", None)
    if columns is not None and index is not None:
        close_name = None
        for name in columns:
            if str(name).strip().lower() == "close":
                close_name = name
                break
        if close_name is None:
            # 没有 close 列就无法按「日期 + close」定指纹：返回空让调用方标不可复现，
            # 而不是拿别的列凑一个看起来像指纹的值。
            return []
        try:
            closes = prices[close_name]
            return [[_date_str(idx), _close_value(val)] for idx, val in zip(index, closes)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"价格序列无法归一化: {exc}") from exc
    # list[dict] 路径：每根要有 close；date 缺就给空串（调用方自己决定要不要用）。
    if isinstance(prices, (list, tuple)):
        rows: list[list] = []
        for pos, item in enumerate(prices):
            if not isinstance(item, Mapping):
                raise ValueError(f"价格序列第 {pos} 项不是 Mapping: {type(item).__name__}")
            if "close" not in item:
                raise ValueError(f"价格序列第 {pos} 项缺少 close")
            rows.append([_date_str(item.get("date")), _close_value(item["close"])])
        return rows
    raise TypeError(f"data_fingerprint 不支持的价格序列类型: {type(prices).__name__}")


def data_fingerprint(prices) -> str | None:
    """输入指纹：对「本次实际喂给引擎的价格序列」（index 日期 + close）取 `sha256[:32]`。

    中文（D1 决策边界）：这**不是** PIT 快照指纹，它只绑定本次输入 —— 同一份数据重放出同一
    hash，但「这份数据是不是历史时点的 PIT 快照」不在证明范围内（PR-122 未合入）。
    空/None/无 close 列一律返回 None（不造假 hash：空输入算出来的 sha 会让空壳看起来可复现）。
    """
    rows = _price_rows(prices)
    if not rows:
        return None
    return _digest32(canonical_json({"v": HASH_VERSION, "kind": "prices", "rows": rows}))


def result_hash(*, metrics: Mapping, positions_csv: str | None, tearsheet: str = "") -> str:
    """产物指纹：metrics（数值 round 12）+ positions CSV 文本 + tearsheet → `sha256[:32]`。

    中文：只吃「产物本身」，不含耗时/缓存命中/生成时间等易变字段（`VOLATILE_RESULT_KEYS`
    在 metrics 顶层剔除）—— 否则同一次重跑会因为耗时不同而 hash 不同，指标就失去意义。
    """
    if not isinstance(metrics, Mapping):
        raise TypeError(f"result_hash 需要 metrics Mapping，收到 {type(metrics).__name__}")
    for name, value in (("positions_csv", positions_csv), ("tearsheet", tearsheet)):
        if value is not None and not isinstance(value, str):
            raise TypeError(f"result_hash 的 {name} 必须是 str 或 None，收到 {type(value).__name__}")
    _metrics = {key: item for key, item in metrics.items() if str(key) not in VOLATILE_RESULT_KEYS}
    payload = {
        "v": HASH_VERSION,
        "kind": "result",
        "metrics": _metrics,
        "positions_csv": positions_csv or "",
        "tearsheet": tearsheet or "",
    }
    return _digest32(canonical_json(payload))


# ---------------------------------------------------------------------------
# 代码版本（拿不到就是 None，绝不编假 sha）
# ---------------------------------------------------------------------------
def _read_git_commit() -> str | None:
    """`git rev-parse --short HEAD`（cwd=仓库根，2s 超时）；任何失败都返回 None。"""
    try:
        proc = subprocess.run(  # noqa: S603 - 固定参数列表，无 shell，无用户输入拼接
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT),
            timeout=_GIT_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        # 无 git 二进制 / 超时（TimeoutExpired 是 SubprocessError 子类）/ 无仓库读权限 /
        # cwd 不存在（装在 wheel 里时 parents 结构不同）—— 一律「未知」，不猜。
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not _COMMIT_RE.match(out):
        # git 有输出但不像 short sha（例如 fatal: 被写到 stdout 的极端情况）：仍然算未知。
        return None
    return out


def code_commit() -> str | None:
    """当前代码版本（short sha）；首次调用查一次并缓存，拿不到返回 None。

    中文：**绝不编一个假 sha**。返回 None 时调用方必须显式降级（server 追加
    `code_commit_unknown` warning），让「代码版本未知」在响应里可见，而不是看起来已验证。
    """
    global _code_commit_cache
    if _code_commit_cache is not _UNSET:
        return _code_commit_cache  # type: ignore[return-value]
    with _code_commit_lock:
        if _code_commit_cache is _UNSET:
            _code_commit_cache = _read_git_commit()
        return _code_commit_cache  # type: ignore[return-value]


def reset_code_commit_cache() -> None:
    """清掉 commit 缓存（测试用：验证「无 git → None」后必须能回到真实环境）。"""
    global _code_commit_cache
    with _code_commit_lock:
        _code_commit_cache = _UNSET


__all__ = [
    "HASH_VERSION",
    "CONFIG_KEYS",
    "VOLATILE_CONFIG_KEYS",
    "canonical_json",
    "config_hash",
    "data_fingerprint",
    "result_hash",
    "code_commit",
    "reset_code_commit_cache",
]
