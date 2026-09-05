"use client";

/* W6-S2 根组件：会话/工作区状态层 + 状态轮询 + 心跳。
 * 后续切片：S3 发送/流式 → S4 答案卡 → S5 输入区行为 → S6 面板 → S7 设置。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type SessionInfo } from "@/lib/api";
import TopBar, { type IndexState } from "@/components/TopBar";
import Rail, { type KbSummary } from "@/components/Rail";
import Chat, { type MsgVM } from "@/components/Chat";
import Composer from "@/components/Composer";
import TracePanel from "@/components/TracePanel";

const INTRO: MsgVM = { kind: "intro" };

export default function Home() {
  const [workspaces, setWorkspaces] = useState<{ id: string; name: string }[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState("");
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [currentSession, setCurrentSession] = useState("");
  const [messages, setMessages] = useState<MsgVM[]>([INTRO]);
  const [indexState, setIndexState] = useState<IndexState>({ label: "连接中", ok: false });
  const [memCount, setMemCount] = useState(0);
  const [kb, setKb] = useState<KbSummary>({
    docCount: "—",
    chunkCount: "—",
    hitRate: "—",
    todayQueries: "—",
    syncLabel: "连接中",
  });
  const [ctxPct, setCtxPct] = useState(0);
  const [toast, setToast] = useState<{ msg: string; error: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((msg: string, error = false) => {
    setToast({ msg, error });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  }, []);

  /* ----- 会话 ----- */
  const openSession = useCallback(
    async (id: string, known?: SessionInfo[]) => {
      setCurrentSession(id);
      try {
        const list = known ?? sessions;
        if (!list.some((s) => s.id === id)) {
          const sd = await api.sessions(activeWorkspace);
          setSessions(sd.sessions);
        }
        const data = await api.sessionMessages(id);
        let lastQ = "";
        setMessages([
          INTRO,
          ...data.messages.map((m): MsgVM => {
            if (m.role === "user") {
              lastQ = m.content;
              return { kind: "user", text: m.content };
            }
            return { kind: "agent", text: m.content };
          }),
        ]);
      } catch (e) {
        showToast((e as Error).message || "加载会话失败", true);
      }
    },
    [activeWorkspace, sessions, showToast],
  );

  const createSession = useCallback(
    async (ws?: string) => {
      const workspaceId = ws ?? activeWorkspace;
      try {
        const d = await api.createSession(workspaceId);
        setSessions((prev) => [
          { id: d.session_id, title: "新的会话", updated_at: new Date().toISOString(), message_count: 0 },
          ...prev,
        ]);
        setMessages([INTRO]);
        setCurrentSession(d.session_id);
        return d.session_id;
      } catch (e) {
        showToast((e as Error).message || "新建会话失败", true);
        return null;
      }
    },
    [activeWorkspace, showToast],
  );

  const loadSessionsInto = useCallback(
    async (workspaceId: string, preferred?: string) => {
      const data = await api.sessions(workspaceId);
      setSessions(data.sessions);
      if (!data.sessions.length) {
        await createSession(workspaceId);
        return;
      }
      const id = preferred && data.sessions.some((s) => s.id === preferred) ? preferred : data.sessions[0].id;
      await openSession(id, data.sessions);
    },
    [createSession, openSession],
  );

  const onNewSession = useCallback(async () => {
    await createSession();
  }, [createSession]);

  const onDeleteSession = useCallback(
    async (id: string) => {
      const s = sessions.find((x) => x.id === id);
      if (!s) return;
      if (!confirm(`确定删除会话“${s.title}”吗？删除后无法恢复。`)) return;
      try {
        await api.deleteSession(id);
        showToast("会话已删除");
        const rest = sessions.filter((x) => x.id !== id);
        setSessions(rest);
        if (currentSession === id) {
          if (rest.length) await openSession(rest[0].id, rest);
          else await createSession();
        }
      } catch (e) {
        showToast((e as Error).message || "删除失败", true);
      }
    },
    [sessions, currentSession, openSession, createSession, showToast],
  );

  const onResetSession = useCallback(async () => {
    if (!currentSession) return;
    if (!confirm("确定清空当前会话的对话记录吗？可同时删除该会话长期记忆，删除后无法恢复。")) return;
    try {
      await api.resetSession(currentSession);
    } catch (_e) {
      /* 后端离线也照常清空本地视图 */
    }
    setMessages([INTRO]);
    setMemCount(0);
  }, [currentSession]);

  /* ----- 工作区 ----- */
  const onSwitchWorkspace = useCallback(
    async (id: string) => {
      const previous = activeWorkspace;
      try {
        await api.switchWorkspace(id);
        setActiveWorkspace(id);
        setMessages([INTRO]);
        await loadSessionsInto(id);
      } catch (e) {
        showToast((e as Error).message || "切换失败", true);
        setActiveWorkspace(previous);
      }
    },
    [activeWorkspace, loadSessionsInto, showToast],
  );

  /* ----- 初始化：workspaces → sessions → 首个会话 ----- */
  useEffect(() => {
    (async () => {
      try {
        const data = await api.workspaces();
        setWorkspaces(data.workspaces.map((w) => ({ id: w.id, name: w.name })));
        setActiveWorkspace(data.active);
        await loadSessionsInto(data.active);
      } catch (_e) {
        setIndexState({ label: "后端未连接", ok: false });
        setKb((k) => ({ ...k, syncLabel: "连接中断" }));
      }
    })();
    // 仅挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ----- 状态轮询（5s）+ 心跳（3s）----- */
  const refreshStatus = useCallback(async () => {
    try {
      const st = await api.status();
      setIndexState({ label: st.building ? "已连接 · 重建中" : "已连接 · 已同步", ok: true });
      setMemCount(st.memory_turns ?? 0);
      const used = st.context?.prompt_tokens ?? 0;
      const max = st.context?.max_context ?? 128000;
      setCtxPct(Math.min(1, used / max));
      const [kbR, stR] = await Promise.allSettled([api.kb(), api.stats()]);
      if (kbR.status === "fulfilled")
        setKb((k) => ({
          ...k,
          docCount: String(kbR.value.md_count ?? "—"),
          chunkCount: String(st.leaves ?? "—"),
          syncLabel: kbR.value.building ? "重建中" : "已同步",
        }));
      if (stR.status === "fulfilled")
        setKb((k) => ({
          ...k,
          hitRate: String(stR.value.hit_rate ?? "—"),
          todayQueries: String(stR.value.today_queries ?? "—"),
        }));
    } catch (_e) {
      setIndexState({ label: "后端未连接", ok: false });
      setKb((k) => ({ ...k, syncLabel: "连接中断" }));
    }
  }, []);

  useEffect(() => {
    const beat = () => {
      api.heartbeat().catch(() => {});
    };
    beat();
    const t = setInterval(beat, 3000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    refreshStatus();
    const t = setInterval(refreshStatus, 5000);
    return () => clearInterval(t);
  }, [refreshStatus]);

  /* ----- ⌘K 新建会话 ----- */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        void createSession();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [createSession]);

  const onToggleTheme = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("myagents-theme", next);
  };

  return (
    <div className="app">
      <TopBar
        workspaces={workspaces}
        activeWorkspace={activeWorkspace}
        indexState={indexState}
        memCount={memCount}
        onSwitchWorkspace={onSwitchWorkspace}
        onResetSession={onResetSession}
        onOpenSettings={() => showToast("设置面板将在 S7 迁移", false)}
        onToggleTheme={onToggleTheme}
      />
      <Rail
        sessions={sessions}
        currentSession={currentSession}
        kb={kb}
        onNewSession={onNewSession}
        onOpenSession={(id) => void openSession(id)}
        onDeleteSession={(id) => void onDeleteSession(id)}
        onManageKb={() => showToast("知识库管理将在 S7 迁移", false)}
      />
      <main className="chat">
        <Chat messages={messages} welcomeDocs={{ docCount: kb.docCount, chunks: kb.chunkCount }} />
        <Composer ctxPct={ctxPct} />
      </main>
      <TracePanel />
      <div className={`toast${toast ? " show" : ""}`} style={toast?.error ? { background: "var(--red)" } : undefined}>
        {toast?.msg}
      </div>
    </div>
  );
}
