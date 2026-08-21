//! hero-quant Rust core — stub exposing hot-path kernels via PyO3.
//! Minimal proof of extraction: provides sma/ema/rsi stubs with pure-Rust parity logic.
//! Full 60 kernels to be ported incrementally; this stub proves crate boundary + CI gate.

use pyo3::prelude::*;

/// Pure-Rust SMA: rolling mean with window, returns Option<f64> (None for insufficient data)
fn sma_vec(data: &[f64], window: usize) -> Vec<Option<f64>> {
    let n = if window == 0 { 20 } else { window };
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

/// Pure-Rust EMA: ewm(span, adjust=False) min_periods=1
fn ema_vec(data: &[f64], span: usize) -> Vec<f64> {
    let n = if span == 0 { 20 } else { span };
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

fn rsi_vec(data: &[f64], period: usize) -> Vec<f64> {
    let n = if period == 0 { 14 } else { period };
    if data.is_empty() {
        return vec![];
    }
    let mut gains = vec![0.0; data.len()];
    let mut losses = vec![0.0; data.len()];
    for i in 1..data.len() {
        let d = data[i] - data[i - 1];
        if d > 0.0 {
            gains[i] = d;
        } else {
            losses[i] = -d;
        }
    }
    // Wilder's smoothing alpha = 1/n
    let alpha = 1.0 / n as f64;
    let mut avg_gain = 0.0;
    let mut avg_loss = 0.0;
    let mut out = Vec::with_capacity(data.len());
    for i in 0..data.len() {
        if i == 0 {
            avg_gain = gains[i];
            avg_loss = losses[i];
        } else {
            avg_gain = alpha * gains[i] + (1.0 - alpha) * avg_gain;
            avg_loss = alpha * losses[i] + (1.0 - alpha) * avg_loss;
        }
        let rsi = if avg_loss == 0.0 {
            if avg_gain == 0.0 { 50.0 } else { 100.0 }
        } else {
            let rs = avg_gain / avg_loss;
            100.0 - (100.0 / (1.0 + rs))
        };
        // first element synthetic uses 50
        if i == 0 {
            out.push(50.0);
        } else {
            out.push(rsi.clamp(0.0, 100.0));
        }
    }
    out
}

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
        if cummax != 0.0 {
            let dd = v / cummax - 1.0;
            if dd < mdd {
                mdd = dd;
            }
        }
    }
    if mdd > 0.0 { 0.0 } else { mdd }
}

// ── PyO3 wrappers ──

#[pyfunction]
fn sma(data: Vec<f64>, window: usize) -> PyResult<Vec<Option<f64>>> {
    Ok(sma_vec(&data, window))
}

#[pyfunction]
fn ema(data: Vec<f64>, span: usize) -> PyResult<Vec<f64>> {
    Ok(ema_vec(&data, span))
}

#[pyfunction]
fn rsi(data: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    Ok(rsi_vec(&data, period))
}

#[pyfunction]
fn max_drawdown(equity: Vec<f64>) -> PyResult<f64> {
    Ok(max_drawdown_vec(&equity))
}

#[pyfunction]
fn bollinger(data: Vec<f64>, window: usize, num_std: f64) -> PyResult<(Vec<Option<f64>>, Vec<Option<f64>>, Vec<Option<f64>>)> {
    let n = if window == 0 { 20 } else { window };
    let k = if num_std.is_nan() { 2.0 } else { num_std };
    let mid = sma_vec(&data, n);
    // std dev rolling
    let mut upper = Vec::with_capacity(data.len());
    let mut lower = Vec::with_capacity(data.len());
    for i in 0..data.len() {
        if i + 1 < n {
            upper.push(None);
            lower.push(None);
        } else {
            let slice = &data[i + 1 - n..=i];
            let m = mid[i].unwrap_or(0.0);
            let var: f64 = slice.iter().map(|v| (v - m).powi(2)).sum::<f64>() / (n as f64 - 1.0).max(1.0);
            let std = var.sqrt();
            upper.push(Some(m + k * std));
            lower.push(Some(m - k * std));
        }
    }
    Ok((mid, upper, lower))
}

#[pyfunction]
fn macd(data: Vec<f64>, fast: usize, slow: usize, signal: usize) -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
    let ef = ema_vec(&data, if fast == 0 { 12 } else { fast });
    let es = ema_vec(&data, if slow == 0 { 26 } else { slow });
    let macd_line: Vec<f64> = ef.iter().zip(es.iter()).map(|(a, b)| a - b).collect();
    let signal_line = ema_vec(&macd_line, if signal == 0 { 9 } else { signal });
    let hist: Vec<f64> = macd_line.iter().zip(signal_line.iter()).map(|(a, b)| a - b).collect();
    Ok((macd_line, signal_line, hist))
}

#[pymodule]
fn quantlib(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sma, m)?)?;
    m.add_function(wrap_pyfunction!(ema, m)?)?;
    m.add_function(wrap_pyfunction!(rsi, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown, m)?)?;
    m.add_function(wrap_pyfunction!(bollinger, m)?)?;
    m.add_function(wrap_pyfunction!(macd, m)?)?;
    m.add("__version__", "0.1.0")?;
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
        let (a, b, c) = macd(v.clone(), 2, 3, 2).unwrap();
        assert_eq!(a.len(), v.len());
        assert_eq!(b.len(), v.len());
        assert_eq!(c.len(), v.len());
    }
}
