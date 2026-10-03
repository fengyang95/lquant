import { describe, expect, it } from 'vitest';

import type { AgentEventMsg } from './ask-api';
import { applyTraceEvent, newRunTrace, summarizeArgs } from './ask-stream';

/** 依次喂事件，返回最终过程轨 */
function run(events: AgentEventMsg[], step = 100) {
  let t = newRunTrace(0);
  events.forEach((ev, i) => {
    t = applyTraceEvent(t, ev, (i + 1) * step);
  });
  return t;
}

describe('applyTraceEvent', () => {
  it('连续 thinking 合成一段推理，遇到工具调用即收尾', () => {
    const t = run([
      { type: 'thinking', text: '先看看' },
      { type: 'thinking', text: '行情数据' },
      { type: 'tool_call', name: 'get_quotes', args: { symbol: '600519' } },
    ]);
    expect(t.steps).toHaveLength(2);
    const think = t.steps[0];
    expect(think.kind).toBe('thinking');
    if (think.kind !== 'thinking') throw new Error('unreachable');
    expect(think.text).toBe('先看看行情数据');
    expect(think.finishedAt).toBe(300); // 工具调用到来的那一刻收尾
  });

  it('工具结果按名字配回对应的 running 步骤，带上摘要与全文', () => {
    const t = run([
      { type: 'tool_call', name: 'get_quotes', args: { symbol: '600519' } },
      { type: 'tool_result', name: 'get_quotes', summary: '价格 1500', text: '价格 1500 元' },
    ]);
    expect(t.steps).toHaveLength(1);
    const s = t.steps[0];
    expect(s.kind).toBe('tool');
    if (s.kind !== 'tool') throw new Error('unreachable');
    expect(s.status).toBe('done');
    expect(s.summary).toBe('价格 1500');
    expect(s.detail).toBe('价格 1500 元');
    expect(s.finishedAt).toBe(200);
  });

  it('同名并发工具各自配对，不互相顶掉', () => {
    const t = run([
      { type: 'tool_call', name: 'get_quotes' },
      { type: 'tool_call', name: 'get_quotes' },
      { type: 'tool_result', name: 'get_quotes', summary: '第二次' },
    ]);
    // 结果配给**最靠后**的那条同名 running
    expect(t.steps.map((s) => (s.kind === 'tool' ? s.status : ''))).toEqual(['running', 'done']);
  });

  it('结果先到时补一条完整步骤，不静默丢数据', () => {
    const t = run([{ type: 'tool_result', name: 'get_daily', summary: '日线' }]);
    expect(t.steps).toHaveLength(1);
    const s = t.steps[0];
    if (s.kind !== 'tool') throw new Error('unreachable');
    expect(s.status).toBe('done');
    expect(s.name).toBe('get_daily');
  });

  it('done 把还挂着的工具定成成功并落收尾时间', () => {
    const t = run([
      { type: 'tool_call', name: 'get_quotes' },
      { type: 'done' },
    ]);
    expect(t.failed).toBe(false);
    expect(t.finishedAt).toBe(200);
    const s = t.steps[0];
    if (s.kind !== 'tool') throw new Error('unreachable');
    expect(s.status).toBe('done');
  });

  it('error（含用户中断）把挂着的工作标成失败', () => {
    const t = run([
      { type: 'tool_call', name: 'get_quotes' },
      { type: 'error', message: '已中断' },
    ]);
    expect(t.failed).toBe(true);
    const s = t.steps[0];
    if (s.kind !== 'tool') throw new Error('unreachable');
    expect(s.status).toBe('error');
  });

  it('system：init 上屏成一条运行环境，hook 噪声丢弃', () => {
    const t = run([
      { type: 'system', data: { subtype: 'hook_started', hook_name: 'SessionStart' } },
      { type: 'system', data: { subtype: 'init', model: 'm1', tools: ['a', 'b'], mcp_servers: [{}] } },
    ]);
    expect(t.steps).toHaveLength(1);
    const s = t.steps[0];
    if (s.kind !== 'note') throw new Error('unreachable');
    expect(s.level).toBe('info');
    expect(s.text).toContain('m1');
    expect(s.text).toContain('2 个内置工具');
  });

  it('system：level=error 的告警单独上屏（codex 的非致命错误）', () => {
    const t = run([{ type: 'system', data: { level: 'error', message: '模型元数据缺失' } }]);
    const s = t.steps[0];
    if (s.kind !== 'note') throw new Error('unreachable');
    expect(s.level).toBe('warn');
    expect(s.text).toContain('模型元数据缺失');
  });

  it('assistant_delta 不动过程轨（正文归消息列表）', () => {
    const t = run([{ type: 'assistant_delta', text: '你好' }]);
    expect(t.steps).toHaveLength(0);
  });
});

describe('summarizeArgs', () => {
  it('键值一行预览；长字符串截断', () => {
    expect(summarizeArgs({ symbol: '600519', n: 5 })).toBe('symbol=600519, n=5');
    const long = summarizeArgs({ q: 'x'.repeat(200) });
    expect(long.length).toBeLessThanOrEqual(141);
    expect(long.endsWith('…')).toBe(true);
  });

  it('无入参 / undefined 返回空串', () => {
    expect(summarizeArgs(undefined)).toBe('');
    expect(summarizeArgs({})).toBe('');
  });
});
