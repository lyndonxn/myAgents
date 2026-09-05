"use client";

/* 输入区：受控 textarea（Enter 发送/限高自适应）+ ＋上传（S5）/ 语音（S5）/ 圆形发送键（↑/■）。
 * 左侧上下文竖轨按 ctxPct 着色（绿→红）。 */

import { useRef } from "react";

export default function Composer(props: {
  ctxPct: number;
  value: string;
  busy: boolean;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onStop: () => void;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const segs = 24;
  const active = Math.round(segs * props.ctxPct);
  const hue = Math.round(142 * (1 - props.ctxPct));
  const color = `hsl(${hue} 68% 42%)`;

  const autoGrow = () => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 132)}px`;
  };

  return (
    <div className="composer">
      <div className="composer-inner">
        <div className="img-preview" id="imgPreview" hidden>
          <img id="imgThumb" alt="待搜索图片" />
          <button className="img-clear" type="button" title="移除图片" aria-label="移除图片">
            ×
          </button>
        </div>
        <div className="inputrow">
          <textarea
            id="input"
            ref={taRef}
            rows={1}
            placeholder="输入问题，Enter 发送，Shift+Enter 换行"
            value={props.value}
            onChange={(e) => {
              props.onChange(e.target.value);
              autoGrow();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (props.busy) props.onStop();
                else props.onSubmit();
              }
            }}
          />
          <div className="input-actions">
            <button className="iconbtn addbtn" id="imgBtn" title="上传图片（S5 接入）" aria-label="上传图片" type="button" disabled>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
            </button>
            <span className="ia-spacer" />
            <button className="iconbtn" id="micBtn" title="语音输入（S5 接入）" aria-label="语音输入" type="button" disabled>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="9" y="2" width="6" height="12" rx="3" />
                <path d="M5 10a7 7 0 0 0 14 0M12 17v5M8 22h8" />
              </svg>
            </button>
            <input type="file" id="imgFile" accept="image/*" hidden />
            <button
              className={`sendbtn${props.busy ? " stop" : ""}`}
              id="sendBtn"
              aria-label={props.busy ? "停止生成" : "发送消息"}
              title={props.busy ? "停止生成" : "发送消息"}
              type="button"
              disabled={!props.busy && !props.value.trim()}
              onClick={() => (props.busy ? props.onStop() : props.onSubmit())}
            >
              {props.busy ? (
                <svg viewBox="0 0 24 24" aria-hidden="true" style={{ fill: "currentColor", stroke: "none" }}>
                  <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" />
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <line x1="12" y1="19" x2="12" y2="5" />
                  <polyline points="5 12 12 5 19 12" />
                </svg>
              )}
            </button>
          </div>
        </div>
        <div className="composer-note">
          <span>支持截图以图搜库 · 回答均附引用来源</span>
          <span className="webtag">默认不联网；知识库未命中时会先询问是否联网检索</span>
        </div>
      </div>
    </div>
  );
}
