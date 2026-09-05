"use client";

/* 聊天区：INTRO / 用户气泡 / 回答卡（统一 agent-stream 模型，三相 thinking→streaming→done）。
 * done 相由 AnswerCard 呈现（W1–W4 全部能力）；S3 打字机与滚动跟随保留。 */

import { useLayoutEffect, useRef } from "react";
import type { FeedbackValue, MetricsInfo, PlanInfo, SourceDetail } from "@/lib/api";
import { esc, simpleHtml } from "@/lib/markdown";
import AnswerCard, { type AnswerData } from "@/components/AnswerCard";

export interface AgentStreamMsg {
  kind: "stream";
  key: string;
  phase: "thinking" | "streaming" | "done";
  /** 文本载体：thinking 为空、streaming 为已渲染片段、done 为全文 */
  shown: string;
  stageName: string;
  sources: string[];
  sourcesDetail: SourceDetail[];
  plan?: PlanInfo;
  metrics?: MetricsInfo;
  messageId?: string;
  question: string;
  feedback: FeedbackValue;
  followups?: string[];
  respNo?: number;
}

export type MsgVM =
  | { kind: "intro" }
  | { kind: "user"; text: string }
  | { kind: "stream"; data: AgentStreamMsg };

export interface ChatHandlers {
  onCopy: (d: AnswerData) => void;
  onShare: (d: AnswerData) => void;
  onRegenerate: (d: AnswerData) => void;
  onFeedback: (messageId: string, value: Exclude<FeedbackValue, "">) => void;
  onQuickAsk: (text: string) => void;
  onOpenDetail: (messageId: string) => void;
}

export default function Chat(props: {
  messages: MsgVM[];
  welcomeDocs?: { docCount: string; chunks: string };
  handlers: ChatHandlers;
  /** 流式/消息数变化信号：仅此变化才触发跟随滚动（轮询重渲染不再打扰阅读） */
  streamTick?: number;
}) {
  const msgs = props.messages;
  const showWelcome = msgs.length === 1 && msgs[0].kind === "intro";
  const docs = props.welcomeDocs ?? { docCount: "—", chunks: "—" };
  const scRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // W6-S8 修复：仅在消息数/流式更新时跟随滚动——之前每次渲染（含 2s/5s 轮询）都强制回底，
  // 用户翻历史记录时会一直被拽到底部造成上下闪烁
  useLayoutEffect(() => {
    if (pinnedRef.current && scRef.current) scRef.current.scrollTop = scRef.current.scrollHeight;
  }, [props.messages.length, props.streamTick]);

  return (
    <div
      className="chat-scroll scroll"
      id="chatScroll"
      ref={scRef}
      onScroll={() => {
        const sc = scRef.current;
        if (!sc) return;
        pinnedRef.current = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 140;
      }}
    >
      <div className="chat-inner" id="chatInner">
        <div className="date-div">
          <span>{new Date().toLocaleDateString("zh-CN")} · 会话记录</span>
        </div>
        {showWelcome && (
          <div className="welcome">
            <div className="w-top">
              <div className="w-text">
                <div className="w-title">检索你的知识库</div>
                <div className="w-sub">
                  当前工作区已索引{" "}
                  <b>
                    {docs.docCount} 篇文档 · {docs.chunks} 个知识块
                  </b>
                  。直接输入问题，所有回答都会标注命中来源。
                </div>
              </div>
            </div>
            <div className="quick-grid">
              {QUICK.map((a) => (
                <button className="quick-card" type="button" key={a.title}>
                  <div className="qc-t">
                    <span className="qc-ico">{a.icon}</span>
                    <span>{a.title}</span>
                  </div>
                  <div className="qc-d">{a.desc}</div>
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m, i) => {
          if (m.kind === "intro")
            return (
              <div className="msg agent" key={`intro-${i}`}>
                <div className="avatar">M</div>
                <div className="m-main">
                  <div className="msg-label">
                    <span>MYAGENTS</span>
                    <span className="role-tag">RAG · PLANNER · TOOLS</span>
                  </div>
                  <div className="card">
                    <div className="block lead show">
                      你好，我是 MYAGENTS 知识库问答助手（<b>RAG + 规划层 + 工具层</b>）。
                      可以直接询问知识库内的内容，支持连续追问；默认只在知识库内检索，
                      未命中时会先询问是否联网，所有回答均附引用来源。
                    </div>
                  </div>
                </div>
              </div>
            );
          if (m.kind === "user")
            return (
              <div className="msg user" key={`u-${i}`}>
                <div className="avatar">徐</div>
                <div className="m-main">
                  <div className="msg-label">
                    <span className="role-tag">USER</span>
                    <span>我</span>
                  </div>
                  <div className="bubble">{m.text}</div>
                </div>
              </div>
            );
          return <StreamCard msg={m.data} handlers={props.handlers} key={m.data.key} />;
        })}
      </div>
    </div>
  );
}

function StreamCard({ msg, handlers }: { msg: AgentStreamMsg; handlers: ChatHandlers }) {
  if (msg.phase === "done") {
    const data: AnswerData = {
      respNo: msg.respNo ?? 1,
      question: msg.question,
      answer: msg.shown,
      messageId: msg.messageId,
      sources: msg.sources,
      sourcesDetail: msg.sourcesDetail,
      plan: msg.plan,
      metrics: msg.metrics,
      feedback: msg.feedback,
      followups: msg.followups,
    };
    return <AnswerCard data={data} onCopy={handlers.onCopy} onShare={handlers.onShare} onRegenerate={handlers.onRegenerate} onFeedback={handlers.onFeedback} onQuickAsk={handlers.onQuickAsk} onOpenDetail={handlers.onOpenDetail} />;
  }
  return (
    <div className="msg agent">
      <div className="avatar">M</div>
      <div className="m-main">
        <div className="msg-label">
          <span>MYAGENTS</span>
          <span className="role-tag">RAG · PLANNER · TOOLS</span>
        </div>
        <div className="card">
          {msg.phase === "thinking" && (
            <div className="retrieving">
              <span className="spinner" />
              <span>正在{msg.stageName || "规划与检索"}</span>
              <div className="bar" />
            </div>
          )}
          {msg.phase === "streaming" && (
            <>
              {/* W7（图3）：转圈圈 + 状态文字 + 工具 chip（无扫描条）+ 流式正文 */}
              <div className="retrieving">
                <span className="spinner" />
                <span>正在生成答案 · 引用 {msg.sources.length} 段内容</span>
                {msg.plan?.steps?.find((st) => st.action) && (
                  <span className="st-tool">{esc(msg.plan.steps.find((st) => st.action)!.action)}</span>
                )}
              </div>
              <div
                className="block lead show"
                dangerouslySetInnerHTML={{
                  __html: simpleHtml(msg.shown) + '<span class="stream-cursor" aria-hidden="true"></span>',
                }}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

const QUICK = [
  { icon: "◈", title: "随机抽取面试题", desc: "从 50 道高频题中随机抽一道" },
  { icon: "⟳", title: "RAG 完整流程", desc: "离线建库与在线检索全链路" },
  { icon: "⇅", title: "重排的必要性", desc: "召回与精排的分工逻辑" },
  { icon: "⇗", title: "联网检索示例", desc: "知识库未命中时自动联网" },
];
