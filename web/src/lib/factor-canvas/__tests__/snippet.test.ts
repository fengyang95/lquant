import { describe, expect, it } from 'vitest';

import { EMPTY_CATALOG, findOp } from '../catalog';
import { infixSnippet, opSnippet, snippetGroups } from '../snippet';
import { FIXTURE_CATALOG } from './fixtures';

describe('算子片段（因子注册表单的算子面板）', () => {
  it('空目录不产出任何按钮', () => {
    expect(snippetGroups(EMPTY_CATALOG)).toEqual([]);
  });

  it('算子片段是完整合法调用：元数与参数都补齐', () => {
    const snippet = (name: string) => {
      const op = findOp(FIXTURE_CATALOG, name);
      if (!op) throw new Error(`夹具缺算子 ${name}`);
      return opSnippet(op);
    };
    expect(snippet('Ts_Mean')).toBe('Ts_Mean($close,5)');
    expect(snippet('Ts_Corr')).toBe('Ts_Corr($close,$close,5)');
    expect(snippet('Ts_Quantile')).toBe('Ts_Quantile($close,5,0.8)');
    expect(snippet('Rank')).toBe('Rank($close)');
    expect(snippet('If')).toBe('If($close,$close,$close)');
  });

  it('中缀片段按元数出括号表达式', () => {
    expect(infixSnippet({ token: '-', label: '减法', arity: 2 })).toBe('($close - $open)');
    expect(infixSnippet({ token: '-', label: '取相反数', arity: 1 })).toBe('-($close)');
    expect(infixSnippet({ token: '>', label: '大于', arity: 2 })).toBe('($close > $open)');
  });

  it('分组顺序固定，且旧版硬编码的坏算子名绝迹', () => {
    const groups = snippetGroups(FIXTURE_CATALOG);
    expect(groups.map((g) => g.label)).toEqual([
      '数据字段',
      '四则与比较',
      '时序算子',
      '截面算子',
      '逐元素算子',
    ]);

    const inserts = groups.flatMap((g) => g.items.map((item) => item.insert));
    // 旧版面板写死了 Mean(/Std(/Corr(/Ref(/Delta(/Ratio(，
    // 其中 Delta/Ratio/Ref 引擎根本不认 —— 这些名字不许再出现
    for (const stale of ['Mean(', 'Std(', 'Corr(', 'Ref(', 'Delta(', 'Ratio(']) {
      expect(inserts.some((s) => s.startsWith(stale))).toBe(false);
    }
  });

  it('字段片段用 $ 前缀，与服务端字段清单一致', () => {
    const fields = snippetGroups(FIXTURE_CATALOG)[0].items.map((item) => item.insert);
    expect(fields).toEqual(['$open', '$close', '$volume']);
  });
});
