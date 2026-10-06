/** 个股分析前端契约辅助：配色口径、覆盖度文案、响应体校验。 */
import { describe, expect, it } from 'vitest';
import {
  isSecurityAnalysis,
  pctText,
  scoreWidth,
  signalBg,
  signalLabel,
  signalTone,
} from './security';

describe('信号配色（A 股红涨绿跌）', () => {
  it('偏多用朱砂红，偏空用青绿，中性走灰', () => {
    expect(signalTone('bullish')).toBe('text-up');
    expect(signalTone('bearish')).toBe('text-down');
    expect(signalTone('neutral')).toBe('text-ink-dim');
    expect(signalTone(null)).toBe('text-ink-dim');
    expect(signalTone(undefined)).toBe('text-ink-dim');
  });

  it('徽标底色与信号一致', () => {
    expect(signalBg('bullish')).toContain('bg-up');
    expect(signalBg('bearish')).toContain('bg-down');
  });

  it('中文标签', () => {
    expect(signalLabel('bullish')).toBe('偏多');
    expect(signalLabel('bearish')).toBe('偏空');
    expect(signalLabel('neutral')).toBe('中性');
    expect(signalLabel(null)).toBe('—');
  });
});

describe('数值展示', () => {
  it('scoreWidth 夹在 0–100', () => {
    expect(scoreWidth(0)).toBe('0%');
    expect(scoreWidth(50)).toBe('50%');
    expect(scoreWidth(120)).toBe('100%');
    expect(scoreWidth(-5)).toBe('0%');
    expect(scoreWidth(null)).toBe('0%');
  });

  it('pctText 把 0–1 渲染成百分比，缺值显示破折号', () => {
    expect(pctText(0.85)).toBe('85%');
    expect(pctText(0.855, 1)).toBe('85.5%');
    expect(pctText(null)).toBe('—');
  });
});

describe('isSecurityAnalysis 结构守卫', () => {
  const good = { symbol: '600519.SH', angles: [], score: { score: 50 } };

  it('接受合法报告', () => {
    expect(isSecurityAnalysis(good)).toBe(true);
  });

  it('拒绝上游误返回的数组（SWR mock/代理异常时会出现）', () => {
    expect(isSecurityAnalysis([])).toBe(false);
    expect(isSecurityAnalysis(null)).toBe(false);
    expect(isSecurityAnalysis(undefined)).toBe(false);
    expect(isSecurityAnalysis('x')).toBe(false);
  });

  it('缺少关键字段时拒绝', () => {
    expect(isSecurityAnalysis({ symbol: '600519.SH' })).toBe(false);
    expect(isSecurityAnalysis({ angles: [], score: {} })).toBe(false);
    expect(isSecurityAnalysis({ symbol: 'x', angles: {}, score: {} })).toBe(false);
  });
});
