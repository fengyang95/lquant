/** 个股分析报告渲染：多角度、缺失标注、风险提示与红涨绿跌口径。 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import SecurityAnalysisView from '../SecurityAnalysis';
import type { SecurityAnalysis } from '@/lib/security';

function report(over: Partial<SecurityAnalysis> = {}): SecurityAnalysis {
  return {
    schema_version: '1.0',
    symbol: '600519.SH',
    asof: '2026-09-30',
    overview: {
      symbol: '600519.SH', name: '贵州茅台', asof: '2026-09-30',
      sec_type: 'stock', board: null, is_st: false,
      industry: '食品饮料', industry_code: 'SW', peer_count: 122,
      benchmark: '000300.SH', metrics: [], notes: [],
    },
    score: {
      score: 62.5, grade: '偏多', stance: 'bullish',
      n_scored: 4, n_angles: 6, angle_coverage: 0.85, data_coverage: 0.9,
      weights: { technical: 0.24 }, contributions: [
        { id: 'technical', label: '技术面', score: 70, stance: 'bullish',
          weight: 0.24, effective_weight: 0.3, contribution: 6.0 },
        { id: 'valuation', label: '估值', score: 30, stance: 'bearish',
          weight: 0.2, effective_weight: 0.25, contribution: -5.0 },
      ],
    },
    verdict: { points: ['综合 63 分（偏多）'], risks: ['资金面缺失：没有资金流数据'] },
    angles: [
      {
        id: 'technical', label: '技术面', weight: 0.24, desc: '趋势',
        available: true, score: 70, stance: 'bullish', coverage: 1,
        summary: '8 项技术信号：6 多 / 1 空', metrics: [
          { key: 'rsi14', label: 'RSI(14)', value: 68.2, display: '68.2',
            unit: null, percentile: null, signal: 'bearish', note: '超买区，追高风险' },
        ], hint: null, extra: {},
      },
      {
        id: 'valuation', label: '估值', weight: 0.2, desc: 'PE/PB',
        available: true, score: 30, stance: 'bearish', coverage: 1,
        summary: '4 项估值指标中 3 项处自身历史高位',
        metrics: [], hint: null, extra: {},
      },
      {
        id: 'capital', label: '资金面', weight: 0.15, desc: '主力净流入',
        available: false, score: null, stance: null, coverage: 0,
        summary: '资金流数据为空',
        metrics: [], hint: '没有资金流数据（money_flow 表为空或未覆盖该标的）', extra: {},
      },
      {
        id: 'news', label: '消息面', weight: 0, desc: '热度',
        available: true, score: null, stance: null, coverage: 0.5,
        summary: '近 30 天 3 条关联新闻 · 仅热度，不参与评分',
        metrics: [], hint: null, extra: { scored: false },
      },
    ],
    risk: {
      available: true, scored: false, flags: ['近 60 日年化波动 65%，波动显著偏高'],
      metrics: [
        { key: 'vol60', label: '年化波动率（60 日）', value: 65, display: '65.0%',
          unit: '%', percentile: null, signal: null, note: '越高波动越大' },
      ],
      hint: null,
    },
    disclaimer: '本报告由平台按公开数据自动计算，不构成投资建议。',
    ...over,
  };
}

describe('SecurityAnalysisView', () => {
  it('渲染综合分、评级与覆盖度', () => {
    render(<SecurityAnalysisView report={report()} />);
    expect(screen.getByText('63')).toBeInTheDocument();       // score 四舍五入
    // 「偏多」同时出现在综合评级与角度徽标上 —— 断言存在而非唯一
    expect(screen.getAllByText('偏多').length).toBeGreaterThan(0);
    expect(screen.getByText('4/6')).toBeInTheDocument();
    expect(screen.getByText('85%')).toBeInTheDocument();      // 分析面覆盖
  });

  it('每个角度都渲染标签与结论', () => {
    render(<SecurityAnalysisView report={report()} />);
    // 角度名同时出现在明细卡与「贡献」列表里
    expect(screen.getAllByText('技术面').length).toBeGreaterThan(0);
    expect(screen.getAllByText('资金面').length).toBeGreaterThan(0);
    expect(screen.getAllByText('消息面').length).toBeGreaterThan(0);
    expect(screen.getByText(/8 项技术信号/)).toBeInTheDocument();
  });

  it('取不到数的角度显示 hint（而不是 0 分）', () => {
    render(<SecurityAnalysisView report={report()} />);
    expect(screen.getByText(/money_flow 表为空/)).toBeInTheDocument();
  });

  it('不评分的角度明确标注「不评分」，不误读成中性', () => {
    render(<SecurityAnalysisView report={report()} />);
    // 资金面/消息面 score=null → 头部显示「不评分」；风险面板也带「不参与评分」字样
    expect(screen.getAllByText('不评分').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/不参与评分/).length).toBeGreaterThan(0);
  });

  it('偏多角度用朱砂红（A 股红涨绿跌）', () => {
    render(<SecurityAnalysisView report={report()} />);
    const scoreEl = screen.getByText('70');
    expect(scoreEl.className).toContain('text-up');
  });

  it('偏空角度用青绿', () => {
    render(<SecurityAnalysisView report={report()} />);
    const bearish = screen.getByText('30');
    expect(bearish.className).toContain('text-down');
  });

  it('渲染风险提示与数据缺口', () => {
    render(<SecurityAnalysisView report={report()} />);
    expect(screen.getByText(/年化波动 65%/)).toBeInTheDocument();
    expect(screen.getByText(/资金面缺失/)).toBeInTheDocument();
  });

  it('渲染免责声明', () => {
    render(<SecurityAnalysisView report={report()} />);
    expect(screen.getByText(/不构成投资建议/)).toBeInTheDocument();
  });

  it('无法评分时给出说明而不是空白', () => {
    const r = report();
    r.score = { ...r.score, score: null, grade: '无法评分', stance: null,
                n_scored: 0, angle_coverage: 0, contributions: [] };
    render(<SecurityAnalysisView report={r} />);
    expect(screen.getByText('无法评分')).toBeInTheDocument();
    expect(screen.getByText(/可用数据不足以形成综合判断/)).toBeInTheDocument();
  });

  it('结构不匹配（上游返回 []）时不崩，显示空态', () => {
    render(<SecurityAnalysisView report={[]} />);
    expect(screen.getByText(/分析报告为空或结构不匹配/)).toBeInTheDocument();
  });

  it('report 为 undefined 时不崩', () => {
    render(<SecurityAnalysisView report={undefined} />);
    expect(screen.getByText(/分析报告为空或结构不匹配/)).toBeInTheDocument();
  });
});
