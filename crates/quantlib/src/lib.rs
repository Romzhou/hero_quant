//! Quantlib Rust 性能内核
//! 定位：纯 Rust 实现的行情与回测热路径指标计算，供 Python 通过 PyO3 桥接调用（`quantlib.rust`）。
//! 已实现：SMA / EMA / RSI / Bollinger / MACD / max_drawdown 等，向 60+ kernels 渐进迁移。
//! 约定：Python 侧默认（SMA/EMA 20、RSI 14、MACD 12/26/9）通过 `Option` 默认值注入；`window==0`、NaN/Inf 视为调用错误并抛 `PyValueError`，不再静默回落。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// ── 命名常量：消除魔法数，单点维护默认值 ──
const DEFAULT_SMA_WINDOW: usize = 20;
const DEFAULT_EMA_SPAN: usize = 20;
const DEFAULT_RSI_PERIOD: usize = 14;
const DEFAULT_BOLLINGER_WINDOW: usize = 20;
const DEFAULT_BOLLINGER_K: f64 = 2.0;
const DEFAULT_MACD_FAST: usize = 12;
const DEFAULT_MACD_SLOW: usize = 26;
const DEFAULT_MACD_SIGNAL: usize = 9;

// ── 校验辅助 ──

/// 校验切片中无非有限值（NaN/Inf），否则返回 `PyValueError`。
fn validate_finite(data: &[f64]) -> PyResult<()> {
    for &v in data {
        if !v.is_finite() {
            return Err(PyValueError::new_err(
                "data contains non-finite value (NaN/Inf)",
            ));
        }
    }
    Ok(())
}

/// 校验窗口为正，非 0。
fn validate_window(n: usize, name: &str) -> PyResult<usize> {
    if n == 0 {
        return Err(PyValueError::new_err(format!("{name} must be >0")));
    }
    Ok(n)
}

/// 校验 Bollinger 倍数有限且非负。
fn validate_num_std(k: f64) -> PyResult<f64> {
    if !k.is_finite() {
        return Err(PyValueError::new_err(
            "num_std must be finite (got NaN/Inf)",
        ));
    }
    if k < 0.0 {
        return Err(PyValueError::new_err(format!(
            "num_std must be >=0, got {k}"
        )));
    }
    Ok(k)
}

// ── 纯 Rust 内核（已校验输入，返回确定性结果） ──

/// SMA 滚动均值
///
/// 窗口为 `n`（>0），返回与输入等长向量，不足窗口处为 `None`。
/// 复杂度 O(n)，单次遍历维护滚动和，数值误差通过增量加减控制；超长序列可考虑 Kahan
/// 但当前 warm-up 语义保证首 `n-1` 为 None，后续精确为 `sum/n`。
fn sma_vec(data: &[f64], window: usize) -> Vec<Option<f64>> {
    let n = window;
    let mut out = Vec::with_capacity(data.len());
    let mut sum = 0.0;
    for i in 0..data.len() {
        sum += data[i];
        if i >= n {
            sum -= data[i - n];
        }
        if i + 1 >= n {
            out.push(Some(sum / n as f64));
        } else {
            out.push(None);
        }
    }
    out
}

/// EMA 指数移动平均（EWMA，`adjust=False`，`min_periods=1`）
///
/// `span` 为周期（>0），`alpha = 2/(n+1)`，首值取首个输入，后续按 `alpha*v + (1-alpha)*prev` 递推。
fn ema_vec(data: &[f64], span: usize) -> Vec<f64> {
    let n = span;
    let alpha = 2.0 / (n as f64 + 1.0);
    let mut out = Vec::with_capacity(data.len());
    let mut prev: Option<f64> = None;
    for &v in data {
        let cur = match prev {
            None => v,
            Some(p) => alpha * v + (1.0 - alpha) * p,
        };
        out.push(cur);
        prev = Some(cur);
    }
    out
}

