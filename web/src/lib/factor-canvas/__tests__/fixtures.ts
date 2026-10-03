/**
 * 测试用目录夹具：形状与服务端 `GET /factors/ops` 一致。
 *
 * 刻意只放少量算子 —— 测试关心的是"目录驱动"这条机制，
 * 真实算子清单由后端测试（tests/unit/test_api_factor_ops.py）保证。
 */
import type { Catalog } from '../types';

export const FIXTURE_CATALOG: Catalog = {
  ops: [
    {
      name: 'Ts_Mean',
      category: 'TS',
      label: '时序均值',
      min_window: 1,
      series_arity: 1,
      params: [{ name: 'n', type: 'window', required: true, default: null }],
    },
    {
      name: 'Ts_Corr',
      category: 'TS',
      label: '时序相关',
      min_window: 2,
      series_arity: 2,
      params: [{ name: 'n', type: 'window', required: true, default: null }],
    },
    {
      name: 'Ts_Quantile',
      category: 'TS',
      label: '时序分位数',
      min_window: 1,
      series_arity: 1,
      params: [
        { name: 'n', type: 'window', required: true, default: null },
        { name: 'q', type: 'number', required: false, default: 0.8 },
      ],
    },
    {
      name: 'Rank',
      category: 'CS',
      label: '截面排名',
      min_window: 0,
      series_arity: 1,
      params: [],
    },
    {
      name: 'Greater',
      category: 'EL',
      label: '逐元素取大',
      min_window: 0,
      series_arity: 2,
      params: [],
    },
    {
      name: 'If',
      category: 'EL',
      label: '逐元素条件',
      min_window: 0,
      series_arity: 3,
      params: [],
    },
    {
      // 服务端拿不到签名时的哨兵值：画布必须拒绝，不能当成零参算子
      name: 'OpaqueOp',
      category: 'EL',
      label: '不可自省算子',
      min_window: 0,
      series_arity: -1,
      params: [],
    },
  ],
  infix: [
    { token: '+', label: '加法', arity: 2 },
    { token: '-', label: '减法', arity: 2 },
    { token: '*', label: '乘法', arity: 2 },
    { token: '/', label: '除法', arity: 2 },
    { token: '>', label: '大于（输出 0/1）', arity: 2 },
    { token: '-', label: '取相反数', arity: 1 },
  ],
  fields: [
    { name: 'open', label: '开盘价' },
    { name: 'close', label: '收盘价' },
    { name: 'volume', label: '成交量' },
  ],
};
