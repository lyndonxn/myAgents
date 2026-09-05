"use client";

/* 右侧面板（S2 静态 idle；S6 接检索轨迹/来源/指标/任务轮询） */

const IDLE_STEPS = [
  { name: "查询改写", en: "Query Rewrite" },
  { name: "向量检索", en: "Vector Search" },
  { name: "交叉重排", en: "Rerank · Top-5" },
  { name: "组装上下文", en: "Assemble Context" },
  { name: "生成回答", en: "Generate" },
  { name: "引用校验", en: "Citation Check" },
];

export default function TracePanel() {
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
          <div className="fig-label">
            <span className="fig-no">02</span>
            <span>命中来源</span>
          </div>
          <div className="fig-sub">Sources · 按相关度排序</div>
          <div id="srcList">
            <div className="src-empty">
              暂无检索记录。
              <br />
              发起提问后，此处列出命中的知识块与相关度。
            </div>
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
            <div className="v-row"><span className="k">本轮召回</span><span id="vRecall">—</span></div>
            <div className="v-row"><span className="k">本轮耗时</span><span id="vTime">—</span></div>
            <div className="v-row"><span className="k">生成速率</span><span id="vTok">—</span></div>
            <div className="v-row"><span className="k">联网回退</span><span className="green" id="vWeb">待命</span></div>
          </div>
        </div>
        <div className="fig">
          <div className="fig-label">
            <span className="fig-no">04</span>
            <span>后台任务</span>
          </div>
          <div className="fig-sub">Background Tasks · 活跃 2s / 空闲 8s 自动刷新</div>
          <div id="taskList">
            <div className="src-empty">
              暂无后台任务。
              <br />
              后台执行的问题会在此显示状态与步骤进度。
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
