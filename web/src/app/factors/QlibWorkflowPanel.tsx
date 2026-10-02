'use client';

/** Qlib 工作流面板：配置选择/编辑保存 + 运行 + 历史运行列表。 */
import { useCallback, useEffect, useState } from 'react';
import { Panel } from '@/components/Panel';
import {
  getQlibConfig,
  listQlibConfigs,
  listQlibRuns,
  postQlibWorkflow,
  putQlibConfig,
  type QlibRun,
} from '@/lib/qlib';

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中', running: '运行中', finished: '已完成',
  failed: '失败', canceled: '已取消',
};

export default function QlibWorkflowPanel() {
  const [configs, setConfigs] = useState<string[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [expName, setExpName] = useState('lquant_qlib');
  const [market, setMarket] = useState('');
  const [runs, setRuns] = useState<QlibRun[]>([]);
  const [msg, setMsg] = useState('');
  // 加载失败必须显式可见：静默 catch 会让「取不到配置」看起来像「没有配置」
  const [loadError, setLoadError] = useState('');

  const refresh = useCallback(() => {
    setLoadError('');
    listQlibConfigs()
      .then((cs) => {
        setConfigs(cs.map((c) => c.name));
        setSelected((s) => s || cs[0]?.name || '');
      })
      .catch((e) => setLoadError(`配置列表加载失败：${e instanceof Error ? e.message : e}`));
    listQlibRuns()
      .then(setRuns)
      .catch((e) => setLoadError(`历史运行列表加载失败：${e instanceof Error ? e.message : e}`));
  }, []);
  useEffect(refresh, [refresh]);

  useEffect(() => {
    if (!selected) return;
    let alive = true;
    setLoadError('');
    getQlibConfig(selected)
      .then((c) => {
        if (alive) setContent(c.content);
      })
      .catch((e) => {
        if (alive) setLoadError(`配置内容加载失败：${e instanceof Error ? e.message : e}`);
      });
    return () => {
      alive = false;
    };
  }, [selected]);

  async function save() {
    setMsg('');
    try {
      await putQlibConfig(selected, content);
      setMsg('✓ 配置已保存');
    } catch (e) {
      setMsg(`✗ 保存失败：${e instanceof Error ? e.message : e}`);
    }
  }

  async function run() {
    setMsg('');
    try {
      const r = await postQlibWorkflow({
        config: selected,
        exp_name: expName,
        market: market || null,
      });
      setMsg(`✓ 已提交运行 ${r.run_id}，进度见任务中心`);
      setTimeout(refresh, 500);
    } catch (e) {
      setMsg(`✗ 提交失败：${e instanceof Error ? e.message : e}`);
    }
  }

  return (
    <Panel title="Qlib 工作流" meta="训练 → IC 分析 → 组合回测">
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-ink-faint">
            配置
            <select className="input input-mono" value={selected}
              onChange={(e) => setSelected(e.target.value)}>
              {configs.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="text-xs text-ink-faint">
            股票池
            <input className="input input-mono" value={market}
              placeholder="all / top300"
              onChange={(e) => setMarket(e.target.value)} />
          </label>
          <label className="text-xs text-ink-faint">
            实验名
            <input className="input input-mono" value={expName}
              onChange={(e) => setExpName(e.target.value)} />
          </label>
          <button className="btn" onClick={save}>保存配置</button>
          <button className="btn btn-primary" onClick={run}
            disabled={!selected}>运行</button>
        </div>
        <textarea className="input input-mono w-full font-mono text-xs" rows={16}
          value={content} onChange={(e) => setContent(e.target.value)}
          spellCheck={false} />
        {msg && <p className="text-xs text-ink-faint">{msg}</p>}
        {loadError && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            ⚠ {loadError}
            <button onClick={refresh} className="ml-2 text-indigo hover:underline">重试</button>
          </div>
        )}
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-ink-faint">
              <th className="py-1">ID</th><th>配置</th><th>状态</th>
              <th>IC</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} className="border-t border-line">
                <td className="py-1 font-mono text-xs">{r.id}</td>
                <td>{r.config}</td>
                <td>{STATUS_TEXT[r.status] ?? r.status}</td>
                <td className="font-mono text-xs">
                  {r.metrics?.IC != null ? r.metrics.IC.toFixed(4) : '—'}
                </td>
                <td className="font-mono text-xs">{r.created_at.slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
