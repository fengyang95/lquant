// 指标选择器 —— 数据源 /data/indicators/registry，不硬编码指标清单。
// 按 category 分组渲染勾选框；至少保留一个，避免请求 names= 为空被后端 422。
'use client';

import { useMemo } from 'react';
import useSWR from 'swr';
import { ErrorNote } from '@/components/States';
import { fetcher } from '@/lib/api';

export type IndicatorMeta = {
  name: string;
  label: string;
  category: string;
  /** 挂载面板：price 与价格同轴可叠 K 线；sub/volume 各自独立子图。
   *  量纲信息由后端显式声明，前端不按 category 猜。 */
  pane: 'price' | 'sub' | 'volume';
  min_window: number;
  inputs: string[];
  outputs: string[];
};

export type IndicatorRegistry = {
  indicators: IndicatorMeta[];
  default: string[];
};

const CATEGORY_LABELS: Record<string, string> = {
  trend: '趋势',
  oscillator: '摆动',
  volume: '量能',
  channel: '通道',
  pattern: '形态',
};

export function useIndicatorRegistry() {
  return useSWR<IndicatorRegistry>('/data/indicators/registry', fetcher);
}

/** 与价格同轴的挂载面板 —— 只有这些指标的输出列可以叠加到 K 线上。 */
export const PRICE_PANE = 'price';

/** 判断某个输出列是否适合画成 K 线叠加线（布尔信号不画线）。 */
export function isLineOutput(rows: Record<string, unknown>[] | undefined,
                             key: string): boolean {
  if (!rows?.length) return false;
  // 只看尾部若干行：布尔列（如 td_gold_buy）从头到尾都是 bool
  const tail = rows.slice(-30);
  return tail.some((r) => typeof r[key] === 'number');
}

export default function IndicatorPicker({
  meta,
  value,
  onChange,
}: {
  meta: IndicatorMeta[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const grouped = useMemo(() => {
    const out: Record<string, IndicatorMeta[]> = {};
    for (const m of meta) {
      (out[m.category] ??= []).push(m);
    }
    return out;
  }, [meta]);

  function toggle(name: string) {
    if (value.includes(name)) {
      // 不允许清空：names= 为空后端会 422，前端先兜住
      if (value.length === 1) return;
      onChange(value.filter((n) => n !== name));
    } else {
      onChange([...value, name]);
    }
  }

  return (
    <div className="space-y-2" data-testid="indicator-picker">
      {Object.entries(grouped).map(([cat, items]) => (
        <div key={cat} className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="w-8 shrink-0 text-xs text-ink-faint">
            {CATEGORY_LABELS[cat] ?? cat}
          </span>
          {items.map((m) => (
            <label key={m.name} className="flex cursor-pointer items-center gap-1 text-xs"
                   title={`预热 ${m.min_window} 根 · 输出 ${m.outputs.join(', ')}`}>
              <input
                type="checkbox"
                checked={value.includes(m.name)}
                onChange={() => toggle(m.name)}
                aria-label={m.label}
              />
              <span className={value.includes(m.name) ? 'text-ink' : 'text-ink-dim'}>
                {m.label}
              </span>
            </label>
          ))}
        </div>
      ))}
    </div>
  );
}

/** 注册表加载失败时的降级提示（不阻塞页面其余部分）。 */
export function IndicatorPickerFallback({ error }: { error: unknown }) {
  if (!error) return null;
  return <ErrorNote>指标注册表加载失败：{String(error)}</ErrorNote>;
}