/// RSI 相对强弱指标（Wilder 平滑，首值 50，warm-up 期前 `n` 个为 50）
///
/// 采用经典 Wilder：首 `n` 个增益/损失的 SMA 为种子，之后按 `avg = (prev*(n-1)+curr)/n` 递推；
/// `RS = avg_gain/avg_loss`，`RSI = 100 - 100/(1+RS)`；全涨 100，平盘 50。为保持与 Python
/// 短序列可用性，`data.len() <= n` 时返回全 50；`i < n` warm-up 亦为 50，避免零种子偏置。
fn rsi_vec(data: &[f64], period: usize) -> Vec<f64> {
    let n = period;
    if data.is_empty() {
        return vec![];
    }
    if data.len() <= n {
        // 样本不足以 Wilder 种子，返回中性值以保持可调用性；上层可通过长度判断 warm-up
        return vec![50.0; data.len()];
    }
    // 种子：前 n 个差分的 SMA
    let mut gains_sum = 0.0;
    let mut losses_sum = 0.0;
    for i in 1..=n {
        let d = data[i] - data[i - 1];
        if d > 0.0 {
            gains_sum += d;
        } else {
            losses_sum += -d;
        }
    }
    let mut avg_gain = gains_sum / n as f64;
    let mut avg_loss = losses_sum / n as f64;

    let mut out = Vec::with_capacity(data.len());
    for i in 0..data.len() {
        if i == 0 {
            out.push(50.0);
        } else if i < n {
            // warm-up 期：尚未完成种子，发射中性值而非偏置的 EWMA
            out.push(50.0);
        } else if i == n {
            let rsi = if avg_loss == 0.0 {
                if avg_gain == 0.0 { 50.0 } else { 100.0 }
            } else {
                let rs = avg_gain / avg_loss;
                100.0 - (100.0 / (1.0 + rs))
            };
            out.push(rsi.clamp(0.0, 100.0));
        } else {
            let d = data[i] - data[i - 1];
            let gain = d.max(0.0);
            let loss = (-d).max(0.0);
            avg_gain = (avg_gain * (n as f64 - 1.0) + gain) / n as f64;
            avg_loss = (avg_loss * (n as f64 - 1.0) + loss) / n as f64;
            let rsi = if avg_loss == 0.0 {
                if avg_gain == 0.0 { 50.0 } else { 100.0 }
            } else {
                let rs = avg_gain / avg_loss;
                100.0 - (100.0 / (1.0 + rs))
            };
            out.push(rsi.clamp(0.0, 100.0));
        }
    }
    out
}

/// 最大回撤（max drawdown）
///
/// 遍历权益曲线维护 `cummax`，`dd = v/cummax - 1` 取最小负值；空序列返回 0。
/// 调用前已校验无非有限值；`cummax == 0` 时跳过除法（零起点权益无回撤语义），
/// 但若后续出现 `v < 0` 且 `cummax == 0` 视为极端回撤，返回 `f64::NEG_INFINITY` 语义由上层处理。
/// 不变量：`mdd <= 0.0`，无回测则 0。
fn max_drawdown_vec(equity: &[f64]) -> f64 {
    if equity.is_empty() {
        return 0.0;
    }
    let mut cummax = equity[0];
    let mut mdd: f64 = 0.0;
    for &v in equity {
        if v > cummax {
            cummax = v;
        }
        if cummax == 0.0 {
            // 零峰值时除法无意义；若负权益则视为无限回撤，避免静默 0
            if v < 0.0 {
                return f64::NEG_INFINITY;
            }
            continue;
        }
        let dd = v / cummax - 1.0;
        if dd < mdd {
            mdd = dd;
        }
    }
    // invariant: mdd <= 0.0, 0 means no drawdown (remove dead branch `if mdd>0 {0}`)
    mdd
}

// ── PyO3 导出（属性参数不动，doc 用于 Python __doc__ 亦可中文） ──

