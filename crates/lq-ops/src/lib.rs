//! lq-ops：因子重算子的 Rust 实现。
//!
//! 用 `#[pyfunction]` + `Vec<Option<f64>>` 暴露（与 lq-metrics 同构），
//! 而非 `#[polars_expr]` polars 插件：
//! - 仓库 Rust 侧 polars 0.49 与 Python 侧 polars 1.44 的插件 FFI 协议不同，
//!   跨版本调用会 ABI 错配；`#[pyfunction]` 走 pyo3 纯边界，彻底绕开。
//! - 确定性数组接口与 `lquant/_rust/ops_ref.py` 逐字段镜像，对拍可逐位比较。
//!
//! 每个算子都在 Python 侧有参考实现（`lquant/_rust/ops_ref.py`），
//! 两者单测对拍（tests/unit/test_rust_alignment.py）。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// 时序相关系数：滚动窗口内的 Pearson 相关。对应参考 `ops_ref.ts_corr`。
///
/// 语义与参考逐一对齐：`i + 1 < n` 窗口不足 → None；窗口内有空值或
/// `cnt < n` → None；零方差 → None（而非 NaN）。
#[pyfunction]
fn ts_corr(
    x: Vec<Option<f64>>,
    y: Vec<Option<f64>>,
    n: usize,
) -> PyResult<Vec<Option<f64>>> {
    validate_xy(&x, &y, n)?;
    let len = x.len();
    let mut out = Vec::with_capacity(len);
    for i in 0..len {
        if i + 1 < n {
            out.push(None);
            continue;
        }
        let (mut sx, mut sy, mut sxx, mut syy, mut sxy, mut cnt) =
            (0.0, 0.0, 0.0, 0.0, 0.0, 0usize);
        for j in (i + 1 - n)..=i {
            match (x[j], y[j]) {
                (Some(a), Some(b)) => {
                    sx += a;
                    sy += b;
                    sxx += a * a;
                    syy += b * b;
                    sxy += a * b;
                    cnt += 1;
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
    Ok(out)
}

/// 回归 beta（y 对 x）。对应参考 `ops_ref.ts_regbeta`。
#[pyfunction]
fn ts_regbeta(
    y: Vec<Option<f64>>,
    x: Vec<Option<f64>>,
    n: usize,
) -> PyResult<Vec<Option<f64>>> {
    validate_xy(&y, &x, n)?;
    let len = y.len();
    let mut out = Vec::with_capacity(len);
    for i in 0..len {
        if i + 1 < n {
            out.push(None);
            continue;
        }
        let (mut sx, mut sy, mut sxx, mut sxy, mut cnt) = (0.0, 0.0, 0.0, 0.0, 0usize);
        for j in (i + 1 - n)..=i {
            match (x[j], y[j]) {
                (Some(a), Some(b)) => {
                    sx += a;
                    sy += b;
                    sxx += a * a;
                    sxy += a * b;
                    cnt += 1;
                }
                _ => {}
            }
        }
        if cnt < n || cnt == 0 {
            out.push(None);
            continue;
        }
        let nf = cnt as f64;
        let cov = sxy / nf - (sx / nf) * (sy / nf);
        let vx = sxx / nf - (sx / nf).powi(2);
        if vx <= 0.0 {
            out.push(None);
        } else {
            out.push(Some(cov / vx));
        }
    }
    Ok(out)
}

/// 入口校验：两序列必须等长、窗口 n > 0。脏输入直接报错而非越界 panic。
fn validate_xy(x: &[Option<f64>], y: &[Option<f64>], n: usize) -> PyResult<()> {
    if x.len() != y.len() {
        return Err(PyValueError::new_err(format!(
            "x/y 长度不一致: {} vs {}",
            x.len(),
            y.len()
        )));
    }
    if n == 0 {
        return Err(PyValueError::new_err("窗口长度 n 必须大于 0"));
    }
    Ok(())
}

/// 模块：显式注册算子为可直呼的 Python 函数。
#[pymodule]
fn lq_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(ts_corr, m)?)?;
    m.add_function(wrap_pyfunction!(ts_regbeta, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn length_mismatch_errors() {
        let x = vec![Some(1.0), Some(2.0)];
        let y = vec![Some(1.0)];
        assert!(ts_corr(x, y, 2).is_err());
    }

    #[test]
    fn zero_window_errors() {
        let x = vec![Some(1.0), Some(2.0)];
        assert!(ts_corr(x.clone(), x, 0).is_err());
    }

    #[test]
    fn corr_insufficient_window_is_none() {
        let x = vec![Some(1.0), Some(2.0), Some(3.0)];
        let out = ts_corr(x.clone(), x, 5).unwrap();
        assert!(out.iter().all(|v| v.is_none()));
    }
}