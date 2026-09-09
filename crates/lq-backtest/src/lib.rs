//! lq-backtest：撮合引擎 + 账户状态机。
//!
//! 通过 PyO3 + Arrow C Data Interface 零拷贝与 Python 交互。
//! 撮合规则全部由外部 RuleSet（YAML）传入，本 crate 不含任何硬编码规则 ——
//! 这是能支持 ETF T+0、印花税历史区间的前提。

use pyo3::prelude::*;

#[derive(Clone, Copy, Debug)]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Debug)]
pub struct InstrumentRules {
    pub commission_rate: f64,
    pub commission_min: f64,
    pub commission_per_order: bool,
    pub transfer_fee_rate: f64,
    pub tax_rate: f64,
    pub lot_size: f64,
    pub sellable_after_days: u8,
}

#[derive(Clone, Debug)]
pub struct Order {
    pub symbol: String,
    pub side: Side,
    pub qty: f64,
    pub filled_qty: f64,
    pub cum_amount: f64,   // 本订单累计成交额
    pub paid_comm: f64,    // 本订单已付佣金
}

#[derive(Debug, Default)]
pub struct Fill {
    pub symbol: String,
    pub qty: f64,
    pub price: f64,
    pub fee: f64,
}
/// 撮合：返回成交与费用。
/// 最低佣金按订单累计（分多次成交只收一次）。
/// `max_qty`：单次撮合的最大成交量（None = 剩余全部成交），用于分批成交。
pub fn match_order(order: &mut Order, price: f64, r: &InstrumentRules, max_qty: Option<f64>) -> Option<Fill> {
    let remain = order.qty - order.filled_qty;
    let fillable = max_qty.unwrap_or(remain).min(remain);
    let qty = (fillable / r.lot_size).floor() * r.lot_size;
    if qty <= 0.0 {
        return None;
    }
    let amount = qty * price;
    let transfer = amount * r.transfer_fee_rate;
    let tax = match order.side {
        Side::Sell => amount * r.tax_rate,
        Side::Buy => 0.0,
    };

    // 最低佣金按订单累计：整个订单佣金 = max(min, 累计成交额 * rate)。
    // 分多次成交时后续只补差额（可能为零），绝不重复收 5 元。
    let cum = order.cum_amount + amount;
    let target = if r.commission_per_order {
        r.commission_min.max(cum * r.commission_rate)
    } else {
        r.commission_min.max(amount * r.commission_rate)
    };
    let comm = (target - order.paid_comm).max(0.0);
    order.cum_amount = cum;
    order.paid_comm += comm;
    let fee = comm + transfer + tax;

    order.filled_qty += qty;
    Some(Fill { symbol: order.symbol.clone(), qty, price, fee })
}

#[pyfunction]
fn match_order_py(symbol: &str, is_buy: bool, qty: f64, price: f64,
                  commission_rate: f64, commission_min: f64,
                  transfer_fee_rate: f64, tax_rate: f64, lot_size: f64) -> (f64, f64) {
    let mut o = Order {
        symbol: symbol.to_string(),
        side: if is_buy { Side::Buy } else { Side::Sell },
        qty, filled_qty: 0.0, cum_amount: 0.0, paid_comm: 0.0,
    };
    let r = InstrumentRules {
        commission_rate, commission_min, commission_per_order: true,
        transfer_fee_rate, tax_rate, lot_size, sellable_after_days: 1,
    };
    match match_order(&mut o, price, &r, None) {
        Some(f) => (f.qty, f.fee),
        None => (0.0, 0.0),
    }
}

#[pymodule]
fn lq_backtest(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(match_order_py, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn etf_no_stamp_tax() {
        let mut o = Order { symbol: "510300.SH".into(), side: Side::Sell,
                            qty: 1000.0, filled_qty: 0.0, cum_amount: 0.0, paid_comm: 0.0 };
        let r = InstrumentRules { commission_rate: 0.00025, commission_min: 5.0,
                                  commission_per_order: true, transfer_fee_rate: 0.0,
                                  tax_rate: 0.0, lot_size: 100.0, sellable_after_days: 0 };
        let f = match_order(&mut o, 4.0, &r, None).unwrap();
        assert_eq!(f.qty, 1000.0);
        // 4000 元 * 0.00025 = 1 元 < 最低 5 元 → 补到 5 元，且无印花税
        assert_eq!(f.fee, 5.0);
    }

    #[test]
    fn min_commission_once_per_order() {
        // 订单分两次成交：5 元最低佣金只应收一次
        let mut o = Order { symbol: "600000.SH".into(), side: Side::Buy,
                            qty: 400.0, filled_qty: 0.0,
                            cum_amount: 0.0, paid_comm: 0.0 };
        let r = InstrumentRules { commission_rate: 0.00025, commission_min: 5.0,
                                  commission_per_order: true, transfer_fee_rate: 0.0,
                                  tax_rate: 0.0, lot_size: 100.0, sellable_after_days: 1 };
        let f1 = match_order(&mut o, 10.0, &r, Some(200.0)).unwrap();
        let f2 = match_order(&mut o, 10.0, &r, Some(200.0)).unwrap();
        assert_eq!(f1.qty, 200.0);
        assert_eq!(f2.qty, 200.0);
        assert!((f1.fee - 5.0).abs() < 1e-9);   // 第一笔补足到 5 元
        assert!(f2.fee.abs() < 1e-9);           // 第二笔已被覆盖
    }
}
