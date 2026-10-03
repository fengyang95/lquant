/** 「问 AI」的过程轨（run trace）：把 thinking / tool_call / tool_result / system
 *  这些**过程事件**归约成一条可视化的步骤序列。
 *
 *  为什么单独一层而不是塞进消息列表：过程数据的生命周期是**这一轮运行**，
 *  而消息列表是**落库事实源**（done 时要拉库对账替换）。两者混在一起，
 *  对账那一下会把过程抹掉（或者反过来，过程残留成第二条回答）。
 *  所以：正文走 reduceMessages，过程走 applyTraceEvent，各归各的。
 *
 *  纯函数、无 React 依赖，能直接被 vitest 单测。
 */
import type { AgentEventMsg } from './ask-api';

export type ToolStep = {
  kind: 'tool';
  id: string;
  name: string;
  args?: Record<string, unknown>;
  status: 'running' | 'done' | 'error';
  /** 工具结果的截断摘要（后端已截到 200 字） */
  summary?: string;
  /** 完整结果文本，折叠展示 */
  detail?: string;
  startedAt: number;
  finishedAt?: number;
};

export type ThinkingStep = {
  kind: 'thinking';
  id: string;
  text: string;
  startedAt: number;
  finishedAt?: number;
};

/** 运行时信息（模型 / 工具数 / 非致命告警）—— 不是回答的一部分 */
export type NoteStep = {
  kind: 'note';
  id: string;
  text: string;
  level: 'info' | 'warn';
  startedAt: number;
};

export type TraceStep = ToolStep | ThinkingStep | NoteStep;

export type RunTrace = {
  startedAt: number;
  /** 收尾时间（done/error 时落），running 中为 undefined */
  finishedAt?: number;
  /** 是否以失败/中断收尾 */
  failed?: boolean;
  steps: TraceStep[];
};

/** codex 侧未一阶映射的 item 类型 → 中文标签（见 codex_json.py 的降级口径） */
const CODEX_ITEM_LABELS: Record<string, string> = {
  file_change: '文件改动',
  todo_list: '待办清单',
  web_search: '联网检索',
  reasoning: '推理',
};

/** 工具结果留在内存里的上限；超出部分只保留摘要（summary 本身已由后端截到 200 字） */
const _DETAIL_MAX = 4000;

export function newRunTrace(now: number): RunTrace {
  return { startedAt: now, steps: [] };
}

/** 事件 → 过程轨。未知事件类型原样返回（前向兼容，不抛错）。 */
export function applyTraceEvent(trace: RunTrace, ev: AgentEventMsg, now: number): RunTrace {
  switch (ev.type) {
    case 'thinking':
      return appendThinking(trace, ev.text ?? '', now);
    case 'tool_call':
      // 开始调工具 = 这段推理结束（否则「推理中」会一直挂着，耗时也算不准）
      return pushStep(closeOpenThinking(trace, now), {
        kind: 'tool',
        id: nextId(trace),
        name: ev.name || '工具',
        args: ev.args,
        status: 'running',
        startedAt: now,
      });
    case 'tool_result':
      return finishTool(trace, ev, now);
    case 'system':
      return appendNote(trace, ev.data ?? {}, now);
    case 'done':
    case 'error':
      return finishRun(trace, now, ev.type === 'error');
    default:
      // assistant_delta 走消息流，不该在这里改过程轨
      return trace;
  }
}

/** 收尾：把还挂着的 running 工具步骤定态（done→成功，error→失败），
 *  否则界面上会永远留一个转圈的「正在调用」。 */
function finishRun(trace: RunTrace, now: number, failed: boolean): RunTrace {
  const steps = trace.steps.map((s) =>
    s.kind === 'tool' && s.status === 'running'
      ? { ...s, status: failed ? ('error' as const) : ('done' as const), finishedAt: now }
      : s.kind === 'thinking' && s.finishedAt === undefined
        ? { ...s, finishedAt: now }
        : s,
  );
  return { ...trace, steps, finishedAt: now, failed };
}

function appendThinking(trace: RunTrace, text: string, now: number): RunTrace {
  const last = trace.steps[trace.steps.length - 1];
  // 连续的 thinking 事件是同一段推理的分片，累加而不是每条一个步骤
  if (last && last.kind === 'thinking' && last.finishedAt === undefined) {
    const merged: ThinkingStep = { ...last, text: last.text + text };
    return { ...trace, steps: [...trace.steps.slice(0, -1), merged] };
  }
  const step: ThinkingStep = {
    kind: 'thinking',
    id: nextId(trace),
    text,
    startedAt: now,
  };
  return { ...trace, steps: [...trace.steps, step] };
}

