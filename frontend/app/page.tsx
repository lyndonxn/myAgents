/* W6-S1 静态壳：三栏 chrome 全量移植（类名与 legacy 一致，样式来自 globals.css）。
 * 后续切片逐块接入行为：S2 会话/工作区 → S3 流式聊天 → S4 答案卡 → S5 composer → S6 面板 → S7 设置。 */

const IDLE_STEPS = [
  { name: "查询改写", en: "Query Rewrite" },
  { name: "向量检索", en: "Vector Search" },
  { name: "交叉重排", en: "Rerank · Top-5" },
  { name: "组装上下文", en: "Assemble Context" },
  { name: "生成回答", en: "Generate" },
  { name: "引用校验", en: "Citation Check" },
];

const QUICK_ACTIONS = [
  { icon: "◈", title: "随机抽取面试题", desc: "从 50 道高频题中随机抽一道" },
  { icon: "⟳", title: "RAG 完整流程", desc: "离线建库与在线检索全链路" },
  { icon: "⇅", title: "重排的必要性", desc: "召回与精排的分工逻辑" },
  { icon: "⇗", title: "联网检索示例", desc: "知识库未命中时自动联网" },
];

export default function Home() {
  return (
    <div className="app">
      {/* ======== TOP BAR ======== */}
      <header className="topbar">
        <div className="brand">
          <span className="logo">M</span>
          <span className="wordmark">MYAGENTS</span>
          <span className="plan">TEAM</span>
        </div>
        <div className="topbar-right">
          <div className="ws-switch">
            <span className="ws-ic" />
            <select id="workspaceSelect" aria-label="工作区" defaultValue="服务未启动">
              <option>服务未启动</option>
            </select>
          </div>
          <div className="stat">
            <span className="dot" id="statusDot" />
            <span className="k">索引</span>
            <span id="indexStatus">连接中</span>
          </div>
          <div className="stat">
            <span className="k">记忆</span>
            <span id="memCount">0</span> 轮
          </div>
          <button className="topbtn" type="button">重置会话</button>
          <button className="topbtn" type="button">设置</button>
          <button className="topbtn" id="themeBtn" title="切换主题" type="button">◐</button>
          <span className="account">徐</span>
        </div>
      </header>

      {/* ======== LEFT RAIL ======== */}
      <aside className="rail">
        <button className="newsession" type="button">
          <span>＋ 新建会话</span>
          <span className="kbd">⌘K</span>
        </button>
        <div className="rail-search">
          <input id="sessionSearch" placeholder="搜索对话" />
          <span className="rs-kbd">⌕</span>
        </div>
        <div className="rail-section">
          <span>最近会话</span>
          <span id="sesCount">00</span>
        </div>
        <div className="sessions scroll" id="sessionList" />
        <div className="kb-card">
          <div className="kb-label">
            <span>知识库概览</span>
            <span className="live" id="kbSyncStatus">连接中</span>
          </div>
          <div className="kb-grid">
            <div className="kb-cell"><div className="kc-v"><span id="docCount">—</span><span className="u">篇</span></div><div className="kc-k">文档总数</div></div>
            <div className="kb-cell"><div className="kc-v"><span id="chunkCount">—</span><span className="u">块</span></div><div className="kc-k">知识片段</div></div>
            <div className="kb-cell"><div className="kc-v"><span id="hitRate">—</span><span className="u">%</span></div><div className="kc-k">今日命中率</div></div>
            <div className="kb-cell"><div className="kc-v"><span id="todayQueries">—</span><span className="u">次</span></div><div className="kc-k">今日检索</div></div>
          </div>
          <div className="kb-manage"><span>管理知识库</span><span>→</span></div>
        </div>
      </aside>

      {/* ======== CHAT ======== */}
      <main className="chat">
        <div className="ctx-rail" id="ctxLine" title="上下文容量" />
        <div className="chat-scroll scroll" id="chatScroll">
          <div className="chat-inner" id="chatInner">
            <div className="date-div">
              <span>{new Date().toLocaleDateString("zh-CN")} · 会话记录</span>
            </div>
            <div className="welcome">
              <div className="w-top">
                <div className="w-text">
                  <div className="w-title">检索你的知识库</div>
                  <div className="w-sub">
                    当前工作区已索引 <b>— 篇文档 · — 个知识块</b>。直接输入问题，所有回答都会标注命中来源。
                  </div>
                </div>
              </div>
              <div className="quick-grid">
                {QUICK_ACTIONS.map((a) => (
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
            <div className="msg agent">
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
          </div>
        </div>
        <div className="composer">
          <div className="composer-inner">
            <div className="img-preview" id="imgPreview" hidden>
              <img id="imgThumb" alt="待搜索图片" />
              <button className="img-clear" type="button" title="移除图片" aria-label="移除图片">×</button>
            </div>
            <div className="inputrow">
              <textarea id="input" rows={1} placeholder="输入问题，Enter 发送，Shift+Enter 换行" />
              <div className="input-actions">
                <button className="iconbtn addbtn" id="imgBtn" title="上传图片（也可直接拖入）" aria-label="上传图片" type="button">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                </button>
                <span className="ia-spacer" />
                <button className="iconbtn" id="micBtn" title="语音输入" aria-label="语音输入" type="button">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10a7 7 0 0 0 14 0M12 17v5M8 22h8" /></svg>
                </button>
                <input type="file" id="imgFile" accept="image/*" hidden />
                <button className="sendbtn" id="sendBtn" aria-label="发送消息" type="button" disabled />
              </div>
            </div>
            <div className="composer-note">
              <span>支持截图以图搜库 · 回答均附引用来源</span>
              <span className="webtag">默认不联网；知识库未命中时会先询问是否联网检索</span>
            </div>
          </div>
        </div>
      </main>

      {/* ======== TRACE PANEL ======== */}
      <aside className="trace scroll">
        <div className="trace-inner">
          <div className="fig">
            <div className="fig-label"><span className="fig-no">01</span><span>检索轨迹</span></div>
            <div className="fig-sub">Retrieval Trace · 实时</div>
            <div className="steps" id="steps">
              {IDLE_STEPS.map((s, i) => (
                <div className="step idle" key={s.en}>
                  <div className="st-ic">0{i + 1}</div>
                  <div className="st-body">
                    <div className="st-name">{s.name}</div>
                    <div className="st-en">{s.en}</div>
                  </div>
                  <div className="st-time">—</div>
                </div>
              ))}
            </div>
          </div>
          <div className="fig">
            <div className="fig-label"><span className="fig-no">02</span><span>命中来源</span></div>
            <div className="fig-sub">Sources · 按相关度排序</div>
            <div id="srcList">
              <div className="src-empty">暂无检索记录。<br />发起提问后，此处列出命中的知识块与相关度。</div>
            </div>
          </div>
          <div className="fig">
            <div className="fig-label"><span className="fig-no">03</span><span>运行指标</span></div>
            <div className="fig-sub">Index Vitals · 本轮</div>
            <div className="vitals">
              <div className="v-row"><span className="k">向量模型</span><span>bge-local · 512d</span></div>
              <div className="v-row"><span className="k">重排模型</span><span>cross-encoder</span></div>
              <div className="v-row"><span className="k">本轮召回</span><span id="vRecall">—</span></div>
              <div className="v-row"><span className="k">本轮耗时</span><span id="vTime">—</span></div>
              <div className="v-row"><span className="k">生成速率</span><span id="vTok">—</span></div>
              <div className="v-row"><span className="k">联网回退</span><span className="green" id="vWeb">待命</span></div>
            </div>
          </div>
          <div className="fig">
            <div className="fig-label"><span className="fig-no">04</span><span>后台任务</span></div>
            <div className="fig-sub">Background Tasks · 活跃 2s / 空闲 8s 自动刷新</div>
            <div id="taskList">
              <div className="src-empty">暂无后台任务。<br />后台执行的问题会在此显示状态与步骤进度。</div>
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
}