/// PyO3 导出：SMA 滚动均值
///
/// - `data`: 价格序列 `Vec<f64>`，禁止 NaN/Inf（抛 `PyValueError`）
/// - `window`: 窗口大小（`None` 默认为 20，必须 >0）
#[pyfunction]
#[pyo3(signature = (data, window=None))]
fn sma(data: Vec<f64>, window: Option<usize>) -> PyResult<Vec<Option<f64>>> {
    let n = validate_window(window.unwrap_or(DEFAULT_SMA_WINDOW), "window")?;
    validate_finite(&data)?;
    Ok(sma_vec(&data, n))
}

/// PyO3 导出：EMA 指数移动平均
///
/// - `span`: 周期（`None` 默认为 20，必须 >0）
#[pyfunction]
#[pyo3(signature = (data, span=None))]
fn ema(data: Vec<f64>, span: Option<usize>) -> PyResult<Vec<f64>> {
    let n = validate_window(span.unwrap_or(DEFAULT_EMA_SPAN), "span")?;
    validate_finite(&data)?;
    Ok(ema_vec(&data, n))
}

/// PyO3 导出：RSI（Wilder 平滑，首值 50，warm-up 前 n 为 50）
///
/// - `period`: 周期（`None` 默认为 14，必须 >0）
#[pyfunction]
#[pyo3(signature = (data, period=None))]
fn rsi(data: Vec<f64>, period: Option<usize>) -> PyResult<Vec<f64>> {
    let n = validate_window(period.unwrap_or(DEFAULT_RSI_PERIOD), "period")?;
    validate_finite(&data)?;
    Ok(rsi_vec(&data, n))
}

/// PyO3 导出：最大回撤
///
/// - `equity`: 权益曲线 `Vec<f64>`，禁止 NaN/Inf
#[pyfunction]
fn max_drawdown(equity: Vec<f64>) -> PyResult<f64> {
    if equity.is_empty() {
        return Ok(0.0);
    }
    // 显式校验非有限值，避免静默忽略
    for &v in &equity {
        if !v.is_finite() {
            return Err(PyValueError::new_err(
                "equity contains non-finite value (NaN/Inf)",
            ));
        }
    }
    Ok(max_drawdown_vec(&equity))
}

/// PyO3 导出：布林带（Bollinger Bands）
///
/// 中轨为 SMA(n)，上下轨 `m ± k·σ`，σ 为样本标准差（分母 `n-1`），`k` 默认为 2.0；
/// 不足窗口处为 None。`window==1` 时 σ 未定义，返回 `(mid, mid, mid)`（带宽为 0）。
/// 采用 O(n) 滚动 `sum`/`sum_sq`，避免每根 K 线全窗口迭代。
#[pyfunction]
#[pyo3(signature = (data, window=None, num_std=None))]
fn bollinger(
    data: Vec<f64>,
    window: Option<usize>,
    num_std: Option<f64>,
) -> PyResult<(Vec<Option<f64>>, Vec<Option<f64>>, Vec<Option<f64>>)> {
    let n = validate_window(window.unwrap_or(DEFAULT_BOLLINGER_WINDOW), "window")?;
    let k = validate_num_std(num_std.unwrap_or(DEFAULT_BOLLINGER_K))?;
    validate_finite(&data)?;

    // O(n) 滚动：维护 sum 与 sum_sq
    let mut mid = Vec::with_capacity(data.len());
    let mut upper = Vec::with_capacity(data.len());
    let mut lower = Vec::with_capacity(data.len());
    let mut sum = 0.0_f64;
    let mut sum_sq = 0.0_f64;

    for i in 0..data.len() {
        sum += data[i];
        sum_sq += data[i] * data[i];
        if i >= n {
            sum -= data[i - n];
            sum_sq -= data[i - n] * data[i - n];
        }
        if i + 1 < n {
            mid.push(None);
            upper.push(None);
            lower.push(None);
        } else {
            let m = sum / n as f64;
            mid.push(Some(m));
            let var = if n == 1 {
                0.0
            } else {
                // 样本方差： (sum_sq - sum^2/n)/(n-1)，钳制浮点负误差
                let v = (sum_sq - sum * sum / n as f64) / (n as f64 - 1.0);
                v.max(0.0)
            };
            let std = var.sqrt();
            upper.push(Some(m + k * std));
            lower.push(Some(m - k * std));
        }
    }
    Ok((mid, upper, lower))
}

