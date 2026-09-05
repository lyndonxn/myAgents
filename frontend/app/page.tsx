"use client";

/* W6 根组件。S3 流式聊天 + S4 答案卡全家桶接线。
 * 后续：S5 图片上传/拖入/语音 → S6 面板 → S7 设置 → S8 收尾切换。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  askImage,
  askStream,
  loadTaskDetail,
  loadTasks,
  ModelNotConfiguredError,
  postTaskAction,
  TASK_TERMINAL,
  type MetricsInfo,
  type PlanInfo,
  type SessionInfo,
  type SourceDetail,
  type TaskDetail,
  type TaskInfo,
} from "@/lib/api";
import TopBar, { type IndexState } from "@/components/TopBar";
import Rail, { type KbSummary } from "@/components/Rail";
import Chat, { type AgentStreamMsg, type ChatHandlers, type MsgVM } from "@/components/Chat";
import Composer, { type PendingImage } from "@/components/Composer";
import TracePanel, { type SourceVM, type TraceStepVM, type VitalsVM } from "@/components/TracePanel";
import AnswerDetailModal from "@/components/AnswerDetailModal";

const INTRO: MsgVM = { kind: "intro" };

/* 检索轨迹脚本（装饰性演示数据，与 legacy FLOW_RAG 一致） */
const IDLE_STEPS: TraceStepVM[] = [
  { name: "查询改写", en: "Query Rewrite", state: "idle" },
  { name: "向量检索", en: "Vector Search", state: "idle" },
  { name: "交叉重排", en: "Rerank · Top-5", state: "idle" },
  { name: "组装上下文", en: "Assemble Context", state: "idle" },
  { name: "生成回答", en: "Generate", state: "idle" },
  { name: "引用校验", en: "Citation Check", state: "idle" },
];
const TRACE_FLOW: { name: string; en: string; detail: string; time: string }[] = [
  { name: "查询改写", en: "Query Rewrite", detail: "原始问题 → 检索式 ×2", time: "0.08s" },
  { name: "向量检索", en: "Vector Search", detail: "召回 24 块 / 全库 295 块", time: "0.21s" },
  { name: "交叉重排", en: "Rerank · Top-5", detail: "cross-encoder 精排 24 → 5", time: "0.34s" },
  { name: "组装上下文", en: "Assemble Context", detail: "5 块 · 约 2.1k tokens", time: "0.05s" },
  { name: "生成回答", en: "Generate", detail: "流式输出", time: "1.20s" },
  { name: "引用校验", en: "Citation Check", detail: "全部命中", time: "0.06s" },
];

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
  /* 流式发送状态 */
  const [inputValue, setInputValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [, setTick] = useState(0);
  const seqRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const playerRef = useRef<{
    timer: ReturnType<typeof setInterval> | null;
    buf: string;
    shown: number;
    finished: boolean;
    active: boolean;
  } | null>(null);
  const streamFailedFlag = useRef(false);
  const [detailMsg, setDetailMsg] = useState<AgentStreamMsg | null>(null);
  const [pendingImage, setPendingImage] = useState<PendingImage | null>(null);
  /* ----- W6-S6 面板状态 ----- */
  const [traceSteps, setTraceSteps] = useState<TraceStepVM[]>(IDLE_STEPS);
  const [sources, setSources] = useState<SourceVM[]>([]);
  const [vitals, setVitals] = useState<VitalsVM>({ recall: "—", time: "—", tok: "—", web: "待命" });
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [expandedTask, setExpandedTask] = useState<string | null>(null);
  const [taskDetails, setTaskDetails] = useState<Record<string, TaskDetail | false | undefined>>({});
  const taskDetailCache = useRef<Record<string, TaskDetail | false>>({});

  const onPickImage = useCallback(
    (file: File) => {
      const reader = new FileReader();
      reader.onload = () => {
        setPendingImage({ name: file.name, dataUrl: String(reader.result) });
      };
      reader.readAsDataURL(file);
    },
    [],
  );
  const onClearImage = useCallback(() => setPendingImage(null), []);

  const showToast = useCallback((msg: string, error = false) => {
    setToast({ msg, error });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  }, []);

  const findMsg = useCallback(
    (messageId: string): AgentStreamMsg | null => {
      for (const m of messages) {
        if (m.kind === "stream" && String(m.data.messageId) === String(messageId)) return m.data;
      }
      return null;
    },
    [messages],
  );

  /* 回答落库后仅刷新列表（标题/计数）；消息保留在内存，不做整段替换（避免 legacy 的交换丢答案问题） */
  const refreshSessionsList = useCallback(async () => {
    try {
      const data = await api.sessions(activeWorkspace);
      setSessions(data.sessions);
    } catch (_e) {
      /* 静默：下一次轮询会补上 */
    }
  }, [activeWorkspace]);

  /* ----- 会话 ----- */
  const openSession = useCallback(
    async (id: string, known?: SessionInfo[]) => {
      setCurrentSession(id);
      setSources([]);
      setVitals({ recall: "—", time: "—", tok: "—", web: "待命" });
      setTraceSteps(IDLE_STEPS);
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
            return {
              kind: "stream",
              data: {
                kind: "stream",
                key: `h${m.id ?? "x"}-${++seqRef.current}`,
                phase: "done",
                shown: m.content,
                stageName: "",
                sources: m.sources || [],
                sourcesDetail: m.sources_detail || [],
                plan: m.plan,
                metrics: m.metrics,
                messageId: m.id != null ? String(m.id) : undefined,
                question: lastQ,
                feedback: m.feedback || "",
              },
            };
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

  const onDeleteSession = useCallback(
    async (id: string) => {
      if (busy) return;
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
    [busy, sessions, currentSession, openSession, createSession, showToast],
  );

  const onResetSession = useCallback(async () => {
    if (busy || !currentSession) return;
    if (!confirm("确定清空当前会话的对话记录吗？可同时删除该会话长期记忆，删除后无法恢复。")) return;
    try {
      await api.resetSession(currentSession);
    } catch (_e) {
      /* 后端离线也照常清空本地视图 */
    }
    setMessages([INTRO]);
    setMemCount(0);
  }, [busy, currentSession]);

  /* ----- 工作区 ----- */
  const onSwitchWorkspace = useCallback(
    async (id: string) => {
      if (busy) return;
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
    [busy, activeWorkspace, loadSessionsInto, showToast],
  );

  /* ----- W6-S6：检索轨迹动画（装饰性，与 legacy runTrace 一致）----- */
  const runTrace = useCallback(() => {
    setTraceSteps(TRACE_FLOW.map((s) => ({ ...s, state: "idle" as const })));
    let i = 0;
    const next = () => {
      if (i > 0) setTraceSteps((prev) => prev.map((s, idx) => (idx === i - 1 ? { ...s, state: "done" as const } : s)));
      if (i >= TRACE_FLOW.length) return;
      const cur = i;
      setTraceSteps((prev) => prev.map((s, idx) => (idx === cur ? { ...s, state: "running" as const } : s)));
      i += 1;
      setTimeout(next, 380 + Math.random() * 260);
    };
    next();
  }, []);

  /* ----- 流式问答（队列式播放器：delta 进缓冲、打字机节奏渲染，done 后收 followups）----- */
  const onSend = useCallback(
    async (textArg?: string) => {
      if (busy) {
        controllerRef.current?.abort();
        return;
      }
      const text = (textArg ?? inputValue).trim();
      if (!text && !pendingImage) return;
      if (!currentSession) {
        showToast("会话未就绪，请稍候", true);
        return;
      }
      const image = pendingImage;
      setPendingImage(null);
      setInputValue("");
      setBusy(true);
      runTrace();
      const key = `s${++seqRef.current}`;
      const msg: AgentStreamMsg = {
        kind: "stream",
        key,
        phase: "thinking",
        shown: "",
        stageName: "规划与检索",
        sources: [],
        sourcesDetail: [],
        question: text,
        feedback: "",
      };
      const displayText = text || (image ? `图片：${image.name}` : "");
      setMessages((prev) => [...prev, { kind: "user", text: displayText }, { kind: "stream", data: msg }]);
      if (image) {
        /* 以图搜库：/api/ask_image 非流式，一次性出卡 */
        try {
          const data = await askImage({ image_data_url: image.dataUrl, question: text, session_id: currentSession });
          msg.phase = "done";
          msg.shown = data.answer || "";
          msg.sources = data.sources || [];
          msg.sourcesDetail = data.sources_detail || [];
          msg.plan = data.plan;
          msg.metrics = data.metrics;
          msg.messageId = data.message_id != null ? String(data.message_id) : undefined;
          setTick((t) => t + 1);
          /* 右侧面板：命中来源 + 运行指标 */
          const rows = (data.sources || []).map((s, i) => ({
            name: String(s),
            ref: `来源 ${String(i + 1).padStart(2, "0")}`,
            pct: Math.max(68, 92 - i * 7),
          }));
          setSources(rows);
          setVitals({
            recall: `${rows.length} 个来源`,
            time: `${data.metrics?.latency_s ?? "—"}s`,
            tok: data.metrics?.completion_tokens ? `${data.metrics.completion_tokens} tokens` : "—",
            web: data.plan?.fallback ? "已触发" : "待命",
          });
        } catch (e) {
          msg.phase = "done";
          msg.shown = `请求失败：${(e as Error).message}`;
          setTick((t) => t + 1);
          streamFailedFlag.current = true;
        } finally {
          controllerRef.current = null;
          setBusy(false);
          if (!streamFailedFlag.current) {
            void refreshSessionsList();
            setMemCount((m) => m + 1);
          }
          streamFailedFlag.current = false;
        }
        return;
      }
      const controller = new AbortController();
      controllerRef.current = controller;
      let streamFailed = false;
      const player: NonNullable<typeof playerRef.current> = { timer: null, buf: "", shown: 0, finished: false, active: true };
      playerRef.current = player;
      player.timer = setInterval(() => {
        if (!player.active) return;
        if (player.shown < player.buf.length) {
          player.shown = Math.min(player.buf.length, player.shown + 4);
          msg.shown = player.buf.slice(0, player.shown);
          setTick((t) => t + 1);
        } else if (player.finished) {
          player.active = false;
          if (player.timer) clearInterval(player.timer);
          playerRef.current = null;
          msg.phase = "done";
          setTick((t) => t + 1);
        }
      }, 30);
      const streamed = {
        answer: "",
        sources: [] as string[],
        sourcesDetail: [] as SourceDetail[],
        plan: undefined as PlanInfo | undefined,
        metrics: undefined as MetricsInfo | undefined,
        message_id: "",
      };
      try {
        await askStream(
          { question: text, session_id: currentSession },
          {
            signal: controller.signal,
            onEvent: (ev) => {
              if (ev.event === "stage") {
                msg.stageName = String(ev.name || msg.stageName);
                if (msg.phase === "thinking") setTick((t) => t + 1);
              } else if (ev.event === "meta") {
                streamed.sources = (ev.sources as string[]) || [];
                streamed.sourcesDetail = (ev.sources_detail as SourceDetail[]) || [];
                streamed.plan = (ev.plan as PlanInfo) || undefined;
                streamed.metrics = (ev.metrics as MetricsInfo) || undefined;
                streamed.message_id = String(ev.message_id ?? "");
                msg.phase = "streaming";
                msg.sources = streamed.sources;
                msg.sourcesDetail = streamed.sourcesDetail;
                msg.plan = streamed.plan;
                msg.metrics = streamed.metrics;
                msg.messageId = streamed.message_id;
                setTick((t) => t + 1);
              } else if (ev.event === "delta") {
                streamed.answer += String(ev.text || "");
                player.buf = streamed.answer;
              } else if (ev.event === "followups") {
                msg.followups = ((ev.items as string[]) || []).slice(0, 3);
                if (player.finished) setTick((t) => t + 1);
              } else if (ev.event === "done" && ev.error) {
                player.active = false;
                if (player.timer) clearInterval(player.timer);
                playerRef.current = null;
                throw new Error(String(ev.error));
              }
            },
          },
        );
        player.finished = true;
        /* 右侧面板：命中来源 + 运行指标（W6-S6） */
        const rows = streamed.sources.map((s, i) => ({
          name: String(s),
          ref: `来源 ${String(i + 1).padStart(2, "0")}`,
          pct: Math.max(68, 92 - i * 7),
        }));
        setSources(rows);
        setVitals({
          recall: `${rows.length} 个来源`,
          time: `${streamed.metrics?.latency_s ?? "—"}s`,
          tok: streamed.metrics?.completion_tokens ? `${streamed.metrics.completion_tokens} tokens` : "—",
          web: streamed.plan?.fallback ? "已触发" : "待命",
        });
      } catch (e) {
        const aborted = e instanceof DOMException && e.name === "AbortError";
        streamFailed = !aborted;
        player.active = false;
        if (player.timer) clearInterval(player.timer);
        playerRef.current = null;
        msg.phase = "done";
        setMessages((prev) => {
          const arr = [...prev];
          const last = arr[arr.length - 1];
          if (last.kind === "stream" && last.data === msg && !msg.shown) arr.pop();
          return arr;
        });
        setMessages((prev) => [
          ...prev,
          {
            kind: "stream",
            data: {
              kind: "stream",
              key: `e${++seqRef.current}`,
              phase: "done",
              shown: aborted ? "思考已停止，本次回答已中断。" : `请求失败：${(e as Error).message}`,
              stageName: "",
              sources: [],
              sourcesDetail: [],
              question: "",
              feedback: "",
            },
          },
        ]);
        if (e instanceof ModelNotConfiguredError) showToast(`${e.message}（设置面板 S7 迁移）`, true);
      } finally {
        controllerRef.current = null;
        setBusy(false);
        if (!streamFailed) {
          void refreshSessionsList();
          setMemCount((m) => m + 1);
        }
      }
    },
    [busy, inputValue, currentSession, showToast, refreshSessionsList],
  );

  const onStop = useCallback(() => {
    controllerRef.current?.abort();
  }, []);

  /* ----- 答案卡 handlers（S4）----- */
  const handlers: ChatHandlers = useMemo(
    () => ({
      onCopy: async (d) => {
        try {
          await navigator.clipboard.writeText(d.answer);
          showToast("回答已复制");
        } catch (_e) {
          showToast("复制失败，请检查浏览器权限", true);
        }
      },
      onShare: async (d) => {
        const payload = d.question ? `${d.question}\n\n${d.answer}` : d.answer;
        if (navigator.share) {
          try {
            await navigator.share({ title: "MYAGENTS 回答", text: payload });
            return;
          } catch (_e) {
            /* 用户取消 → 复制兜底 */
          }
        }
        try {
          await navigator.clipboard.writeText(payload);
          showToast("回答已复制，可粘贴分享");
        } catch (_e) {
          showToast("分享失败，请检查浏览器权限", true);
        }
      },
      onRegenerate: (d) => {
        if (busy || !d.question) return;
        void onSend(d.question);
      },
      onFeedback: async (messageId, value) => {
        try {
          await api.feedback(Number(messageId), value);
          const target = findMsg(messageId);
          if (target) target.feedback = value;
          setTick((t) => t + 1);
          showToast("感谢反馈");
        } catch (_e) {
          showToast("反馈保存失败", true);
        }
      },
      onQuickAsk: (text) => {
        void onSend(text);
      },
      onOpenDetail: (messageId) => {
        const m = findMsg(messageId);
        if (!m) {
          showToast("未找到该回答的详情", true);
          return;
        }
        setDetailMsg(m);
      },
    }),
    [busy, findMsg, onSend, showToast],
  );

  /* 回答序号（RESPONSE·NN）：直接在消息对象上标记，数量变化时重排 */
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useMemo(() => {
    let n = 0;
    for (const m of messages) {
      if (m.kind === "stream") m.data.respNo = ++n;
    }
    return messages.length;
  }, [messages]);

  /* ----- 初始化 ----- */
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
        if (!busy) void createSession();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [busy, createSession]);

  /* ----- W6-S6：后台任务轮询（活跃 2s / 空闲 8s，页面隐藏降频）----- */
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (stopped) return;
      try {
        const list = await loadTasks();
        if (!stopped) setTasks(list);
      } catch (_e) {
        /* 后端未连接保留现有列表 */
      }
      if (stopped) return;
      const active = tasks.some((t) => !TASK_TERMINAL.has(t.status));
      timer = setTimeout(tick, document.hidden ? 8000 : active ? 2000 : 8000);
    };
    tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [tasks]);

  const onToggleTask = useCallback(
    async (taskId: string) => {
      const next = expandedTask === taskId ? null : taskId;
      setExpandedTask(next);
      if (!next) return;
      try {
        const d = await loadTaskDetail(taskId);
        taskDetailCache.current[taskId] = d;
        setTaskDetails((prev) => ({ ...prev, [taskId]: d }));
      } catch (_e) {
        setTaskDetails((prev) => ({ ...prev, [taskId]: false }));
      }
    },
    [expandedTask],
  );

  const onTaskAction = useCallback(
    async (taskId: string, action: string) => {
      try {
        await postTaskAction(taskId, action);
        showToast(action === "pause" ? "任务已暂停" : action === "resume" ? "任务已继续" : "任务已取消");
        delete taskDetailCache.current[taskId];
        setTaskDetails((prev) => ({ ...prev, [taskId]: undefined }));
        setTasks(await loadTasks());
      } catch (e) {
        showToast((e as Error).message || "操作失败", true);
      }
    },
    [showToast],
  );

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
        onNewSession={() => {
          if (!busy) void createSession();
        }}
        onOpenSession={(id) => {
          if (!busy) void openSession(id);
        }}
        onDeleteSession={(id) => void onDeleteSession(id)}
        onManageKb={() => showToast("知识库管理将在 S7 迁移", false)}
      />
      <main className="chat">
        <Chat messages={messages} welcomeDocs={{ docCount: kb.docCount, chunks: kb.chunkCount }} handlers={handlers} />
        <Composer
          ctxPct={ctxPct}
          value={inputValue}
          busy={busy}
          pendingImage={pendingImage}
          onChange={setInputValue}
          onSubmit={() => void onSend()}
          onStop={onStop}
          onPickImage={onPickImage}
          onClearImage={onClearImage}
        />
      </main>
      <TracePanel
        steps={traceSteps}
        sources={sources}
        vitals={vitals}
        tasks={tasks}
        expandedTask={expandedTask}
        taskDetails={taskDetails}
        onToggleTask={(id) => void onToggleTask(id)}
        onTaskAction={(id, action) => void onTaskAction(id, action)}
      />
      <AnswerDetailModal msg={detailMsg} onClose={() => setDetailMsg(null)} />
      <div className={`toast${toast ? " show" : ""}`} style={toast?.error ? { background: "var(--red)" } : undefined}>
        {toast?.msg}
      </div>
    </div>
  );
}
