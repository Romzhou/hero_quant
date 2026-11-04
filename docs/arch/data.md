# Data 模块深挖（双源行情 + 注册表 + provenance）

> 依据：`docs/recon.md` §1/§3（数据层）、`docs/issues.md` P3-3。行号为 2026-10-19 快照实测值。

## 1. 职责一句话

`src/hero_quant/data/` 提供行情源接入与双源 fallback，`src/hero_quant/data/registry.py` 是 `VALID_SOURCES` 白名单（16 个枚举）与 `provenance{source, unit, adjust}` 全链路透传的唯一收口；任何单位/复权口径差都必须阻断而非换算放行。**运行时仅注册 Tencent + Yahoo 两源**（`tools/market_data.py::_make_registry`），`get_bars` 在这两源间按注册顺序回退，不做 16 源择优/打分，也不存在覆盖 16 源的回退链；恢复多源需另开线。

## 2. 关键文件与行号

| 文件 | 行 | 内容 |
|---|---|---|
| `src/hero_quant/data/registry.py` | 模块 docstring | “白名单 16 源仅为契约枚举，运行时仅注册 Tencent + Yahoo 两源；按注册顺序回退，无 16 源择优” |
| `src/hero_quant/data/registry.py` | `def _get_data_mode` | 数据模式（settings 缓存，失败 `logger.warning` + `exc_info=True`，见 `:44`） |
| `src/hero_quant/data/registry.py` | `def _resolve_provenance` | 3 处 provenance 块的唯一 helper；`Settings` 只实例化一次（经缓存） |
| `src/hero_quant/data/registry.py` | tencent/yahoo 判定 | `lname` 含 `tencent` → `tencent`；含 `yahoo` → `yahoo` |
| `src/hero_quant/data/registry.py` | `class Provenance:` | provenance 载体（含 `source/unit/adjust/factor_asof`；复权口径见 `:97` 注释“hard-coded qfq 不得静默，tencent/akshare live 均为 qfq”） |
| `src/hero_quant/data/registry.py` | `class MarketDataRegistry:` | 注册表主体 |
| `src/hero_quant/data/registry.py` | `def register` | loader 登记 |
| `src/hero_quant/data/registry.py` | `def _provenance_adjust` | 复权标注：`tencent fqkline qfq / akshare adjust='qfq'` 硬编码前复权 → provenance 带 `adjust='qfq' + factor_asof=查询end`（`:435-436`）；`synthetic/yahoo/ccxt` 另行标注（`:448`） |
| `src/hero_quant/data/registry.py` | `def _compare_ohlcv_or_raise` | 跨源 OHLCV 比对；注释 `:455` “只比首根 close 会漏 volume/unit/OHLC 口径差（tencent 手 vs yahoo 股）” |
| `src/hero_quant/data/registry.py` | `def _infer_loader_source` | 类名映射：`Tencent→tencent`（`:490-491`）、`Yahoo→yahoo`（`:492-493`） |
| `src/hero_quant/data/registry.py` | `def _cross_source_check_bars` | 双源 bars 校验 |
| `src/hero_quant/data/registry.py` | `def _cross_source_check` | 单一签名校验（bars + Provenance 必传时校验 provenance） |
| `src/hero_quant/data/registry.py` | 手/股阻断 | “tencent 手 vs yahoo 股 100x，不得换算放行”——fail-closed |
| `src/hero_quant/data/registry.py` | `def get_bars` | 对外取数：`(bars, Provenance)` 二元组与纯 bars 兼容（`:667`）；经 `_resolve_provenance`（`:674,694,700`）+ `_provenance_adjust`（`:675,695`） |
| `src/hero_quant/data/sources.py` | 源清单 | `"tencent"`（`:7`）、`"yahoo"`（`:9`）双源主位 |
| `src/hero_quant/data/trait.py` | `class SourceTrait(Protocol):` | 源契约；`:32` `get_bars`、`:52` `health` |
| `src/hero_quant/data/loaders/tencent.py` | `class TencentLoader:` | 主源；`:97` `get_bars` |
| `src/hero_quant/data/loaders/yahoo.py` | `class YahooLoader:` | 备源；`:33` `get_bars` |
| `src/hero_quant/data/loaders/akshare_loader.py` | `class AKShareLoader:` | `:108` `TODO: confirm akshare unit; if in shares: vol = vol / 100.0`（路径含 `loaders/` 一级，行号仍 108，与 `docs/issues.md` P3-3 一致）；`:138` `get_bars` |
| `src/hero_quant/data/loaders/ccxt_loader.py` | `class CCXTLoader:` | 加密源；`:104` `get_bars` |
| `src/hero_quant/tools/registry.py` | `def get_definitions` | 工具层定义装配（数据工具经此暴露给 LLM `bind_tools`） |

## 3. 上游（谁调我）

- 链A：`AgentLoop` 经 `tools/registry` → `tools/market_data/` → `MarketDataRegistry.get_bars(:635)`。
- 数据模式经 `Settings`（`src/hero_quant/config/settings.py`，env 网关；`HERO_DATA_MODE / HERO_DATA_MARKET` 见 recon §1）。

## 4. 下游（我调谁）

- 各 `loaders/*Loader.get_bars` → 返回 `bars` + `Provenance{source, unit, adjust, factor_asof}`。
- 跨源校验失败 → `CrossSourceError`（`registry.py`，`ValueError` 子类）抛给调用方，不静默。

## 5. 降级语义

| 场景 | 语义 | 依据 |
|---|---|---|
| 主源失败 | 双源 fallback 切备源，但 provenance 必须如实改为备源（`_resolve_provenance:56`），不得挂羊头卖狗肉 | `registry.py, 56-76` |
| 手/股单位冲突 | 直接阻断（`CrossSourceError`），100x 差不得换算放行 | `registry.py` |
| loader provenance unit 非法 | `provenance unit invalid` 抛错（fail-closed） | `registry.py` |
| akshare 单位未确认 | TODO 悬置（`:108`），在确认前按当前单位透传 + provenance 标注，不得私自 `/100` | `akshare_loader.py`；P3-3 要求单测锁定一处清理一处 |
| 合成/bench 路径 | 一律 `non-PIT` 披露（见 `docs/arch/quantlib.md` §5），数据层不伪装 PIT | `src/hero_quant/backtest/bench.py` |
| 日线出口质量闸 | `MarketDataRegistry.get_bars` 返回前跑 `check_bars`，block 抛 `DataQualityError`（工具层结构化返回 `ok:False`，不落合成兜底）；synthetic / 分钟线 / 整批无 trade_date 显式写 `provenance.extra.quality_skipped` 后跳过；成功返回带 `pit.applied=False`（registry 未接 PIT，禁止下游默认拿到 PIT 数据） | `src/hero_quant/data/registry.py`、`src/hero_quant/tools/market_data.py` |

## 6. 已知问题挂钩

- P3-3 `akshare_loader.py TODO` + `memory/` 裸 `pass`：归 B 线（测试锁定）+ D 线（清理）；数据层不得新增裸 `pass`。
- 复权硬编码（tencent/akshare qfq）已显式化但仍是硬编码：后续若新增复权口径，必须同步 `_provenance_adjust(:432)` + `Provenance` 注释（`:97`），三处联动。
