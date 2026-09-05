"use client";

/* 聊天区：INTRO / 用户气泡 / 历史回答 / 流式回答卡（thinking→streaming→done 三相，W3 对齐）。
 * S3 为流式管线与简化卡面（纯文本 + 光标），S4 把 done 相升级为完整答案卡。 */

import { useLayoutEffect, useRef } from "react";
import type { MetricsInfo, PlanInfo, SourceDetail } from "@/lib/api";

export type MsgVM =
  | { kind: "intro" }
  | { kind: "user"; text: string }
  | { kind: "agent"; text: string }
  | AgentStreamMsg;

export interface AgentStreamMsg {
  kind: "agent-stream";
  key: string;
  phase: "thinking" | "streaming" | "done";
  shown: string;
  stageName: string;
  sources: string[];
  sourcesDetail: SourceDetail[];
  plan?: PlanInfo;
  metrics?: MetricsInfo;
  messageId?: string;
  question: string;
  followups?: string[];
}

export default function Chat(props: { messages: MsgVM[]; welcomeDocs?: { docCount: string; chunks: string } }) {
  const msgs = props.messages;
  const showWelcome = msgs.length === 1 && msgs[0].kind === "intro";
  const docs = props.welcomeDocs ?? { docCount: "—", chunks: "—" };
  const scRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // 用户上滚则停止跟随；回到底部恢复自动跟随（W3 行为对齐）
  useLayoutEffect(() => {
    if (pinnedRef.current && scRef.current) scRef.current.scrollTop = scRef.current.scrollHeight;
  });

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
          if (m.kind === "agent-stream") return <AgentStreamCard msg={m} key={m.key} />;
          return (
            <div className="msg agent" key={`a-${i}`}>
              <div className="avatar">M</div>
              <div className="m-main">
                <div className="msg-label">
                  <span>MYAGENTS</span>
                  <span className="role-tag">RAG · PLANNER · TOOLS</span>
                </div>
                <div className="card">
                  <div className="block lead show">{m.text}</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function AgentStreamCard({ msg }: { msg: AgentStreamMsg }) {
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
              <div className="gen-status">
                <span>正在生成答案 · 引用 {msg.sources.length} 段内容</span>
                <i className="gen-line" aria-hidden="true" />
              </div>
              <div className="block lead show">
                {msg.shown}
                <span className="stream-cursor" aria-hidden="true" />
              </div>
            </>
          )}
          {msg.phase === "done" && <div className="block lead show">{msg.shown}</div>}
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
