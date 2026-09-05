"use client";

/* 输入区（S2 静态壳：发送/上传/语音行为在 S5 接入；结构 = W5 验收版） */

export default function Composer(props: { ctxPct: number }) {
  const segs = 24;
  const active = Math.round(segs * props.ctxPct);
  const hue = Math.round(142 * (1 - props.ctxPct));
  const color = `hsl(${hue} 68% 42%)`;
  return (
    <div className="composer">
      <div className="composer-inner">
        <div className="ctx-rail" id="ctxLine" title="上下文容量">
          {Array.from({ length: segs }, (_, i) => (
            <span key={i} className="seg" style={{ background: i < active ? color : "var(--line)" }} />
          ))}
        </div>
        <div className="img-preview" id="imgPreview" hidden>
          <img id="imgThumb" alt="待搜索图片" />
          <button className="img-clear" type="button" title="移除图片" aria-label="移除图片">
            ×
          </button>
        </div>
        <div className="inputrow">
          <textarea id="input" rows={1} placeholder="输入问题，Enter 发送，Shift+Enter 换行" disabled />
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
            <button className="sendbtn" id="sendBtn" aria-label="发送消息（S3 接入）" type="button" disabled title="S3 接入" />
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
