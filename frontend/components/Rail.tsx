"use client";

/* 左栏：新建会话 / 搜索过滤 / 会话列表（激活+删除）/ 知识库概览卡 */

import { useState } from "react";
import type { SessionInfo } from "@/lib/api";

export interface KbSummary {
  docCount: string;
  chunkCount: string;
  hitRate: string;
  todayQueries: string;
  syncLabel: string;
}

export default function Rail(props: {
  sessions: SessionInfo[];
  currentSession: string;
  kb: KbSummary;
  onNewSession: () => void;
  onOpenSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onManageKb: () => void;
}) {
  const [filter, setFilter] = useState("");
  const q = filter.trim();
  const list = props.sessions.filter((s) => !q || s.title.includes(q));

  return (
    <aside className="rail">
      <button className="newsession" type="button" onClick={props.onNewSession}>
        <span>＋ 新建会话</span>
        <span className="kbd">⌘K</span>
      </button>
      <div className="rail-search">
        <input
          id="sessionSearch"
          placeholder="搜索对话"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="rs-kbd">⌕</span>
      </div>
      <div className="rail-section">
        <span>最近会话</span>
        <span id="sesCount">{String(list.length).padStart(2, "0")}</span>
      </div>
      <div className="sessions scroll" id="sessionList">
        {list.length === 0 ? (
          <div style={{ padding: "14px 12px", fontFamily: "var(--mono)", fontSize: 10, color: "var(--mute)" }}>
            无匹配会话
          </div>
        ) : (
          list.map((s) => (
            <div
              key={s.id}
              className={`session ${props.currentSession === s.id ? "active" : ""}`}
              onClick={() => props.onOpenSession(s.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter") props.onOpenSession(s.id);
              }}
            >
              <div className="s-title">
                <span className="s-ico">▤</span>
                <span>{s.title}</span>
              </div>
              <div className="s-meta">
                {new Date(s.updated_at).toLocaleDateString("zh-CN")} · {s.message_count ?? 0} 条消息
              </div>
              <button
                className="session-delete"
                title="删除会话"
                aria-label={`删除会话：${s.title}`}
                onClick={(e) => {
                  e.stopPropagation();
                  props.onDeleteSession(s.id);
                }}
              >
                删除
              </button>
            </div>
          ))
        )}
      </div>
      <div className="kb-card">
        <div className="kb-label">
          <span>知识库概览</span>
          <span className="live" id="kbSyncStatus">
            {props.kb.syncLabel}
          </span>
        </div>
        <div className="kb-grid">
          <div className="kb-cell">
            <div className="kc-v">
              <span id="docCount">{props.kb.docCount}</span>
              <span className="u">篇</span>
            </div>
            <div className="kc-k">文档总数</div>
          </div>
          <div className="kb-cell">
            <div className="kc-v">
              <span id="chunkCount">{props.kb.chunkCount}</span>
              <span className="u">块</span>
            </div>
            <div className="kc-k">知识片段</div>
          </div>
          <div className="kb-cell">
            <div className="kc-v">
              <span id="hitRate">{props.kb.hitRate}</span>
              <span className="u">%</span>
            </div>
            <div className="kc-k">今日命中率</div>
          </div>
          <div className="kb-cell">
            <div className="kc-v">
              <span id="todayQueries">{props.kb.todayQueries}</span>
              <span className="u">次</span>
            </div>
            <div className="kc-k">今日检索</div>
          </div>
        </div>
        <div className="kb-manage" onClick={props.onManageKb} role="button" tabIndex={0}>
          <span>管理知识库</span>
          <span>→</span>
        </div>
      </div>
    </aside>
  );
}
