"use client";

/* W6-S4：完整答案卡（legacy liveAnswerHTML 的 React 化）。
 * 结构：卡头（RESPONSE·NN + 复制/重新生成/分享）→ 两态状态条 → 引用校验/联网披露 →
 * 正文（markdown + 参考来源折叠 + 引用 chip 跳转）→ 执行轨迹 → 证据折叠（三级富卡）→
 * 操作行 → 追问区。事件用容器级委托（cite/followup）。 */

import { useRef, useState } from "react";
import type { FeedbackValue, MetricsInfo, PlanInfo, SourceDetail } from "@/lib/api";
import { esc, splitAnswerHtml } from "@/lib/markdown";

export interface AnswerData {
  respNo: number;
  question?: string;
  answer: string;
  messageId?: string;
  sources: string[];
  sourcesDetail: SourceDetail[];
  plan?: PlanInfo;
  metrics?: MetricsInfo;
  feedback: FeedbackValue;
  followups?: string[];
}

/* 证据折叠状态按 messageId 记忆（跨重渲染/会话切换，legacy EV_FOLD_OPEN 对齐） */
const EV_FOLD_OPEN = new Map<string, boolean>();

const SVG_COPY = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const SVG_REGEN = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
const SVG_SHARE = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>';
const SVG_BOOK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>';
const SVG_GLOBE = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';
const SVG_CHATBOT = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
const SVG_GEAR = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v4m0 14v4M4.22 4.22l2.83 2.83m9.9 9.9l2.83 2.83M1 12h4m14 0h4M4.22 19.78l2.83-2.83m9.9-9.9l2.83-2.83"/></svg>';
const SVG_THUMB_UP = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>';
const SVG_THUMB_DOWN = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zM17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/></svg>';

