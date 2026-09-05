/* W6：后端 API 封装（与 legacy fetch 逻辑 1:1 对齐；全部同源相对路径）。
 * dev 模式（NEXT_DEV_PROXY=1）由 Next rewrites 代理到 8787，浏览器仍视为同源。 */

export interface SourceDetail {
  title: string;
  path: string;
  heading: string;
  snippet: string;
}

export interface PlanStep {
  action: string;
  input?: unknown;
  ok: boolean;
  error?: string;
}

export interface PlanInfo {
  summary?: string;
  steps?: PlanStep[];
  fallback?: boolean;
  rounds?: number;
  reflections?: unknown[] | number;
}

export interface MetricsInfo {
  latency_s?: number;
  llm_calls?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  cost_yuan?: number;
  citations_valid?: number;
  citations_invalid?: number;
  degraded?: boolean;
  web_used?: boolean;
  task_id?: string;
}

export type FeedbackValue = "" | "up" | "down";

export interface SessionMsg {
  role: "user" | "assistant";
  id?: number;
  content: string;
  sources?: string[];
  sources_detail?: SourceDetail[];
  plan?: PlanInfo;
  metrics?: MetricsInfo;
  feedback?: FeedbackValue;
}

export interface SessionInfo {
  id: string;
  title: string;
  updated_at: string;
  message_count?: number;
}

export interface WorkspaceInfo {
  id: string;
  name: string;
  kb_path: string;
  active?: number;
}

export interface StatusInfo {
  llm_configured?: boolean;
  building?: boolean;
  leaves?: number;
  memory_turns?: number;
  context?: { prompt_tokens?: number; max_context?: number };
}

export interface KbInfo {
  path?: string;
  exists?: boolean;
  md_count?: number;
  building?: boolean;
  build_error?: string;
}

export interface StatsInfo {
  today_queries?: number;
  hit_rate?: number;
  kb_hits?: number;
}

async function jget<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

async function jpost<T = Record<string, unknown>>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const data = (await res.json().catch(() => ({}))) as T & { error?: string };
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

export const api = {
  sessions: (workspaceId: string) =>
    jget<{ sessions: SessionInfo[] }>(`/api/sessions?workspace_id=${encodeURIComponent(workspaceId)}`),
  sessionMessages: (id: string) => jget<{ messages: SessionMsg[] }>(`/api/sessions/${encodeURIComponent(id)}`),
  createSession: (workspaceId: string) => jpost<{ session_id: string }>("/api/sessions", { workspace_id: workspaceId }),
  deleteSession: (id: string) => jpost("/api/sessions", { action: "delete", session_id: id }),
  resetSession: (id: string) => jpost("/api/reset", { session_id: id }),
  workspaces: () => jget<{ workspaces: WorkspaceInfo[]; active: string }>("/api/workspaces"),
  switchWorkspace: (id: string) => jpost("/api/workspaces", { action: "switch", workspace_id: id }),
  status: () => jget<StatusInfo>("/api/status"),
  kb: () => jget<KbInfo>("/api/kb"),
  stats: () => jget<StatsInfo>("/api/stats"),
  heartbeat: () => jpost("/api/heartbeat"),
  feedback: (messageId: number, value: Exclude<FeedbackValue, "">) =>
    jpost("/api/feedback", { message_id: messageId, value }),
};

/* ----- 以图搜库（/api/ask_image 非流式）----- */

export interface AskImageResult {
  answer: string;
  error?: string;
  sources?: string[];
  sources_detail?: SourceDetail[];
  plan?: PlanInfo;
  metrics?: MetricsInfo;
  message_id?: number;
}

