"use client";

/* 顶栏：工作区切换 / 索引状态 / 记忆轮数 / 重置会话 / 设置 / 主题 */

export type IndexState = { label: string; ok: boolean };

export default function TopBar(props: {
  workspaces: { id: string; name: string }[];
  activeWorkspace: string;
  indexState: IndexState;
  memCount: number;
  onSwitchWorkspace: (id: string) => void;
  onResetSession: () => void;
  onOpenSettings: () => void;
  onToggleTheme: () => void;
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="logo">M</span>
        <span className="wordmark">MYAGENTS</span>
        <span className="plan">TEAM</span>
      </div>
      <div className="topbar-right">
        <div className="ws-switch">
          <span className="ws-ic" />
          <select
            id="workspaceSelect"
            aria-label="工作区"
            value={props.activeWorkspace}
            onChange={(e) => props.onSwitchWorkspace(e.target.value)}
          >
            {props.workspaces.length === 0 && <option value="">服务未启动</option>}
            {props.workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </div>
        <div className="stat">
          <span
            className="dot"
            id="statusDot"
            style={{ background: props.indexState.ok ? "var(--green)" : "var(--red)" }}
          />
          <span className="k">索引</span>
          <span id="indexStatus">{props.indexState.label}</span>
        </div>
        <div className="stat">
          <span className="k">记忆</span>
          <span id="memCount">{props.memCount}</span> 轮
        </div>
        <button className="topbtn" type="button" onClick={props.onResetSession}>
          重置会话
        </button>
        <button className="topbtn" type="button" onClick={props.onOpenSettings}>
          设置
        </button>
        <button className="topbtn" id="themeBtn" title="切换主题" type="button" onClick={props.onToggleTheme}>
          ◐
        </button>
        <span className="account">徐</span>
      </div>
    </header>
  );
}
