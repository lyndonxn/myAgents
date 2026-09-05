"use client";

/* 输入区（W6-S5）：受控 textarea + ＋上传/外部拖入图片（52px 预览+右上角关闭）/ 语音输入 /
 * 圆形发送键（↑/■）。左侧上下文竖轨按 ctxPct 着色（绿→红）。 */

import { useEffect, useRef, useState } from "react";

export interface PendingImage {
  name: string;
  dataUrl: string;
}

export default function Composer(props: {
  ctxPct: number;
  ctxK?: { used: number; max: number };
  value: string;
  busy: boolean;
  pendingImage: PendingImage | null;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  onPickImage: (file: File) => void;
  onClearImage: () => void;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [recording, setRecording] = useState(false);
  const recognitionRef = useRef<{ stop: () => void } | null>(null);

  const SpeechRec =
    typeof window !== "undefined"
      ? (window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown })
          .SpeechRecognition ?? (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition
      : undefined;
  const voiceAvailable = Boolean(SpeechRec);

  const autoGrow = () => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 132)}px`;
  };

  useEffect(() => {
    autoGrow();
  }, [props.value]);

  /* W4/W5 行为平移：外部拖入图片（dragDepth 防抖 + composer 高亮） */
  const depthRef = useRef(0);
  useEffect(() => {
    const composerEl = document.querySelector(".composer");
    const over = (e: DragEvent) => e.preventDefault();
    const enter = () => {
      depthRef.current += 1;
      composerEl?.classList.add("dragover");
    };
    const leave = () => {
      depthRef.current = Math.max(0, depthRef.current - 1);
      if (!depthRef.current) composerEl?.classList.remove("dragover");
    };
    const drop = (e: DragEvent) => {
      e.preventDefault();
      depthRef.current = 0;
      composerEl?.classList.remove("dragover");
      if (props.busy) return;
      const file = Array.from(e.dataTransfer?.files || []).find((f) => f.type.startsWith("image/"));
      if (file) props.onPickImage(file);
    };
    window.addEventListener("dragover", over);
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [props]);

  /* 语音输入（webkitSpeechRecognition，interim 结果直接替换输入框，legacy 对齐） */
  const toggleVoice = () => {
    if (recording) {
      recognitionRef.current?.stop();
      return;
    }
    if (!SpeechRec) return;
    type RecCtor = new () => {
      lang: string;
      interimResults: boolean;
      onresult: (e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void;
      onend: () => void;
      start: () => void;
      stop: () => void;
    };
    const recognition = new (SpeechRec as RecCtor)();
    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.onresult = (event) => {
      const text = Array.from(event.results)
        .map((r) => r[0].transcript)
        .join("");
      props.onChange(text);
    };
    recognition.onend = () => setRecording(false);
    recognitionRef.current = recognition;
    setRecording(true);
    recognition.start();
  };

  const segs = 24;
  const active = Math.round(segs * props.ctxPct);
  const hue = Math.round(142 * (1 - props.ctxPct));
  const color = `hsl(${hue} 68% 42%)`;
  const canSend = props.busy || !!props.value.trim() || !!props.pendingImage;

  return (
    <div className="composer">
      {/* W6-S8 补回：上下文竖轨（S5 重写时遗失）——绝对定位锚定 .chat 左缘 */}
      <div
        className="ctx-rail"
        id="ctxLine"
        title={`上下文 ${((props.ctxK?.used ?? 0) / 1000).toFixed(1)}K / ${((props.ctxK?.max ?? 128000) / 1000).toFixed(0)}K（${Math.round(props.ctxPct * 100)}%）`}
      >
        {Array.from({ length: segs }, (_, i) => (
          <span key={i} className="seg" style={{ background: i < active ? color : "var(--line)" }} />
        ))}
      </div>
      <div className="composer-inner">
        <div className="img-preview" id="imgPreview" hidden={!props.pendingImage}>
          <img id="imgThumb" alt="待搜索图片" src={props.pendingImage?.dataUrl} />
          <button
            className="img-clear"
            type="button"
            title="移除图片"
            aria-label="移除图片"
            onClick={props.onClearImage}
          >
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
            <button
              className="iconbtn addbtn"
              id="imgBtn"
              title="上传图片（也可直接拖入）"
              aria-label="上传图片"
              type="button"
              disabled={props.busy}
              onClick={() => fileRef.current?.click()}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
            </button>
            <input
              type="file"
              id="imgFile"
              ref={fileRef}
              accept="image/*"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) props.onPickImage(file);
                e.target.value = "";
              }}
            />
            <span className="ia-spacer" />
            <button
              className={`iconbtn${recording ? " recording" : ""}`}
              id="micBtn"
              title={voiceAvailable ? (recording ? "停止录音" : "语音输入") : "当前浏览器不支持语音识别"}
              aria-label="语音输入"
              type="button"
              disabled={!voiceAvailable || props.busy}
              onClick={toggleVoice}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="9" y="2" width="6" height="12" rx="3" />
                <path d="M5 10a7 7 0 0 0 14 0M12 17v5M8 22h8" />
              </svg>
            </button>
            <button
              className={`sendbtn${props.busy ? " stop" : ""}`}
              id="sendBtn"
              aria-label={props.busy ? "停止生成" : "发送消息"}
              title={props.busy ? "停止生成" : "发送消息"}
              type="button"
              disabled={!canSend}
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