export async function askImage(payload: {
  image_data_url: string;
  question: string;
  session_id?: string;
}): Promise<AskImageResult> {
  const res = await fetch("/api/ask_image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = (await res.json().catch(() => ({}))) as AskImageResult & { error?: string };
  if (!res.ok || data.error) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

/* ----- 后台任务（T2 面板）----- */

export interface TaskInfo {
  task_id: string;
  status: string;
  question: string;
  created_at?: string;
  updated_at?: string;
}

export interface TaskDetail {
  steps?: { action?: string; ok?: boolean; error?: string }[];
  final_answer?: string;
  error?: string;
}

export const TASK_TERMINAL = new Set(["completed", "failed", "canceled"]);

export async function loadTasks(): Promise<TaskInfo[]> {
  const data = await jget<{ tasks?: TaskInfo[] }>("/api/tasks");
  return data.tasks || [];
}

export async function loadTaskDetail(taskId: string): Promise<TaskDetail> {
  const data = await jget<TaskDetail & { error?: string }>(`/api/tasks/${encodeURIComponent(taskId)}`);
  if (data.error) throw new Error(data.error);
  return data;
}

export async function postTaskAction(taskId: string, action: string): Promise<void> {
  await jpost(`/api/tasks/${encodeURIComponent(taskId)}/${action}`);
}

/* ----- 流式问答（NDJSON：stage → meta → delta → done → followups?）----- */

export class ModelNotConfiguredError extends Error {
  constructor() {
    super("请先在设置中配置模型 API Key");
    this.name = "ModelNotConfiguredError";
  }
}

export async function askStream(
  payload: { question: string; session_id?: string },
  handlers: { onEvent: (ev: Record<string, unknown>) => void; signal: AbortSignal },
): Promise<void> {
  const res = await fetch("/api/ask_stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: handlers.signal,
  });
  if (!res.ok) {
    let failure: { error?: string; code?: string } = {};
    try {
      failure = await res.json();
    } catch (_e) {
      /* 非 JSON 错误体 */
    }
    if (failure.code === "MODEL_NOT_CONFIGURED") throw new ModelNotConfiguredError();
    throw new Error(failure.error || `请求失败（${res.status}）`);
  }
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop()!;
    for (const line of lines) {
      if (line.trim()) handlers.onEvent(JSON.parse(line) as Record<string, unknown>);
    }
    if (done) break;
  }
}

/* ----- 设置（W6-S7）----- */

export interface AppConfig {
  llm: {
    api_key_masked?: string;
    base_url?: string;
    chat_model?: string;
    mode?: string;
    temperature: number;
    max_tokens: number;
  };
  retrieval: {
    top_k: number;
    rerank: string;
    rerank_candidates: number;
    multi_query: boolean;
    reranker_model?: string;
  };
  vision: { api_key_masked?: string; base_url?: string; model?: string };
  tools?: { kb_fallback_web?: boolean };
  memory?: { long_term_enabled?: boolean; entities_enabled?: boolean };
}

export interface ConfigSavePayload {
  llm: {
    api_key: string;
    base_url: string;
    chat_model: string;
    mode: string;
    temperature: number;
    max_tokens: number;
  };
  retrieval: {
    top_k: number;
    rerank: string;
    rerank_candidates: number;
    multi_query: boolean;
    reranker_model: string;
  };
  vision: { api_key: string; base_url: string; model: string };
  tools: { kb_fallback_web: boolean };
  memory: { long_term_enabled: boolean; entities_enabled: boolean };
}

export const getConfig = () => jget<AppConfig>("/api/config");
export const saveConfig = (payload: ConfigSavePayload) => jpost("/api/config", payload);
export const rebuildKb = (path: string) => jpost("/api/kb", { path });

export interface MemoryEpisode {
  id: string;
  question: string;
  answer_summary: string;
  session_id?: string;
  ts?: string;
  hits?: number;
  sources?: string[];
}

export const loadMemory = (q: string) =>
  jget<{ episodes: MemoryEpisode[]; total?: number; disabled?: boolean }>(
    `/api/memory?q=${encodeURIComponent(q)}&limit=100`,
  );
export const loadMemoryEntities = () =>
  jget<{ entities: Record<string, { facts?: string[]; last_seen?: string }> }>("/api/memory/entities");
export async function deleteEpisode(id: string): Promise<void> {
  const res = await fetch(`/api/memory/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}
export const clearMemoryAll = () => jpost("/api/memory/clear");

/* ----- 日志（W6-S7）----- */

export const loadLogs = (lines: number) => jget<{ logs?: string[]; error?: string }>(`/api/logs?lines=${lines}`);
export const clearLogs = () => jpost("/api/logs", { action: "clear" });

/* ----- 工作区创建（W6-S7）----- */

export const createWorkspace = (name: string, kb_path: string) =>
  jpost<{ workspace_id: string }>("/api/workspaces", { action: "create", name, kb_path });
