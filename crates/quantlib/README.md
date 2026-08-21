# quantlib — hero-quant Rust core (stub)

Minimal stub extracted for open-source boundary proof. Provides vectorized kernels
(sma/ema/rsi/bollinger/macd/max_drawdown) via PyO3. Full 60 kernels planned.

Build: `cargo build --manifest-path crates/quantlib/Cargo.toml`
Test:  `cargo test -p quantlib`
Python: `from hero_quant.quantlib.rust import sma` (falls back to Python if not compiled)
