'use client';

import { useCallback, useEffect, useState } from 'react';
import useSWR from 'swr';

import {
  NEW_SKILL_TEMPLATE,
  SKILL_NAME_RE,
  deleteSkill,
  getCapabilities,
  getSkill,
  putSkill,
  readSkillFrontmatter,
  repairSkillContent,
} from '@/lib/agent-api';

function msgOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * skill 编辑器：直接改 config/skills/<name>/SKILL.md 的原文。
 *
 * 改完立刻生效 —— 工作区每轮对话都会重新同步 skill 目录（见后端
 * `CliAgentService._workspace_for`），不需要重启服务。
 */
export default function SkillEditor() {
  const { data: caps, mutate } = useSWR('/agent/capabilities', getCapabilities);
  const skills = caps?.skills ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [newName, setNewName] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  // 新建态：这个名字服务端还没有，不能去 GET（否则 404 会把引导文案冲掉，
  // 而且名字撞上已有 skill 时会静默把模板替换成别人的正文）
  const [isNew, setIsNew] = useState(false);
  // 列表搜索：skill 多了以后「滚动找」比「敲两个字」慢得多
  const [query, setQuery] = useState('');

  const filtered = query.trim()
    ? skills.filter((s) => s.name.toLowerCase().includes(query.trim().toLowerCase()))
    : skills;

  // 首次拉到清单后默认选中第一个
  useEffect(() => {
    if (selected === null && skills.length > 0) setSelected(skills[0].name);
  }, [skills, selected]);

  // 注意：这里**不**清 msg —— 清空放在真正发起动作的地方（点列表 / 保存 / 删除 /
  // 新建）。在 effect 里清会把「新建时名字已存在」那类提示当场冲掉：setSelected
  // 触发本 effect，提示刚 set 就被擦掉，用户看不到任何解释。
  useEffect(() => {
    if (!selected || isNew) return;
    let alive = true;
    getSkill(selected)
      .then((r) => {
        if (alive) setContent(r.content);
      })
      .catch((e) => {
        if (alive) setMsg(`✗ ${msgOf(e)}`);
      });
    return () => {
      alive = false;
    };
  }, [selected, isNew]);

  const save = useCallback(async () => {
    if (!selected) return;
    setBusy(true);
    setMsg('');
    try {
      await putSkill(selected, content);
      setIsNew(false);
      await mutate();
      setMsg(`✓ 已保存 ${selected}`);
    } catch (e) {
      setMsg(`✗ ${msgOf(e)}`);
    } finally {
      setBusy(false);
    }
  }, [selected, content, mutate]);

  const remove = useCallback(async () => {
    if (!selected) return;
    if (!window.confirm(`删除 skill「${selected}」？会删掉 config/skills/${selected}/ 整个目录。`)) {
      return;
    }
    setBusy(true);
    setMsg('');
    try {
      await deleteSkill(selected);
      setIsNew(false);
      setSelected(null);
      setContent('');
      await mutate();
      setMsg(`✓ 已删除 ${selected}`);
    } catch (e) {
      setMsg(`✗ ${msgOf(e)}`);
    } finally {
      setBusy(false);
    }
  }, [selected, mutate]);

  const create = useCallback(() => {
    const name = newName.trim();
    if (!name) return;
    setNewName('');
    // 名字撞上已有 skill 时进编辑态（而不是拿模板覆盖别人的正文）
    if (skills.some((s) => s.name === name)) {
      setIsNew(false);
      setSelected(name);
      setMsg(`「${name}」已存在，已切到编辑`);
      return;
    }
    setIsNew(true);
    setSelected(name);
    // 用模板起头，name 直接填好，用户只改 description 与正文
    setContent(NEW_SKILL_TEMPLATE.replace(/my-skill/g, name));
    setMsg(`新建「${name}」：改完点保存落盘`);
  }, [newName, skills]);

  return (
    <div id="skills" className="scroll-mt-4 rounded-xl border bg-white p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <div className="text-sm font-medium">Skill（config/skills/*/SKILL.md）</div>
        <span className="text-xs text-neutral-400">保存后下一轮对话即生效，无需重启</span>
      </div>

      {msg && <div className="mb-2 rounded-md border px-3 py-1.5 text-sm">{msg}</div>}

      <div className="flex gap-4">
        <div className="w-56 shrink-0">
          <div className="mb-1.5 flex gap-1">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="新 skill 名"
              className="min-w-0 flex-1 rounded-md border px-2 py-1 font-mono text-xs text-neutral-900"
            />
            <button
              onClick={create}
              disabled={!newName.trim()}
              className="rounded border px-2 py-0.5 text-xs hover:bg-neutral-100 disabled:opacity-40"
            >
              新建
            </button>
          </div>
          <ul className="max-h-72 overflow-y-auto border-t">
            {filtered.map((s) => (
              <li key={s.name}>
                <button
                  onClick={() => {
                    setMsg('');
                    setIsNew(false);
                    setSelected(s.name);
                  }}
                  className={`w-full px-2 py-1.5 text-left text-xs ${
                    s.name === selected ? 'bg-neutral-100 font-medium' : 'hover:bg-neutral-50'
                  }`}
                >
                  <span className="font-mono">{s.name}</span>
                  {!s.valid && (
                    <span className="ml-1.5 rounded bg-amber-50 px-1 text-[10px] text-amber-700">
                      名称或 frontmatter 不合格
                    </span>
                  )}
                  <div className="truncate text-neutral-400">{s.description || '—'}</div>
                </button>
              </li>
            ))}
            {filtered.length === 0 && (
              <li className="px-2 py-4 text-center text-xs text-neutral-400">
                {skills.length === 0 ? '暂无 skill' : `没有匹配「${query.trim()}」的 skill`}
              </li>
            )}
          </ul>
          {skills.length > 0 && (
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`搜索 ${skills.length} 个 skill`}
              aria-label="搜索 skill"
              className="mt-1.5 w-full rounded-md border px-2 py-1 text-xs text-neutral-900"
            />
          )}
        </div>

        <div className="min-w-0 flex-1">
          {selected ? (
            <>
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={18}
                spellCheck={false}
                aria-label={`${selected} 的 SKILL.md`}
                className="w-full rounded-md border px-3 py-2 font-mono text-xs text-neutral-900"
              />
              <RepairBanner
                dirName={selected}
                content={content}
                isNew={isNew}
                onRepair={(next) => {
                  setContent(next);
                  setMsg('已补全 frontmatter：确认无误后点保存');
                }}
              />
              <div className="mt-2 flex gap-2">
                <button
                  onClick={() => void save()}
                  disabled={busy}
                  className="rounded border px-3 py-1 text-xs hover:bg-neutral-100 disabled:opacity-40"
                >
                  {busy ? '…' : '保存'}
                </button>
                <button
                  onClick={() => void remove()}
                  disabled={busy}
                  className="rounded border px-3 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-40"
                >
                  删除
                </button>
                <span className="self-center text-xs text-neutral-400">
                  frontmatter 必填 name 与 description，否则 agent 看不到这个 skill
                </span>
              </div>
            </>
          ) : (
            <div className="py-16 text-center text-sm text-neutral-400">
              左侧选一个 skill，或新建一个
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * frontmatter 不合格时的就地修复提示。
 *
 * 为什么值得一个专门的条：不合格的 skill 在能力清单里是 ``valid=false``
 * （选不了、Agent Card 里也不出现），但用户看到的现象只是「这个 skill 用不上」——
 * 没有任何地方告诉他「因为 frontmatter 缺 description」。这里把缺什么、
 * 怎么一键补全说清楚。
 *
 * 只对**已存在**的 skill 提示：新建态下用户正在按模板改，此刻「还没写名字」
 * 是正常中间状态，弹一个警告只会吓人。
 */
function RepairBanner({
  dirName,
  content,
  isNew,
  onRepair,
}: {
  dirName: string;
  content: string;
  isNew: boolean;
  onRepair: (next: string) => void;
}) {
  if (isNew) return null;
  const fm = readSkillFrontmatter(content);
  const missing: string[] = [];
  if (fm === null) missing.push('整段 frontmatter（文件要以 `---` 开头并以 `---` 结束）');
  else {
    if (!fm.name.trim()) missing.push('name');
    if (!fm.description.trim()) missing.push('description');
  }
  const dirOk = SKILL_NAME_RE.test(dirName);
  if (missing.length === 0 && dirOk) return null;

  return (
    <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
      {missing.length > 0 ? (
        <div>
          这个 skill 目前<strong>不可用</strong>：缺少 {missing.join('、')}。
          agent 看不到它，能力清单里也选不上。
        </div>
      ) : null}
      {!dirOk ? (
        <div className="mt-1">
          目录名 <code className="font-mono">{dirName}</code> 不合规（只能用小写字母、
          数字与短横线）。这一项<strong>改正文修不好</strong> —— 需要用合规的名字
          新建一个 skill，再把正文搬过去。
        </div>
      ) : null}
      {missing.length > 0 ? (
        <button
          type="button"
          onClick={() => onRepair(repairSkillContent(content, dirName))}
          className="mt-1.5 rounded border border-amber-300 bg-white px-2 py-0.5 hover:bg-amber-100"
        >
          补全 frontmatter（name 用目录名）
        </button>
      ) : null}
    </div>
  );
}
