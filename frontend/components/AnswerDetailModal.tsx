"use client";

/* W6-S4：回答详情弹窗（字段全部来自 meta 的 plan/metrics 真实数据；Esc 与遮罩关闭） */

import { useEffect } from "react";
import type { AgentStreamMsg } from "@/components/Chat";
import { esc } from "@/lib/markdown";

export default function AnswerDetailModal(props: { msg: AgentStreamMsg | null; onClose: () => void }) {
  const { msg } = props;
  useEffect(() => {
    if (!msg) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.metaKey && !e.ctrlKey) props.onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [msg, props]);

  if (!msg) return null;
  const metrics = msg.metrics || {};
  const plan = msg.plan || {};
  const steps = plan.steps || [];
  const sources = Array.isArray(msg.sources) ? msg.sources : [];
  const webUsed = !!(metrics.web_used || metrics.degraded);
  const mode = metrics.degraded
    ? "Web 降级（KB 未命中）"
    : webUsed
      ? "联网检索"
      : sources.length
        ? "知识库检索"
        : "模型直接回答";
  const citeInvalid = Number(metrics.citations_invalid || 0);
  const citeValid = Number(metrics.citations_valid || 0);
  const citeBadge = citeInvalid > 0
    ? `<span class="ad-badge bad">剔除 ${citeInvalid} 个无效引用</span>`
    : citeValid > 0
      ? `<span class="ad-badge ok">通过 · ${citeValid} 处</span>`
      : '<span class="ad-badge">未校验</span>';

  const rows: [string, string][] = [
    ["回答模式", `<span class="ad-badge ${webUsed ? "warn" : "ok"}">${mode}</span>`],
    ["是否联网", webUsed ? '<span class="ad-badge warn">是 · 发生外发</span>' : '<span class="ad-badge ok">否 · 仅本机</span>'],
  ];
  if (plan.summary) rows.push(["检索路径", esc(plan.summary)]);
  if (plan.rounds != null)
    rows.push(["规划轮次", `${esc(String(plan.rounds))} 轮 · 反思 ${Number(plan.reflections || 0)} 次`]);
  if (steps.length)
    rows.push([
      "工具调用",
      steps
        .map(
          (s) =>
            `<div class="task-step" style="justify-content:flex-end"><span class="ts-ic ${s.ok ? "ok" : "bad"}">${s.ok ? "✓" : "×"}</span><span class="ts-name">${esc(s.action || "")}</span>${!s.ok && s.error ? `<span class="ts-err">${esc(String(s.error).slice(0, 40))}</span>` : ""}</div>`,
        )
        .join(""),
    ]);
  rows.push(["引用校验", citeBadge]);
  rows.push(["命中来源", sources.length ? `${sources.length} 段` : "—"]);
  rows.push(["耗时", metrics.latency_s != null ? `${metrics.latency_s}s` : "—"]);
  rows.push(["Token", `${metrics.prompt_tokens ?? "—"} 输入 · ${metrics.completion_tokens ?? "—"} 输出`]);
  rows.push(["本次成本", `¥${metrics.cost_yuan ?? 0}`]);
  if (msg.messageId) rows.push(["消息 ID", `#${esc(msg.messageId)}`]);

  return (
    <div className="modal show" id="answerModal" role="dialog" aria-modal="true" aria-labelledby="answerDetailTitle" onClick={(e) => { if (e.target === e.currentTarget) props.onClose(); }}>
      <div className="modal-panel narrow">
        <div className="modal-head">
          <h2 id="answerDetailTitle">回答详情</h2>
          <button className="modal-close" onClick={props.onClose} aria-label="关闭" type="button">×</button>
        </div>
        <div id="answerDetailBody">
          {rows.map(([k, v]) => (
            <div className="ad-row" key={k}>
              <span className="k">{k}</span>
              <span className="v" dangerouslySetInnerHTML={{ __html: v }} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