function finishTool(trace: RunTrace, ev: AgentEventMsg, now: number): RunTrace {
  // 优先配同名的那条：并发工具（claude 可以一轮里发多个 tool_use）名字对得上
  // 才不会被后到的结果配错。
  let idx = -1;
  for (let i = trace.steps.length - 1; i >= 0; i--) {
    const s = trace.steps[i];
    if (s.kind !== 'tool' || s.status !== 'running') continue;
    if (!ev.name || s.name === ev.name) {
      idx = i;
      break;
    }
    if (idx < 0) idx = i; // 记下最靠后的 running，找不到同名就用它兜底
  }
  const summary = ev.summary ?? '';
  // 工具结果全文（后端只截 summary，text 原样过 WS）：真跑一次行情查询就可能
  // 是几十 KB，全留在 state + DOM 里会让长会话越用越卡。上屏留头部即可。
  const raw = ev.text ?? '';
  const detail = raw.length > _DETAIL_MAX
    ? `${raw.slice(0, _DETAIL_MAX)}\n…（已截断，完整结果见服务端日志）`
    : raw;
  if (idx < 0) {
    // 结果先到（某些 CLI 的 result 帧不带名字且没有对应 call）：补一条完整步骤
    return pushStep(trace, {
      kind: 'tool',
      id: nextId(trace),
      name: ev.name || '工具',
      status: 'done',
      summary,
      detail,
      startedAt: now,
      finishedAt: now,
    });
  }
  return {
    ...trace,
    steps: trace.steps.map((s, i) =>
      i === idx && s.kind === 'tool'
        ? { ...s, status: 'done' as const, summary, detail, finishedAt: now }
        : s,
    ),
  };
}

function appendNote(trace: RunTrace, data: Record<string, unknown>, now: number): RunTrace {
  const text = noteText(data);
  if (!text) return trace;
  const step: NoteStep = {
    kind: 'note',
    id: nextId(trace),
    text,
    level: data.level === 'error' ? 'warn' : 'info',
    startedAt: now,
  };
  return { ...trace, steps: [...trace.steps, step] };
}

/** 哪些 system 帧值得上屏。**hook 噪声必须挡掉**：配了 hooks 的 claude 每轮
 *  会发十几条 `hook_started` / `hook_response`，全渲染出来过程轨就废了。 */
function noteText(data: Record<string, unknown>): string | null {
  if (data.level === 'error') {
    return `运行时告警：${String(data.message ?? '')}`.trim();
  }
  const subtype = String(data.subtype ?? '');
  if (subtype === 'init') {
    const bits: string[] = [];
    if (data.model) bits.push(String(data.model));
    if (Array.isArray(data.tools)) bits.push(`${data.tools.length} 个内置工具`);
    if (Array.isArray(data.mcp_servers)) bits.push(`MCP ${data.mcp_servers.length} 个`);
    return bits.length ? `运行环境：${bits.join(' · ')}` : '运行环境已就绪';
  }
  const itemType = String(data.item_type ?? '');
  if (itemType) return CODEX_ITEM_LABELS[itemType] ?? itemType;
  return null;
}

function pushStep(trace: RunTrace, step: TraceStep): RunTrace {
  return { ...trace, steps: [...trace.steps, step] };
}

/** 把还开着的推理步骤收尾（拿到下一个步骤的起点时间）。 */
function closeOpenThinking(trace: RunTrace, now: number): RunTrace {
  return {
    ...trace,
    steps: trace.steps.map((s) =>
      s.kind === 'thinking' && s.finishedAt === undefined ? { ...s, finishedAt: now } : s,
    ),
  };
}

/** 步骤 id：步骤只追加不删除，下标即唯一。 */
function nextId(trace: RunTrace): string {
  return `s${trace.steps.length}`;
}

/** 工具入参 → 一行预览（过长截断；对象序列化失败不炸） */
export function summarizeArgs(args?: Record<string, unknown>): string {
  if (!args) return '';
  const parts = Object.entries(args).map(([k, v]) => `${k}=${shortValue(v)}`);
  const s = parts.join(', ');
  return s.length > 140 ? `${s.slice(0, 140)}…` : s;
}

function shortValue(v: unknown): string {
  if (v === null || v === undefined) return String(v);
  if (typeof v === 'string') return v.length > 40 ? `${v.slice(0, 40)}…` : v;
  if (typeof v === 'object') {
    try {
      const s = JSON.stringify(v);
      return s.length > 60 ? `${s.slice(0, 60)}…` : s;
    } catch {
      return '[对象]';
    }
  }
  return String(v);
}
