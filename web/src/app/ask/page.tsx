'use client';

import { Suspense, useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import PageHeader from '@/components/PageHeader';
import { Empty, ErrorNote, Loading } from '@/components/States';
import type { AskSession } from '@/lib/ask-api';
import { createSession, deleteSession, listSessions } from '@/lib/ask-api';
import type { AgentCapabilities, AgentConfig } from '@/lib/agent-api';
import { getCapabilities } from '@/lib/agent-api';
import ChatWindow from '@/components/ask/ChatWindow';
import ContextChip from '@/components/ask/ContextChip';
import NewSessionDialog from '@/components/ask/NewSessionDialog';
import SessionList from '@/components/ask/SessionList';

function AskWorkspace() {
  const params = useSearchParams();
  const symbolParam = params.get('symbol') ?? '';
  const [sessions, setSessions] = useState<AskSession[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [initState, setInitState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [initError, setInitError] = useState('');
  // 新建会话弹层：能力清单拉不到时降级为「直接建会话」（走全局默认），
  // 不让一个可选的配置入口把「新建会话」这条主路径也堵死。
  const [caps, setCaps] = useState<AgentCapabilities | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

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
    setInitError('');
    setCreateError(null);
    // 先拉能力清单再开弹层；拉不到就直接建（走全局默认），不挡住主路径
    try {
      const list = caps ?? (await getCapabilities());
      setCaps(list);
      setDialogOpen(true);
    } catch {
      await createSession().then((created) => {
        setSessions((prev) => [created, ...prev]);
        setCurrentId(created.id);
      }).catch((e) => {
        setInitError(e instanceof Error ? e.message : String(e));
      });
    }
  }, [caps]);

  const handleCreate = useCallback(async (cfg: AgentConfig) => {
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createSession({}, cfg);
      setSessions((prev) => [created, ...prev]);
      setCurrentId(created.id);
      setDialogOpen(false);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  }, []);

  const handleDelete = useCallback(
    async (id: string) => {
      setInitError('');
      try {
        await deleteSession(id);
      } catch (e) {
        setInitError(e instanceof Error ? e.message : String(e));
        return;
      }
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (currentId === id) setCurrentId(null);
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
      {/* ready 态的错误反馈：新建/删除失败不能静默 */}
      {initError ? <ErrorNote>操作失败：{initError}</ErrorNote> : null}
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
            <Empty>还没有会话 —— 点击左上角「新建会话」开始提问</Empty>
          )}
        </div>
      </div>
      {dialogOpen && caps ? (
        <NewSessionDialog
          caps={caps}
          busy={creating}
          error={createError}
          onCancel={() => setDialogOpen(false)}
          onCreate={(cfg) => void handleCreate(cfg)}
        />
      ) : null}
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