export default function AnswerCard(props: {
  data: AnswerData;
  onCopy: (d: AnswerData) => void;
  onShare: (d: AnswerData) => void;
  onRegenerate: (d: AnswerData) => void;
  onFeedback: (messageId: string, value: Exclude<FeedbackValue, "">) => void;
  onQuickAsk: (text: string) => void;
  onOpenDetail: (messageId: string) => void;
}) {
  const { data } = props;
  const key = data.messageId || "";
  const [evOpen, setEvOpen] = useState(() => EV_FOLD_OPEN.get(key) ?? false);
  const [tailOpen, setTailOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);

  const webUsed = !!(data.metrics?.degraded || data.metrics?.web_used);
  const mode = webUsed
    ? { icon: SVG_GLOBE, text: "联网", warn: true }
    : data.sources.length
      ? { icon: SVG_BOOK, text: "知识库", warn: false }
      : { icon: SVG_CHATBOT, text: "模型回答", warn: false };
  const firstTool = data.plan?.steps?.find((s) => s && s.action)?.action || "";

  const citeInvalid = Number(data.metrics?.citations_invalid || 0);
  const citeValid = Number(data.metrics?.citations_valid || 0);
  const citeLine =
    citeInvalid > 0 ? (
      <div className="block cite-warn show">引用校验：剔除 {citeInvalid} 个无效引用</div>
    ) : citeValid > 0 ? (
      <div className="block cite-ok show">引用校验 通过</div>
    ) : null;
  /* 披露字符串与 tests/test_egress_defaults 静态断言一致（S8 将断言迁到本文件） */
  const webState = data.metrics?.degraded ? (
    <div className="block cite-warn show">本回答来自 Web 搜索降级</div>
  ) : data.metrics?.web_used ? (
    <div className="block cite-warn show">本回答使用了 Web 搜索</div>
  ) : null;

  const toggleEv = () => {
    const v = !evOpen;
    setEvOpen(v);
    if (key) EV_FOLD_OPEN.set(key, v);
  };

  const jumpToEv = (n: number) => {
    if (!data.sources.length) return;
    if (!evOpen) {
      setEvOpen(true);
      if (key) EV_FOLD_OPEN.set(key, true);
    }
    setTimeout(() => {
      const bar = cardRef.current?.querySelector(".retrieval-bar");
      const row = bar?.querySelector(`.rb-chunk[data-idx="${n}"]`) as HTMLElement | null;
      if (!row) return;
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.remove("flash");
      void row.offsetWidth;
      row.classList.add("flash");
      setTimeout(() => row.classList.remove("flash"), 1400);
    }, 60);
  };

  const onCardClick = (e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    const cite = t.closest(".cite") as HTMLElement | null;
    if (cite?.dataset.cite) {
      jumpToEv(Number(cite.dataset.cite));
      return;
    }
    const fu = t.closest(".followup") as HTMLElement | null;
    if (fu?.dataset.q) props.onQuickAsk(fu.dataset.q);
  };

  const split = splitAnswerHtml(data.answer);
  const planSteps = data.plan?.steps || [];
  const planHtml = planSteps.length ? (
    <div className="block note show">
      <b>{esc(data.plan?.summary || "执行轨迹")}</b>
      <br />
      {planSteps.map((s) => `${s.ok ? "✓" : "×"} ${esc(s.action)}`).join(" · ")}
    </div>
  ) : null;

  const evRows = data.sources.map((s, i) => {
    const d: Partial<SourceDetail> = data.sourcesDetail[i] || {};
    const idx = String(i + 1).padStart(2, "0");
    if (d.title || d.snippet) {
      const crumb = [d.path, d.heading].filter(Boolean).join(" › ");
      return (
        <div className="rb-chunk rich" data-idx={i + 1} key={i}>
          <span className="ck">#{idx}</span>
          <div className="rc-main">
            <div className="rc-title">{esc(d.title || s)}</div>
            {crumb && <div className="rc-crumb">{esc(crumb)}</div>}
            {d.snippet && <div className="rc-quote">{esc(d.snippet)}</div>}
          </div>
        </div>
      );
    }
    return (
      <div className="rb-chunk" data-idx={i + 1} key={i}>
        <span className="ck">#{idx}</span>
        <span>{esc(s)}</span>
      </div>
    );
  });

  return (
    <div className="msg agent">
      <div className="avatar">M</div>
      <div className="m-main">
        <div className="msg-label">
          <span>MYAGENTS</span>
          <span className="role-tag">RAG · PLANNER · TOOLS</span>
        </div>
        <div className="card" ref={cardRef} onClick={onCardClick}>
          {/* 卡头（图1） */}
          <div className="ans-head">
            <span className="ans-no">RESPONSE · {String(data.respNo || 1).padStart(2, "0")}</span>
            <div className="ans-acts">
              <button className="ans-icon" title="复制" aria-label="复制" type="button" onClick={() => props.onCopy(data)} dangerouslySetInnerHTML={{ __html: SVG_COPY }} />
              {data.question && (
                <button className="ans-icon" title="重新生成" aria-label="重新生成" type="button" onClick={() => props.onRegenerate(data)} dangerouslySetInnerHTML={{ __html: SVG_REGEN }} />
              )}
              <button className="ans-icon" title="分享" aria-label="分享" type="button" onClick={() => props.onShare(data)} dangerouslySetInnerHTML={{ __html: SVG_SHARE }} />
            </div>
          </div>
          {/* L1 状态条 */}
          <div className="ans-status">
            <span className={`st-mode${mode.warn ? " warn" : ""}`}>
              <span style={{ display: "inline-flex" }} dangerouslySetInnerHTML={{ __html: mode.icon }} />
              {mode.text}
            </span>
            {data.metrics?.task_id && <span className="kb-badge task-flag">后台任务</span>}
            {firstTool && (
              <>
                <span className="st-sep">·</span>
                <span className="st-tool">{esc(firstTool)}</span>
              </>
            )}
            {data.sources.length > 0 && (
              <>
                <span className="st-sep">·</span>
                <span>命中 {data.sources.length} 段</span>
              </>
            )}
            <span className="st-sep">·</span>
            <span>{data.metrics?.latency_s ?? "—"}s</span>
            <span className="st-sep">·</span>
            <span>¥{data.metrics?.cost_yuan ?? 0}</span>
            {data.messageId && (
              <button
                className="detail-link"
                type="button"
                onClick={() => props.onOpenDetail(data.messageId!)}
                dangerouslySetInnerHTML={{ __html: SVG_GEAR + "详情" }}
              />
            )}
          </div>
          {citeLine}
          {webState}
          {/* W6-S8 修复：参考来源折叠为受控 JSX 元素——放进 innerHTML 字符串会被轮询重渲染重置，
              非受控 details 也会被 React 19 重渲染合上；open 由 state 显式管理才稳定 */}
          <div className="block lead show" dangerouslySetInnerHTML={{ __html: split.main }} />
          {split.count > 0 && (
            <details
              className="ref-fold"
              open={tailOpen}
              onToggle={(e) => {
                const v = (e.target as HTMLDetailsElement).open;
                if (v !== tailOpen) setTailOpen(v);
              }}
            >
              <summary>
                参考来源（{split.count} 条）
                <span className="rf-caret">▶</span>
              </summary>
              <div className="rf-body" dangerouslySetInnerHTML={{ __html: split.tail }} />
            </details>
          )}
          {planHtml}
          {/* L2 证据折叠 */}
          {data.sources.length > 0 && (
            <div
              className={`block retrieval-bar${evOpen ? " open" : ""}`}
              data-evkey={key}
              onClick={toggleEv}
            >
              <div className="rb-top">
                <span className="rb-caret">▶</span>
                <span className="rb-l">
                  已检索到 <b>{data.sources.length}</b> 个相关来源
                </span>
              </div>
              <div className="rb-list">{evRows}</div>
            </div>
          )}
          {/* L3 操作行（图2） */}
          {data.messageId && (
            <div className="ans-actions">
              <button className="fbtn" type="button" onClick={() => props.onCopy(data)} dangerouslySetInnerHTML={{ __html: SVG_COPY + "复制答案" }} />
              <button
                className={`fbtn${data.feedback === "up" ? " on" : ""}`}
                type="button"
                onClick={() => props.onFeedback(data.messageId!, "up")}
                dangerouslySetInnerHTML={{ __html: SVG_THUMB_UP + "有帮助" }}
              />
              <button
                className={`fbtn${data.feedback === "down" ? " on" : ""}`}
                type="button"
                onClick={() => props.onFeedback(data.messageId!, "down")}
                dangerouslySetInnerHTML={{ __html: SVG_THUMB_DOWN + "不满意" }}
              />
              {data.question && (
                <button className="fbtn regen" type="button" onClick={() => props.onRegenerate(data)} dangerouslySetInnerHTML={{ __html: SVG_REGEN + "重新生成" }} />
              )}
            </div>
          )}
          {/* L4 追问 */}
          {!data.followups ? null : data.followups.length ? (
            <div className="followups">
              <div className="followups-title">追问 ↓</div>
              {data.followups.map((f, i) => (
                <button className="followup" type="button" data-q={esc(f)} key={i}>
                  {esc(f)}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
