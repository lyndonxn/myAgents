/* W6-S4：markdown 渲染（legacy _mdHtml/renderAnswerHtml 1:1 平移）。
 * 输出经 esc 转义的 HTML 字符串，配合 AnswerCard 的容器级事件委托（引用 chip data-cite）。 */

export function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => {
    switch (c) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      default: return "&#39;";
    }
  });
}

function mdCore(text: string): string {
  return esc(text)
    .replace(/```([\s\S]*?)```/g, "<pre><code>$1</code></pre>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/^- (.+)$/gm, "<div>• $1</div>")
    .replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1" title="跳转到引用来源">[$1]</span>')
    .replace(/\n/g, "<br>");
}

/* W7 追加修复：剥离正文末尾的「参考来源」段——来源已在「引用来源」证据卡展示，
 * 正文不再重复渲染（用户明确要求）。兼容 **加粗**、中英冒号、# 号等修饰的任意顺序，
 * 例如「**参考来源**：」「参考来源：」「### 参考来源」。返回剥离后的正文。 */
export function stripRefSection(text: string): { main: string } {
  const raw = String(text || "");
  const re = /(?:^|\n)[ \t]*#{0,4}[ \t]*[-*• \t]*参考来源[ \t]*[:：\*# \t]*(?=\n|$)/g;
  let last = -1;
  let m2: RegExpExecArray | null;
  while ((m2 = re.exec(raw))) last = m2.index;
  if (last >= 0) {
    const main = raw.slice(0, last);
    if (main.trim()) return { main: mdCore(main) };
  }
  return { main: mdCore(raw) };
}

/* 流式中的正文（不打折叠，参考来源行按普通文本渲染） */
export const simpleHtml = mdCore;
