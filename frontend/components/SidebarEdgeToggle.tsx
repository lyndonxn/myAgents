"use client";

/* W10：骑缝圆形收起/展开按钮。
 * 贴合侧栏与内容区分界线（垂直居中、水平骑跨），20×20 白底圆钮 + 8px 单线 chevron。
 * side="left"：展开态 chevron 朝左（点击收起）；side="right"：镜像（供右侧面板复用）。
 * 旋转用 transform 与宽度过渡同步；样式细节见 globals.css .edge-toggle。 */

export default function SidebarEdgeToggle(props: {
  side?: "left" | "right";
  collapsed: boolean;
  onToggle: () => void;
  title?: string;
}) {
  const side = props.side ?? "left";
  const stateTitle = props.collapsed ? "展开" : "收起";
  return (
    <button
      type="button"
      className={`edge-toggle edge-${side}${props.collapsed ? " collapsed" : ""}`}
      aria-label={props.title || stateTitle}
      title={props.title || stateTitle}
      aria-expanded={side === "left" ? !props.collapsed : props.collapsed}
      onClick={props.onToggle}
    >
      <svg
        width="8"
        height="8"
        viewBox="0 0 8 8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M5.5 1 L2.5 4 L5.5 7" />
      </svg>
    </button>
  );
}
