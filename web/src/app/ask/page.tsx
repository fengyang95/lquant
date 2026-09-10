'use client';

import { Suspense, useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import PageHeader from '@/components/PageHeader';
import { Empty, ErrorNote, Loading } from '@/components/States';
import type { AskSession } from '@/lib/ask-api';
import { createSession, deleteSession, listSessions } from '@/lib/ask-api';
import ChatWindow from '@/components/ask/ChatWindow';
import ContextChip from '@/components/ask/ContextChip';
import SessionList from '@/components/ask/SessionList';

function AskWorkspace() {
  const params = useSearchParams();
  const symbolParam = params.get('symbol') ?? '';
  const [sessions, setSessions] = useState<AskSession[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [initState, setInitState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [initError, setInitError] = useState('');

  const current = sessions.find((s) => s.id === currentId) ?? null;

  // 初始化：listSessions；URL 带 ?symbol= 时直接 createSession({symbol})
  useEffect(() => {
    let alive = true;
    setInitState('loading');
    (async () => {
      try {
        if (symbolParam) {
          const created = await createSession({ symbol: symbolParam });
          if (!alive) return;
          setSessions((prev) => [created, ...prev]);
          setCurrentId(created.id);
          setInitState('ready');
        } else {
          const list = await listSessions();
          if (!alive) return;
          setSessions(list);
          setCurrentId(list[0]?.id ?? null);
          setInitState('ready');
        }
      } catch (e) {
        if (!alive) return;
        setInitError(e instanceof Error ? e.message : String(e));
        setInitState('error');
      }
    })();
    return () => {
      alive = false;
    };
  }, [symbolParam]);

  const handleNew = useCallback(async () => {
    try {
      const created = await createSession();
      setSessions((prev) => [created, ...prev]);
      setCurrentId(created.id);
    } catch (e) {
      setInitError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const handleDelete = useCallback(
    async (id: string) => {
      try {
        await deleteSession(id);
      } catch (e) {
        setInitError(e instanceof Error ? e.message : String(e));
        return;
      }
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (currentId === id) {
        setCurrentId((prev) => null);
      }
    },
    [currentId],
  );

  const handleSelect = useCallback((id: string) => {
    setCurrentId(id);
  }, []);

  if (initState === 'loading') return <Loading />;
  if (initState === 'error') return <ErrorNote>加载失败：{initError}</ErrorNote>;

  const symbol = current?.context?.symbol ? String(current.context.symbol) : '';

  return (
    <div className="space-y-5">
      <PageHeader
        title="问 AI"
        sub={symbol ? `上下文 ${symbol}` : 'Agent 对话分析'}
        actions={symbol ? <ContextChip symbol={symbol} /> : null}
      />
      {sessions.length === 0 ? (
        <Empty>还没有会话 —— 点击左上角「新建会话」开始提问</Empty>
      ) : (
        <div className="flex h-[calc(100vh-220px)] min-h-[480px]">
          <aside className="w-56 shrink-0 border border-line bg-panel">
            <SessionList
              sessions={sessions}
              currentId={currentId}
              onSelect={handleSelect}
              onNew={() => void handleNew()}
              onDelete={(id) => void handleDelete(id)}
            />
          </aside>
          <div className="min-w-0 flex-1 border border-l-0 border-line bg-white">
            {current ? (
              <ChatWindow session={current} key={current.id} />
            ) : (
              <Loading />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function AskPage() {
  // useSearchParams 需要 Suspense 边界
  return (
    <Suspense fallback={<Loading />}>
      <AskWorkspace />
    </Suspense>
  );
}