/// PyO3 导出：MACD
///
/// `macd = EMA_fast - EMA_slow`，`signal = EMA(macd)`，`hist = macd - signal`；
/// `None` 时回退默认 12/26/9，`0` 视为错误（抛 `PyValueError`）。
#[pyfunction]
#[pyo3(signature = (data, fast=None, slow=None, signal=None))]
fn macd(
    data: Vec<f64>,
    fast: Option<usize>,
    slow: Option<usize>,
    signal: Option<usize>,
) -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
    let f = validate_window(fast.unwrap_or(DEFAULT_MACD_FAST), "fast")?;
    let s = validate_window(slow.unwrap_or(DEFAULT_MACD_SLOW), "slow")?;
    let sig = validate_window(signal.unwrap_or(DEFAULT_MACD_SIGNAL), "signal")?;
    validate_finite(&data)?;
    let ef = ema_vec(&data, f);
    let es = ema_vec(&data, s);
    let macd_line: Vec<f64> = ef.iter().zip(es.iter()).map(|(a, b)| a - b).collect();
    let signal_line = ema_vec(&macd_line, sig);
    let hist: Vec<f64> = macd_line
        .iter()
        .zip(signal_line.iter())
        .map(|(a, b)| a - b)
        .collect();
    Ok((macd_line, signal_line, hist))
}

