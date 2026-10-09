/** 行业分析报告渲染：行业概览、多角度、缺失标注与红涨绿跌口径。 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import IndustryAnalysisView from '../IndustryAnalysis';
import type { IndustryAnalysis } from '@/lib/industry';

function report(over: Partial<IndustryAnalysis> = {}): IndustryAnalysis {
  return {
    schema_version: '1.0',
    industry: '银行',
    industry_code: '801780.SI',
    std: 'SW',
    asof: '2026-09-30',
    overview: {
      industry: '银行', industry_code: '801780.SI', std: 'SW',
      asof: '2026-09-30', member_count: 42, active_members: 42,
      day_ret: 0.82,
      rank: { rank: 2, n_industries: 31, percentile: 93.55, r20: 8.5 },
      leaders: [
        { symbol: '600036.SH', name: '招商银行', ret20: 12.5, close: 40.0 },
      ],
      benchmark: '000300.SH', notes: [], n_angles: 5,
    },
    score: {
      score: 63.5, grade: '强势', stance: 'bullish',
      n_scored: 4, n_angles: 5, angle_coverage: 0.8, data_coverage: 0.9,
      weights: { trend: 0.28 },
      contributions: [
        { id: 'trend', label: '趋势与轮动', score: 75, stance: 'bullish',
          weight: 0.28, effective_weight: 0.35, contribution: 8.0 },
        { id: 'valuation', label: '估值', score: 30, stance: 'bearish',
          weight: 0.18, effective_weight: 0.22, contribution: -4.0 },
      ],
    },
    verdict: { points: ['综合 64 分（强势）'], risks: ['景气度缺失：没有财务数据'] },
    angles: [
      {
        id: 'trend', label: '趋势与轮动', weight: 0.28, desc: '动量',
        available: true, score: 75, stance: 'bullish', coverage: 1,
        summary: '20 日 +5.0% · RRG 领先', metrics: [
          { key: 'r20', label: '20 日收益', value: 5.0, display: '+5.00%',
            unit: '%', percentile: 90, signal: 'bullish', note: '全行业第 2' },
        ], hint: null, extra: { rrg: { quadrant_label: '领先' } },
      },
      {
        id: 'prosperity', label: '景气度', weight: 0.24, desc: '财务',
        available: false, score: null, stance: null, coverage: 0,
        summary: '缺少财务数据，无法判断景气度', metrics: [],
        hint: '没有 PIT 财务数据：先执行 `lq data financial`', extra: {},
      },
    ],
    risk: {
      available: true, title: '行业风险提示',
      metrics: [
        { key: 'member_count', label: '成分股数', value: 42, display: '42',
          unit: '家', percentile: null, signal: null, note: null },
      ],
      flags: ['行业 PE 处于自身历史 85% 分位（偏贵）'], hint: null,
    },
    disclaimer: '行业指数为成分股等权合成，不构成投资建议。',
    ...over,
  };
}

describe('IndustryAnalysisView', () => {
  it('渲染综合评分与行业口径的分级词', () => {
    render(<IndustryAnalysisView report={report()} />);
    expect(screen.getByText('64')).toBeInTheDocument();
    expect(screen.getByText('强势')).toBeInTheDocument();
    expect(screen.getByText('4/5')).toBeInTheDocument();
    expect(screen.getByText('80%')).toBeInTheDocument();
  });

  it('渲染行业概览：成分股、全行业排名与龙头股', () => {
    render(<IndustryAnalysisView report={report()} />);
    expect(screen.getByText('行业概览')).toBeInTheDocument();
    expect(screen.getByText('2/31')).toBeInTheDocument();
    expect(screen.getByText('94% 分位')).toBeInTheDocument();
    expect(screen.getByText('招商银行')).toBeInTheDocument();
    expect(screen.getByText('+12.50%')).toBeInTheDocument();
  });

  it('明确标注合成口径，避免被当成官方行业指数', () => {
    render(<IndustryAnalysisView report={report()} />);
    // 概览面板与免责声明都会写出口径，多处出现是刻意的
    expect(screen.getAllByText(/等权合成/).length).toBeGreaterThan(0);
    expect(screen.getByText(/不是交易所或申万官方指数/)).toBeInTheDocument();
  });

  it('缺数据的角度显示原因与补齐方式，且标为不评分', () => {
    render(<IndustryAnalysisView report={report()} />);
    expect(screen.getByText('不评分')).toBeInTheDocument();
    expect(screen.getByText(/先执行 `lq data financial`/)).toBeInTheDocument();
  });

  it('渲染风险提示与数据缺口', () => {
    render(<IndustryAnalysisView report={report()} />);
    expect(screen.getByText(/偏贵/)).toBeInTheDocument();
    expect(screen.getByText(/景气度缺失/)).toBeInTheDocument();
  });

  it('结构不匹配时给空态而不是崩掉', () => {
    render(<IndustryAnalysisView report={{}} />);
    expect(screen.getByText(/报告为空或结构不匹配/)).toBeInTheDocument();
  });

  it('无可用角度时不显示假分数', () => {
    const r = report();
    r.score = {
      score: null, grade: '无法评分', stance: null,
      n_scored: 0, n_angles: 5, angle_coverage: 0, data_coverage: 0,
      weights: {}, contributions: [],
    };
    r.angles = [];
    render(<IndustryAnalysisView report={r} />);
    expect(screen.getByText('无法评分')).toBeInTheDocument();
  });
});
