'use client';

import { useEffect, useState } from 'react';

import type { AgentCapabilities, AgentProvider } from '@/lib/agent-api';
import { getCapabilities } from '@/lib/agent-api';
import type { AskSession } from '@/lib/ask-api';
import { forkSession } from '@/lib/ask-api';

function msgOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * 换后端另开会话。
 *
 * 为什么必须是「另开」而不是在当前会话里改 provider：CLI 侧会话 id（claude 的
 * session_id / codex 的 thread_id）在库里共用一列，中途换 provider 就是拿着
 * 别人的 id 去续接 —— 上下文直接串。所以这里复制会话（能力集 + 一段简报），
 * 老会话一动不动地留着。
 *
 * 当前 provider 不列出来：点了也不会发生任何事，而后端会回 400「目标 provider
 * 与会话相同」—— 与其让用户撞一次错误，不如根本不给这个选项。
 */
export default function ForkDialog({
  session,
  currentProvider,
  onCancel,
  onForked,
}: {
  session: AskSession;
  currentProvider: string;
  onCancel: () => void;
  onForked: (created: AskSession) => void;
}) {
  const [caps, setCaps] = useState<AgentCapabilities | null>(null);
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    let alive = true;
    getCapabilities()
      .then((c) => {
        if (alive) setCaps(c);
      })
      .catch((e) => {
        if (alive) setErr(`能力清单加载失败：${msgOf(e)}`);
      });
    return () => {
      alive = false;
    };
  }, []);

  const options: AgentProvider[] = (caps?.providers ?? []).filter(
    (p) => p.id !== currentProvider,
  );

  const submit = async () => {
    if (!target || busy) return;
    setBusy(true);
    setErr('');
    try {
      onForked(await forkSession(session.id, target));
    } catch (e) {
      setErr(msgOf(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/20 p-4">
      <div
        role="dialog"
        aria-label="换后端另开会话"
        className="flex max-h-[85vh] w-[460px] max-w-full flex-col border border-line-strong bg-paper"
      >
        <header className="border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-ink">换后端另开会话</h2>
          <p className="mt-0.5 text-xs text-ink-faint">
            复制这条会话的上下文与能力集，用另一个后端新开一条；原会话保留不动
          </p>
        </header>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-4">
          {!caps ? (
            <div className="py-6 text-center text-sm text-ink-faint">加载后端清单…</div>
          ) : options.length === 0 ? (
            <div className="py-6 text-center text-sm text-ink-faint">
              没有别的后端可选（本机只注册了 {currentProvider || '当前这一个'}）
            </div>
          ) : (
            options.map((p) => (
              <label
                key={p.id}
                className="flex cursor-pointer items-start gap-2 border border-line px-3 py-2 text-sm"
              >
                <input
                  type="radio"
                  name="fork-provider"
                  className="mt-1"
                  checked={target === p.id}
                  onChange={() => setTarget(p.id)}
                />
                <span>
                  <span className="text-ink">{p.label}</span>
                  {!p.available ? (
                    <span className="ml-2 text-[11px] text-up">
                      本机 PATH 上没有这个 CLI，选了会启动失败
                    </span>
                  ) : null}
                  <span className="block text-[11px] text-ink-faint">provider={p.id}</span>
                </span>
              </label>
            ))
          )}
          <p className="text-[11px] text-ink-faint">
            新会话首次提问时会把原会话最近的对话作为简报送给后端
            （超出预算的部分只保留最近的一段），所以新后端能接着聊。
          </p>
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-line px-4 py-2.5">
          <span className="min-w-0 flex-1 truncate text-xs text-up">{err}</span>
          <div className="flex shrink-0 gap-2">
            <button className="btn" onClick={onCancel} disabled={busy}>
              取消
            </button>
            <button
              className="btn btn-primary"
              onClick={() => void submit()}
              disabled={busy || !target}
            >
              {busy ? '开新会话…' : '另开会话'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