/// Python 模块入口：注册全部指标函数并暴露 __version__
#[pymodule]
fn quantlib(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sma, m)?)?;
    m.add_function(wrap_pyfunction!(ema, m)?)?;
    m.add_function(wrap_pyfunction!(rsi, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown, m)?)?;
    m.add_function(wrap_pyfunction!(bollinger, m)?)?;
    m.add_function(wrap_pyfunction!(macd, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sma_basic() {
        let v = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let out = sma_vec(&v, 3);
        assert_eq!(out, vec![None, None, Some(2.0), Some(3.0), Some(4.0)]);
    }

    #[test]
    fn test_ema_first_equals_first() {
        let v = vec![1.0, 2.0, 3.0];
        let out = ema_vec(&v, 3);
        assert_eq!(out[0], 1.0);
        assert!(out.len() == 3);
    }

    #[test]
    fn test_rsi_flat_is_50() {
        let v = vec![10.0; 20];
        let out = rsi_vec(&v, 14);
        for &x in &out {
            assert!((x - 50.0).abs() < 1e-9);
        }
    }

    #[test]
    fn test_max_drawdown_zero_for_rising() {
        let v = vec![1.0, 2.0, 3.0];
        assert_eq!(max_drawdown_vec(&v), 0.0);
    }

    #[test]
    fn test_max_drawdown_negative() {
        let v = vec![100.0, 80.0, 90.0];
        let mdd = max_drawdown_vec(&v);
        assert!(mdd < 0.0);
        assert!((mdd - (-0.2)).abs() < 1e-9);
    }

    #[test]
    fn test_macd_lengths() {
        let v = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let (a, b, c) = macd(v.clone(), Some(2), Some(3), Some(2)).unwrap();
        assert_eq!(a.len(), v.len());
        assert_eq!(b.len(), v.len());
        assert_eq!(c.len(), v.len());
    }

    // ── 新增：修正行为的 TDD 覆盖 ──

    #[test]
    fn test_validate_window_zero_err() {
        assert!(validate_window(0, "window").is_err());
    }

    #[test]
    fn test_validate_finite_nan_err() {
        let v = vec![1.0, f64::NAN, 2.0];
        assert!(validate_finite(&v).is_err());
    }

    #[test]
    fn test_validate_finite_inf_err() {
        let v = vec![1.0, f64::INFINITY];
        assert!(validate_finite(&v).is_err());
    }

    #[test]
    fn test_sma_rejects_nonfinite() {
        let v = vec![1.0, f64::NAN, 3.0];
        assert!(sma(v, Some(2)).is_err());
    }

    #[test]
    fn test_ema_rejects_zero_window() {
        let v = vec![1.0, 2.0, 3.0];
        assert!(ema(v, Some(0)).is_err());
    }

    #[test]
    fn test_rsi_rejects_nonfinite() {
        let v = vec![1.0, f64::NAN];
        assert!(rsi(v, Some(14)).is_err());
    }

    #[test]
    fn test_bollinger_window_one_collapses() {
        let v = vec![1.0, 2.0, 3.0];
        let (mid, up, low) = bollinger(v.clone(), Some(1), Some(2.0)).unwrap();
        assert_eq!(mid, vec![Some(1.0), Some(2.0), Some(3.0)]);
        assert_eq!(up, mid);
        assert_eq!(low, mid);
    }

    #[test]
    fn test_bollinger_rejects_nan_k() {
        let v = vec![1.0, 2.0, 3.0];
        assert!(bollinger(v, Some(2), Some(f64::NAN)).is_err());
    }

    #[test]
    fn test_bollinger_rejects_negative_k() {
        let v = vec![1.0, 2.0, 3.0];
        assert!(bollinger(v, Some(2), Some(-1.0)).is_err());
    }

    #[test]
    fn test_bollinger_rolling_correctness() {
        // 比对 O(n*window) 朴素实现
        let v = vec![1.0, 2.0, 3.0, 4.0, 5.0, 6.0];
        let n = 3usize;
        let k = 2.0;
        let (mid, up, low) = bollinger(v.clone(), Some(n), Some(k)).unwrap();
        // 朴素计算验证
        for i in 0..v.len() {
            if i + 1 < n {
                assert!(mid[i].is_none());
            } else {
                let slice = &v[i + 1 - n..=i];
                let m: f64 = slice.iter().sum::<f64>() / n as f64;
                let var: f64 = slice.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (n as f64 - 1.0);
                let std = var.sqrt();
                assert!((mid[i].unwrap() - m).abs() < 1e-9);
                assert!((up[i].unwrap() - (m + k * std)).abs() < 1e-9);
                assert!((low[i].unwrap() - (m - k * std)).abs() < 1e-9);
            }
        }
    }

    #[test]
    fn test_max_drawdown_rejects_nan() {
        let v = vec![1.0, f64::NAN, 2.0];
        assert!(max_drawdown(v).is_err());
    }

    #[test]
    fn test_max_drawdown_zero_start_negative() {
        // cummax==0 且 v<0 视为极端回撤，无静默 0
        let v = vec![0.0, -1.0, 1.0];
        let mdd = max_drawdown_vec(&v);
        assert!(mdd.is_infinite() && mdd.is_sign_negative());
    }

    #[test]
    fn test_rsi_wilder_seed_not_biased() {
        // 单调上涨：Wilder 种子应使 RSI 接近 100 而非受零种子拖累
        let mut v = vec![10.0];
        for i in 1..30 {
            v.push(10.0 + i as f64);
        }
        let out = rsi_vec(&v, 14);
        // warm-up 前 14 为 50，之后应快速趋近 100
        assert!(out[14] > 70.0, "seed biased: {}", out[14]);
        assert!(out[29] > 90.0);
    }

    #[test]
    fn test_macd_rejects_zero_fast() {
        let v = vec![1.0, 2.0, 3.0];
        assert!(macd(v, Some(0), Some(26), Some(9)).is_err());
    }
}
