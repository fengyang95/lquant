//! lq-metrics：IC / 绩效指标。

use pyo3::prelude::*;

/// 截面 IC（Spearman rank correlation）。
#[pyfunction]
fn rank_ic(factor: Vec<f64>, fwd_ret: Vec<f64>) -> f64 {
    let n = factor.len().min(fwd_ret.len());
    if n < 3 {
        return f64::NAN;
    }
    let rf = rank(&factor[..n]);
    let rr = rank(&fwd_ret[..n]);
    let mf: f64 = rf.iter().sum::<f64>() / n as f64;
    let mr: f64 = rr.iter().sum::<f64>() / n as f64;
    let mut cov = 0.0;
    let mut v1 = 0.0;
    let mut v2 = 0.0;
    for i in 0..n {
        let a = rf[i] - mf;
        let b = rr[i] - mr;
        cov += a * b;
        v1 += a * a;
        v2 += b * b;
    }
    if v1 <= 0.0 || v2 <= 0.0 {
        f64::NAN
    } else {
        cov / (v1 * v2).sqrt()
    }
}

fn rank(v: &[f64]) -> Vec<f64> {
    let mut idx: Vec<usize> = (0..v.len()).collect();
    idx.sort_by(|&a, &b| v[a].partial_cmp(&v[b]).unwrap());
    let mut r = vec![0.0; v.len()];
    let mut i = 0usize;
    while i < idx.len() {
        let mut j = i;
        while j + 1 < idx.len() && v[idx[j + 1]] == v[idx[i]] {
            j += 1;
        }
        let avg = (i + j) as f64 / 2.0 + 1.0;
        for k in i..=j {
            r[idx[k]] = avg;
        }
        i = j + 1;
    }
    r
}

/// 最大回撤。
#[pyfunction]
fn max_drawdown(nav: Vec<f64>) -> f64 {
    let mut peak = f64::MIN;
    let mut mdd = 0.0f64;
    for v in nav {
        peak = peak.max(v);
        if peak > 0.0 {
            mdd = mdd.max((peak - v) / peak);
        }
    }
    mdd
}

#[pymodule]
fn lq_metrics(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rank_ic, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn perfect_positive_ic() {
        let f = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let r = vec![0.1, 0.2, 0.3, 0.4, 0.5];
        assert!((rank_ic(f, r) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn drawdown() {
        assert!((max_drawdown(vec![1.0, 1.2, 0.6, 1.0]) - 0.5).abs() < 1e-9);
    }
}
