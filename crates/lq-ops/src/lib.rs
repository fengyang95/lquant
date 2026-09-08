//! lq-ops：因子重算子的 Rust 实现。
//!
//! 通过 pyo3-polars 的 `#[polars_expr]` 插件机制暴露 —— GIL-free，
//! 且能自动享受 Polars 的并行与 lazy/streaming 优化。
//!
//! 每个算子在 Python 侧都有参考实现（`lquant/_rust/ops_ref.py`），
//! 加载失败自动降级，且两者单测对拍。

use polars::prelude::*;
use pyo3_polars::derive::polars_expr;

/// 时序相关系数：滚动窗口内的 Pearson 相关。
/// 对应算子 Ts_Corr(x, y, n)，category = TS。
#[polars_expr(output_type=Float64)]
fn ts_corr(inputs: &[Series]) -> PolarsResult<Series> {
    let x = &inputs[0];
    let y = &inputs[1];
    let n = inputs[2].get(0)?.try_extract::<usize>()?;

    let xa = x.cast(&DataType::Float64)?;
    let ya = y.cast(&DataType::Float64)?;
    let xf = xa.f64()?;
    let yf = ya.f64()?;

    let mut out: Vec<Option<f64>> = Vec::with_capacity(xf.len());
    for i in 0..xf.len() {
        if i + 1 < n {
            out.push(None);
            continue;
        }
        let (mut sx, mut sy, mut sxx, mut syy, mut sxy, mut cnt) = (0.0, 0.0, 0.0, 0.0, 0.0, 0usize);
        for j in (i + 1 - n)..=i {
            match (xf.get(j), yf.get(j)) {
                (Some(a), Some(b)) => {
                    sx += a; sy += b; sxx += a * a; syy += b * b; sxy += a * b; cnt += 1;
                }
                _ => {}
            }
        }
        if cnt < n {
            out.push(None);
            continue;
        }
        let nf = cnt as f64;
        let cov = sxy / nf - (sx / nf) * (sy / nf);
        let vx = sxx / nf - (sx / nf).powi(2);
        let vy = syy / nf - (sy / nf).powi(2);
        if vx <= 0.0 || vy <= 0.0 {
            out.push(None);
        } else {
            out.push(Some(cov / (vx * vy).sqrt()));
        }
    }
    Ok(Series::new("ts_corr".into(), out))
}

/// 回归 beta：Ts_Regbeta(y, x, n)。
#[polars_expr(output_type=Float64)]
fn ts_regbeta(inputs: &[Series]) -> PolarsResult<Series> {
    let y = inputs[0].cast(&DataType::Float64)?;
    let x = inputs[1].cast(&DataType::Float64)?;
    let n = inputs[2].get(0)?.try_extract::<usize>()?;
    let yf = y.f64()?;
    let xf = x.f64()?;

    let mut out: Vec<Option<f64>> = Vec::with_capacity(yf.len());
    for i in 0..yf.len() {
        if i + 1 < n {
            out.push(None);
            continue;
        }
        let (mut sx, mut sy, mut sxx, mut sxy, mut cnt) = (0.0, 0.0, 0.0, 0.0, 0usize);
        for j in (i + 1 - n)..=i {
            match (xf.get(j), yf.get(j)) {
                (Some(a), Some(b)) => { sx += a; sy += b; sxx += a * a; sxy += a * b; cnt += 1; }
                _ => {}
            }
        }
        let nf = cnt as f64;
        let cov = sxy / nf - (sx / nf) * (sy / nf);
        let vx = sxx / nf - (sx / nf).powi(2);
        if cnt < n || vx <= 0.0 {
            out.push(None);
        } else {
            out.push(Some(cov / vx));
        }
    }
    Ok(Series::new("ts_regbeta".into(), out))
}

use pyo3::prelude::*;

/// 注册为 Polars 插件命名空间 `lq_ops`。
#[pymodule]
fn lq_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
