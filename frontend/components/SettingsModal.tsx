"use client";

/* W6-S7：设置弹窗（六页：模型/检索/知识库/工作区/记忆/日志）。
 * 字段与保存 payload 与 legacy 完全一致；打开时拉取 /api/config 与 /api/kb。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { esc } from "@/lib/markdown";
import {
  api,
  clearLogs,
  clearMemoryAll,
  createWorkspace,
  deleteEpisode,
  getConfig,
  loadLogs,
  loadMemory,
  loadMemoryEntities,
  rebuildKb,
  saveConfig,
  type ConfigSavePayload,
  type MemoryEpisode,
} from "@/lib/api";

export type SettingsTab = "model" | "retrieval" | "kb" | "workspace" | "memory" | "logs";

const TABS: { id: SettingsTab; label: string }[] = [
  { id: "model", label: "模型" },
  { id: "retrieval", label: "检索" },
  { id: "kb", label: "知识库" },
  { id: "workspace", label: "工作区" },
  { id: "memory", label: "记忆" },
  { id: "logs", label: "日志" },
];

const RETRIEVAL_PRESETS: Record<string, { topK: number; rerank: string; candidates: number; multi: boolean }> = {
  balanced: { topK: 6, rerank: "auto", candidates: 24, multi: false },
  fast: { topK: 4, rerank: "off", candidates: 12, multi: false },
  deep: { topK: 8, rerank: "auto", candidates: 32, multi: true },
};

const LOG_LINES = [100, 300, 1000];
const LOG_LEVELS = ["", "ERROR", "WARNING", "INFO", "DEBUG"];

export default function SettingsModal(props: {
  open: boolean;
  initialTab: SettingsTab;
  requireModelNotice: boolean;
  llmConfigured: boolean;
  onClose: () => void;
  onToast: (msg: string, error?: boolean) => void;
  onSaved: () => void;
  onWorkspaceCreated: (id: string) => void;
}) {
  const [tab, setTab] = useState<SettingsTab>(props.initialTab);
  const [saving, setSaving] = useState(false);
  /* 模型 */
  const [provider, setProvider] = useState("deepseek");
  const [mode, setMode] = useState("cloud");
  const [apiKey, setApiKey] = useState("");
  const [keyPlaceholder, setKeyPlaceholder] = useState("sk-...");
  const [baseUrl, setBaseUrl] = useState("");
  const [chatModel, setChatModel] = useState("");
  const [temperature, setTemperature] = useState("0.3");
  const [maxTokens, setMaxTokens] = useState("2048");
  const [visionKey, setVisionKey] = useState("");
  const [visionKeyPlaceholder, setVisionKeyPlaceholder] = useState("留空则使用主 Key");
  const [visionBase, setVisionBase] = useState("");
  const [visionModel, setVisionModel] = useState("");
  /* 检索 */
  const [preset, setPreset] = useState("balanced");
  const [topK, setTopK] = useState("6");
  const [rerank, setRerank] = useState("auto");
  const [candidates, setCandidates] = useState("24");
  const [multi, setMulti] = useState("false");
  const [rerankerModel, setRerankerModel] = useState("");
  const [kbFallbackWeb, setKbFallbackWeb] = useState("false");
  /* 知识库 */
  const [kbPath, setKbPath] = useState("");
  const [kbStatus, setKbStatus] = useState("知识库状态");
  /* 工作区 */
  const [wsName, setWsName] = useState("");
  const [wsKbPath, setWsKbPath] = useState("");
  /* 记忆 */
  const [memQuery, setMemQuery] = useState("");
  const [memLong, setMemLong] = useState("false");
  const [memEntities, setMemEntities] = useState("false");
  const [memStatsText, setMemStatsText] = useState("加载中…");
  const [episodes, setEpisodes] = useState<MemoryEpisode[]>([]);
  const [entities, setEntities] = useState<[string, { facts?: string[]; last_seen?: string }][]>([]);
  /* 日志 */
  const [logLines, setLogLines] = useState(300);
  const [logLevel, setLogLevel] = useState("");
  const [logQuery, setLogQuery] = useState("");
  const [logAuto, setLogAuto] = useState(true);
  const [logText, setLogText] = useState("加载中…");
  const logsRef = useRef<string[]>([]);
  const logTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const showToast = props.onToast;

  /* ----- 打开时拉取配置/知识库 ----- */
  useEffect(() => {
    if (!props.open) return;
    setTab(props.initialTab);
    (async () => {
      try {
        const cfg = await getConfig();
        setKeyPlaceholder(cfg.llm.api_key_masked || "sk-...");
        setBaseUrl(cfg.llm.base_url || "");
        setChatModel(cfg.llm.chat_model || "");
        setMode(cfg.llm.mode || "cloud");
        setProvider((cfg.llm.base_url || "").includes("api.deepseek.com") ? "deepseek" : "custom");
        setTemperature(String(cfg.llm.temperature));
        setMaxTokens(String(cfg.llm.max_tokens));
        setTopK(String(cfg.retrieval.top_k));
        setRerank(cfg.retrieval.rerank);
        setCandidates(String(cfg.retrieval.rerank_candidates));
        setMulti(String(cfg.retrieval.multi_query));
        setRerankerModel(cfg.retrieval.reranker_model || "");
        setKbFallbackWeb(String(!!(cfg.tools || {}).kb_fallback_web));
        setMemLong(String(!!(cfg.memory || {}).long_term_enabled));
        setMemEntities(String(!!(cfg.memory || {}).entities_enabled));
        setVisionKeyPlaceholder(cfg.vision.api_key_masked || "留空则使用主 Key");
        setVisionBase(cfg.vision.base_url || "");
        setVisionModel(cfg.vision.model || "");
        detectPreset(cfg.retrieval.top_k, cfg.retrieval.rerank, cfg.retrieval.rerank_candidates, cfg.retrieval.multi_query);
      } catch (e) {
        showToast((e as Error).message || "配置读取失败", true);
      }
      try {
        const kb = await api.kb();
        setKbPath(kb.path || "");
        setKbStatus(`${kb.exists ? "目录有效" : "目录不存在"} · ${kb.md_count} 个 Markdown 文件${kb.building ? " · 正在重建" : ""}`);
      } catch (_e) {
        setKbStatus("读取失败");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.open, props.initialTab]);

  /* ----- 记忆页 ----- */
  const refreshMemory = useCallback(async (q: string) => {
    try {
      const [data, ent] = await Promise.all([loadMemory(q), loadMemoryEntities()]);
      if (data.disabled) setMemStatsText("长期记忆未开启（可在「检索」页打开开关）：成功问答不会写入经验。");
      else
        setMemStatsText(
          `共 ${data.total ?? data.episodes.length} 条长期记忆${q ? `（匹配「${q}」）` : ""}，显示最近 ${data.episodes.length} 条。`,
        );
      setEpisodes(data.episodes || []);
      setEntities(Object.entries(ent.entities || {}));
    } catch (e) {
      setMemStatsText(`读取失败：${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    if (props.open && tab === "memory") void refreshMemory(memQuery);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.open, tab]);

  /* ----- 日志页 ----- */
  const refreshLogs = useCallback(async () => {
    try {
      const data = await loadLogs(logLines);
      const all = data.error ? [] : data.logs || [];
      const filtered = all.filter(
        (line) => (!logLevel || line.includes(`| ${logLevel}`)) && (!logQuery || line.toLowerCase().includes(logQuery.toLowerCase())),
      );
      logsRef.current = filtered;
      setLogText(data.error ? `读取失败：${data.error}` : filtered.join("\n") || "暂无匹配日志");
    } catch (e) {
      setLogText(`读取失败：${(e as Error).message}`);
    }
  }, [logLines, logLevel, logQuery]);

  useEffect(() => {
    if (!props.open || tab !== "logs") return;
    void refreshLogs();
    if (logTimerRef.current) clearInterval(logTimerRef.current);
    if (logAuto) logTimerRef.current = setInterval(() => void refreshLogs(), 4000);
    return () => {
      if (logTimerRef.current) clearInterval(logTimerRef.current);
      logTimerRef.current = null;
    };
  }, [props.open, tab, logAuto, refreshLogs]);

  /* ----- 动作 ----- */
  const applyProviderDefaults = (p: string) => {
    if (p !== "deepseek") return;
    setBaseUrl("https://api.deepseek.com");
    setChatModel("deepseek-chat");
    setTemperature("0.3");
    setMaxTokens("2048");
  };
  const detectPreset = (tk: number, rr: string, cd: number, mu: boolean) => {
    const match = Object.entries(RETRIEVAL_PRESETS).find(
      ([, p]) => p.topK === tk && p.rerank === rr && p.candidates === cd && p.multi === mu,
    );
    setPreset(match?.[0] || "custom");
  };
  const applyPreset = (p: string) => {
    const conf = RETRIEVAL_PRESETS[p];
    if (!conf) return;
    setTopK(String(conf.topK));
    setRerank(conf.rerank);
    setCandidates(String(conf.candidates));
    setMulti(String(conf.multi));
  };

  const save = async () => {
    if (!props.llmConfigured && !apiKey && mode !== "local") {
      showToast("请填写模型 API Key", true);
      return;
    }
    const payload: ConfigSavePayload = {
      llm: {
        api_key: apiKey,
        base_url: baseUrl.trim(),
        chat_model: chatModel.trim(),
        mode,
        temperature: Number(temperature),
        max_tokens: Number(maxTokens),
      },
      retrieval: {
        top_k: Number(topK),
        rerank,
        rerank_candidates: Number(candidates),
        multi_query: multi === "true",
        reranker_model: rerankerModel.trim(),
      },
      vision: { api_key: visionKey, base_url: visionBase.trim(), model: visionModel.trim() },
      tools: { kb_fallback_web: kbFallbackWeb === "true" },
      memory: { long_term_enabled: memLong === "true", entities_enabled: memEntities === "true" },
    };
    setSaving(true);
    try {
      await saveConfig(payload);
      showToast("模型配置已保存，可以开始提问");
      setApiKey("");
      setVisionKey("");
      props.onSaved();
      props.onClose();
    } catch (e) {
      showToast((e as Error).message || "保存失败", true);
    } finally {
      setSaving(false);
    }
  };

  const doRebuildKb = async () => {
    if (!kbPath.trim()) {
      showToast("请填写知识库目录", true);
      return;
    }
    try {
      await rebuildKb(kbPath.trim());
      showToast("知识库开始重建");
      setKbStatus("正在重建索引…");
      const poll = setInterval(async () => {
        try {
          const kb = await api.kb();
          if (!kb.building) {
            clearInterval(poll);
            setKbStatus(kb.build_error ? `重建失败：${kb.build_error}` : `目录有效 · ${kb.md_count} 个 Markdown 文件`);
            showToast(kb.build_error ? "知识库重建失败" : "知识库重建完成", Boolean(kb.build_error));
            props.onSaved();
          }
        } catch (_e) {
          clearInterval(poll);
        }
      }, 2000);
    } catch (e) {
      showToast((e as Error).message || "重建失败", true);
    }
  };

  const doCreateWorkspace = async () => {
    const name = wsName.trim();
    const path = wsKbPath.trim();
    if (!name || !path) {
      showToast("请填写工作区名称与知识库目录", true);
      return;
    }
    try {
      const d = await createWorkspace(name, path);
      showToast("工作区已创建并切换");
      setWsName("");
      setWsKbPath("");
      props.onWorkspaceCreated(d.workspace_id);
      props.onClose();
    } catch (e) {
      showToast((e as Error).message || "创建失败", true);
    }
  };

  const doDeleteEpisode = async (id: string) => {
    if (!confirm("确定删除这条长期记忆吗？此操作不可恢复。")) return;
    try {
      await deleteEpisode(id);
      showToast("记忆已删除");
      void refreshMemory(memQuery);
    } catch (_e) {
      showToast("删除失败", true);
    }
  };
  const doClearMemory = async () => {
    if (!confirm("确定清空全部长期记忆与实体记忆吗？此操作不可恢复。")) return;
    try {
      await clearMemoryAll();
      showToast("记忆已清空");
      void refreshMemory(memQuery);
    } catch (_e) {
      showToast("清空失败", true);
    }
  };
  const downloadLogs = () => {
    const blob = new Blob([logsRef.current.join("\n")], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `myagents-${new Date().toISOString().slice(0, 10)}.log`;
    link.click();
    URL.revokeObjectURL(link.href);
  };
  const doClearLogs = async () => {
    if (!confirm("确定清空当前日志吗？此操作不可恢复。")) return;
    try {
      await clearLogs();
      showToast("日志已清空");
      void refreshLogs();
    } catch (_e) {
      showToast("清空失败", true);
    }
  };

  if (!props.open) return null;

  return (
    <div className="modal show" id="settingsModal" role="dialog" aria-modal="true" aria-labelledby="settingsTitle"
      onClick={(e) => { if (e.target === e.currentTarget) props.onClose(); }}>
      <div className="modal-panel">
        <div className="modal-head">
          <h2 id="settingsTitle">系统设置</h2>
          <button className="modal-close" onClick={props.onClose} aria-label="关闭" type="button">×</button>
        </div>
        <div className="settings-tabs">
          {TABS.map((t) => (
            <button key={t.id} type="button" className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        {tab === "model" && (
          <div className="settings-grid settings-page active">
            {!props.llmConfigured || props.requireModelNotice ? (
              <div className="setup-notice">
                <b>首次使用需要配置模型</b>
                <br />
                填写 API Key 后保存即可开始问答。密钥仅保存在本机运行目录，不会显示在页面或提交到 Git。
              </div>
            ) : null}
            <div className="field">
              <label>模型服务</label>
              <select value={provider} onChange={(e) => { setProvider(e.target.value); applyProviderDefaults(e.target.value); }}>
                <option value="deepseek">DeepSeek（推荐）</option>
                <option value="custom">自定义兼容接口</option>
              </select>
            </div>
            <div className="field">
              <label>运行模式</label>
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="cloud">云端 API（默认）</option>
                <option value="local">本地离线（Ollama 等）</option>
              </select>
            </div>
            <div className="field">
              <label>API KEY（留空保持现有值）</label>
              <input id="cfgKey" type="password" placeholder={keyPlaceholder} value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
            </div>
            <div className="settings-hint">使用 DeepSeek 时只需填写 API Key，其余参数已采用推荐默认值。</div>
            <div className="settings-hint">离线模式：使用本地模型端点，数据不出本机；API Key 可留空，联网搜索将被禁用。</div>
            <details className="advanced-settings">
              <summary>高级模型设置</summary>
              <div className="advanced-grid">
                <div className="field"><label>API 地址</label><input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></div>
                <div className="field"><label>模型</label><input value={chatModel} onChange={(e) => setChatModel(e.target.value)} /></div>
                <div className="field"><label>温度</label><input type="number" min={0} max={2} step={0.1} value={temperature} onChange={(e) => setTemperature(e.target.value)} /></div>
                <div className="field"><label>最大输出 Token</label><input type="number" min={128} step={128} value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} /></div>
                <div className="field wide"><label>视觉模型 API KEY（可选）</label><input type="password" placeholder={visionKeyPlaceholder} value={visionKey} onChange={(e) => setVisionKey(e.target.value)} /></div>
                <div className="field"><label>视觉模型 API 地址</label><input value={visionBase} onChange={(e) => setVisionBase(e.target.value)} /></div>
                <div className="field"><label>视觉模型</label><input value={visionModel} onChange={(e) => setVisionModel(e.target.value)} /></div>
              </div>
            </details>
          </div>
        )}

        {tab === "retrieval" && (
          <div className="settings-grid settings-page active">
            <div className="field wide">
              <label>检索模式</label>
              <select value={preset} onChange={(e) => { setPreset(e.target.value); applyPreset(e.target.value); }}>
                <option value="balanced">均衡（推荐）</option>
                <option value="fast">快速</option>
                <option value="deep">深度</option>
                <option value="custom">自定义</option>
              </select>
            </div>
            <div className="settings-hint">均衡模式适合大多数知识库；快速模式响应更快；深度模式召回更多内容。</div>
            <div className="settings-hint">
              数据外发与记忆（默认关闭）：联网降级开启后问题可能发送到互联网；记忆数据保存到本机 data/。
              开启对应开关即恢复自动降级与跨会话记忆。
            </div>
            <div className="field">
              <label>联网降级（KB 未命中时自动搜索）</label>
              <select value={kbFallbackWeb} onChange={(e) => setKbFallbackWeb(e.target.value)}>
                <option value="false">关闭</option>
                <option value="true">开启</option>
              </select>
            </div>
            <details className="advanced-settings">
              <summary>高级检索设置</summary>
              <div className="advanced-grid">
                <div className="field"><label>召回数量</label><input type="number" min={1} max={15} value={topK} onChange={(e) => { setTopK(e.target.value); setPreset("custom"); }} /></div>
                <div className="field">
                  <label>重排方式</label>
                  <select value={rerank} onChange={(e) => { setRerank(e.target.value); setPreset("custom"); }}>
                    <option value="auto">自动</option>
                    <option value="cross_encoder">本地精排</option>
                    <option value="llm">模型精排</option>
                    <option value="off">关闭</option>
                  </select>
                </div>
                <div className="field"><label>重排候选数</label><input type="number" min={6} max={60} value={candidates} onChange={(e) => { setCandidates(e.target.value); setPreset("custom"); }} /></div>
                <div className="field">
                  <label>多查询扩展</label>
                  <select value={multi} onChange={(e) => { setMulti(e.target.value); setPreset("custom"); }}>
                    <option value="false">关闭</option>
                    <option value="true">开启</option>
                  </select>
                </div>
                <div className="field wide"><label>重排模型路径或名称</label><input value={rerankerModel} onChange={(e) => setRerankerModel(e.target.value)} /></div>
              </div>
            </details>
            <div className="field wide">
              <label>长期记忆</label>
              <select value={memLong} onChange={(e) => setMemLong(e.target.value)}>
                <option value="false">关闭</option>
                <option value="true">开启</option>
              </select>
            </div>
            <div className="field wide">
              <label>实体记忆</label>
              <select value={memEntities} onChange={(e) => setMemEntities(e.target.value)}>
                <option value="false">关闭</option>
                <option value="true">开启</option>
              </select>
            </div>
          </div>
        )}

        {tab === "kb" && (
          <div className="settings-grid settings-page active">
            <div className="field wide"><label>知识库目录</label><input value={kbPath} onChange={(e) => setKbPath(e.target.value)} /></div>
            <div className="field wide"><label>{kbStatus}</label></div>
            <div className="field wide"><button className="actionbtn" type="button" onClick={doRebuildKb}>保存目录并重建索引</button></div>
          </div>
        )}

        {tab === "workspace" && (
          <div className="settings-grid settings-page active">
            <div className="field"><label>工作区名称</label><input placeholder="例如：产品文档" value={wsName} onChange={(e) => setWsName(e.target.value)} /></div>
            <div className="field"><label>知识库目录</label><input placeholder="/path/to/markdown" value={wsKbPath} onChange={(e) => setWsKbPath(e.target.value)} /></div>
            <div className="field wide"><button className="actionbtn" type="button" onClick={doCreateWorkspace}>创建工作区</button></div>
          </div>
        )}

        {tab === "memory" && (
          <div className="settings-page active">
            <div className="settings-hint">此处可查看被记住的内容，并逐条撤回或全部清除。</div>
            <div className="log-toolbar">
              <input style={{ flex: 1 }} placeholder="搜索记忆内容" value={memQuery} onChange={(e) => setMemQuery(e.target.value)} />
              <button className="actionbtn" type="button" onClick={() => void refreshMemory(memQuery)}>刷新</button>
              <button className="actionbtn" type="button" onClick={doClearMemory}>清空全部</button>
            </div>
            <div className="settings-hint" id="memStats">{memStatsText}</div>
            <div id="memList">
              {episodes.length === 0 ? (
                <div className="settings-hint">暂无记忆条目。</div>
              ) : (
                episodes.map((e) => (
                  <div className="mem-item" key={e.id}>
                    <div className="mem-q">
                      {esc(e.question)}
                      <button className="actionbtn mem-del" type="button" onClick={() => void doDeleteEpisode(e.id)}>删除</button>
                    </div>
                    <div className="mem-a">{esc(e.answer_summary)}</div>
                    <div className="mem-meta">
                      会话 {esc(String(e.session_id || "—").slice(0, 8))} · {esc(String(e.ts || "").slice(0, 19)).replace("T", " ")} · 命中 {e.hits ?? 0} 次
                      {(e.sources || []).length ? ` · 来源 ${e.sources.length} 个` : ""}
                    </div>
                  </div>
                ))
              )}
            </div>
            {entities.length > 0 && (
              <>
                <div className="settings-hint" id="memEntitiesTitle"><b>实体事实</b></div>
                <div id="memEntities">
                  {entities.map(([name, entry]) => {
                    const facts = entry.facts || [];
                    return (
                      <div className="mem-item" key={name}>
                        <div className="mem-q">{esc(name)}</div>
                        <div className="mem-a">{esc(facts[facts.length - 1] || "")}</div>
                        <div className="mem-meta">最近提及 {esc(String(entry.last_seen || "").slice(0, 19)).replace("T", " ")}</div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        )}

        {tab === "logs" && (
          <div className="settings-page active">
            <div className="log-toolbar">
              <select value={logLines} onChange={(e) => setLogLines(Number(e.target.value))}>
                {LOG_LINES.map((n) => (
                  <option key={n} value={n}>最近 {n} 行</option>
                ))}
              </select>
              <select value={logLevel} onChange={(e) => setLogLevel(e.target.value)}>
                {LOG_LEVELS.map((l) => (
                  <option key={l || "all"} value={l}>{l || "全部级别"}</option>
                ))}
              </select>
              <input placeholder="筛选关键词" value={logQuery} onChange={(e) => setLogQuery(e.target.value)} />
              <label>
                <input type="checkbox" checked={logAuto} onChange={(e) => setLogAuto(e.target.checked)} /> 自动刷新
              </label>
              <button className="actionbtn" type="button" onClick={() => void refreshLogs()}>刷新</button>
              <button className="actionbtn" type="button" onClick={downloadLogs}>下载</button>
              <button className="actionbtn" type="button" onClick={doClearLogs}>清空</button>
            </div>
            <div className="log-view" id="logView">{logText}</div>
          </div>
        )}

        <div className="settings-actions">
          <button className="actionbtn primary" type="button" disabled={saving} onClick={save}>
            {saving ? "保存中…" : "保存设置"}
          </button>
        </div>
      </div>
    </div>
  );
}
