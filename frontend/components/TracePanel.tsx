"use client";

/* 右侧面板（W6-S6）：检索轨迹（idle/running/done/miss）+ 命中来源（相关度条）+ 运行指标 + 后台任务。 */

import type { TaskDetail, TaskInfo } from "@/lib/api";
import { esc } from "@/lib/markdown";

export interface TraceStepVM {
  name: string;
  en: string;
  detail?: string;
  state: "idle" | "running" | "done" | "miss";
  time?: string;
}

export interface SourceVM {
  name: string;
  ref: string;
  pct: number;
}

export interface VitalsVM {
  recall: string;
  time: string;
  tok: string;
  web: string;
}

const TASK_STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  canceled: "已取消",
};

function shortTime(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (d.toDateString() === new Date().toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
}

export default function TracePanel(props: {
  steps: TraceStepVM[];
  sources: SourceVM[];
  vitals: VitalsVM;
  tasks: TaskInfo[];
  expandedTask: string | null;
  taskDetails: Record<string, TaskDetail | false | undefined>;
  onToggleTask: (taskId: string) => void;
  onTaskAction: (taskId: string, action: string) => void;
}) {
  return (
    <aside className="trace scroll">
      <div className="trace-inner">
        <div className="fig">
          <div className="fig-label">
            <span className="fig-no">01</span>
            <span>检索轨迹</span>
          </div>
          <div className="fig-sub">Retrieval Trace · 实时</div>
          <div className="steps" id="steps">
            {props.steps.map((s, i) => (
              <div className={`step ${s.state}`} key={s.en}>
                <div className="st-ic">{s.state === "done" ? "✓" : s.state === "miss" ? "✕" : `0${i + 1}`}</div>
                <div className="st-body">
                  <div className="st-name">{s.name}</div>
                  <div className="st-en">{s.en}</div>
                  {(s.state === "done" || s.state === "miss") && s.detail && <div className="st-detail">{s.detail}</div>}
                </div>
                <div className="st-time">{s.state === "done" || s.state === "miss" ? s.time || "" : "—"}</div>
              </div>
            ))}
          </div>
        </div>
        <div className="fig">
          <div className="fig-label">
            <span className="fig-no">02</span>
            <span>命中来源</span>
          </div>
          <div className="fig-sub">Sources · 按相关度排序</div>
          <div id="srcList">
            {props.sources.length === 0 ? (
              <div className="src-empty">
                暂无检索记录。
                <br />
                发起提问后，此处列出命中的知识块与相关度。
              </div>
            ) : (
              props.sources.map((s) => (
                <div className="src-item" key={s.ref}>
                  <div className="sr-top">
                    <span className="sr-name">{s.name}</span>
                    <span className="sr-pct">{s.pct}%</span>
                  </div>
                  <div className="sr-ref">{s.ref}</div>
                  <div className="rel-track">
                    <div className="rel-fill" style={{ width: `${s.pct}%` }} />
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
        <div className="fig">
          <div className="fig-label">
            <span className="fig-no">03</span>
            <span>运行指标</span>
          </div>
          <div className="fig-sub">Index Vitals · 本轮</div>
          <div className="vitals">
            <div className="v-row"><span className="k">向量模型</span><span>bge-local · 512d</span></div>
            <div className="v-row"><span className="k">重排模型</span><span>cross-encoder</span></div>
            <div className="v-row"><span className="k">本轮召回</span><span id="vRecall">{props.vitals.recall}</span></div>
            <div className="v-row"><span className="k">本轮耗时</span><span id="vTime">{props.vitals.time}</span></div>
            <div className="v-row"><span className="k">生成速率</span><span id="vTok">{props.vitals.tok}</span></div>
            <div className="v-row">
              <span className="k">联网回退</span>
              <span className={props.vitals.web === "已触发" ? "amber" : "green"} id="vWeb">{props.vitals.web}</span>
            </div>
          </div>
        </div>
        <div className="fig">
          <div className="fig-label">
            <span className="fig-no">04</span>
            <span>后台任务</span>
          </div>
          <div className="fig-sub">Background Tasks · 活跃 2s / 空闲 8s 自动刷新</div>
          <div id="taskList">
            {props.tasks.length === 0 ? (
              <div className="src-empty">
                暂无后台任务。
                <br />
                后台执行的问题会在此显示状态与步骤进度。
              </div>
            ) : (
              props.tasks.map((t) => {
                const expanded = props.expandedTask === t.task_id;
                const detail = props.taskDetails[t.task_id];
                const badgeClass =
                  t.status === "running" || t.status === "paused"
                    ? "st-running"
                    : t.status === "queued"
                      ? "st-queued"
                      : t.status === "completed"
                        ? "st-completed"
                        : "st-failed";
                const q = String(t.question || "");
                return (
                  <div className="task-item" key={t.task_id}>
                    <div className={`task-row${expanded ? " open" : ""}`} onClick={() => props.onToggleTask(t.task_id)} role="button" tabIndex={0}
                      onKeyDown={(e) => { if (e.key === "Enter") props.onToggleTask(t.task_id); }}>
                      <span className={`task-badge ${badgeClass}`}>{TASK_STATUS_LABEL[t.status] || t.status}</span>
                      <span className="task-q" title={q}>
                        {q.length > 24 ? `${q.slice(0, 24)}…` : q}
                      </span>
                      <span className="task-acts">
                        {t.status === "running" && (
                          <button className="task-act" type="button" onClick={(e) => { e.stopPropagation(); props.onTaskAction(t.task_id, "pause"); }}>暂停</button>
                        )}
                        {t.status === "paused" && (
                          <>
                            <button className="task-act" type="button" onClick={(e) => { e.stopPropagation(); props.onTaskAction(t.task_id, "resume"); }}>继续</button>
                            <button className="task-act danger" type="button" onClick={(e) => { e.stopPropagation(); props.onTaskAction(t.task_id, "cancel"); }}>取消</button>
                          </>
                        )}
                        {t.status === "queued" && (
                          <button className="task-act danger" type="button" onClick={(e) => { e.stopPropagation(); props.onTaskAction(t.task_id, "cancel"); }}>取消</button>
                        )}
                      </span>
                      <span className="task-time">{shortTime(t.updated_at || t.created_at)}</span>
                      <span className="task-caret">▶</span>
                    </div>
                    {expanded && (
                      <div className="task-detail">
                        {!detail ? (
                          <div className="task-empty-line">{detail === false ? "详情加载失败，重新点击可重试" : "详情加载中…"}</div>
                        ) : (
                          <TaskDetailView d={detail} />
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}

function TaskDetailView({ d }: { d: TaskDetail }) {
  const steps = Array.isArray(d.steps) ? d.steps : [];
  const doneCount = steps.filter((s) => s && s.ok).length;
  return (
    <>
      <div className="task-d-label">执行步骤 · {doneCount}/{steps.length}</div>
      {steps.length ? (
        steps.map((s, i) => (
          <div className="task-step" key={i}>
            <span className={`ts-ic ${s?.ok ? "ok" : "bad"}`}>{s?.ok ? "✓" : "×"}</span>
            <span className="ts-name">{esc(s?.action || "")}</span>
            {s && !s.ok && s.error ? <span className="ts-err">{esc(String(s.error).slice(0, 60))}</span> : ""}
          </div>
        ))
      ) : (
        <div className="task-empty-line">暂无步骤记录</div>
      )}
      {d.final_answer && (
        <>
          <div className="task-d-label">最终回答</div>
          <div className="task-answer">
            {esc(String(d.final_answer).slice(0, 120))}
            {String(d.final_answer).length > 120 ? "…" : ""}
          </div>
        </>
      )}
      {d.error && (
        <>
          <div className="task-d-label">错误信息</div>
          <div className="task-answer">{esc(String(d.error).slice(0, 120))}</div>
        </>
      )}
    </>
  );
}
