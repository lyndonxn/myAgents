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

/* 末段「参考来源」拆分（W6-S8 修复：折叠改为真实 JSX 元素渲染——
 * React 19 每次重渲染都会重置 dangerouslySetInnerHTML 子树，折叠放进字符串里会被
 * 轮询重渲染反复打回关闭态，用户永远展开不了。main=正文，tail=来源清单，count=条数） */
export function splitAnswerHtml(text: string): { main: string; tail: string; count: number } {
  const raw = String(text || "");
  const re = /(?:^|\n)[ \t]*#{0,4}[ \t]*\*{0,2}参考来源[:：]?\*{0,2}[ \t]*(?=\n|$)/g;
  let last = -1;
  let m2: RegExpExecArray | null;
  while ((m2 = re.exec(raw))) last = m2.index;
  if (last >= 0) {
    const main = raw.slice(0, last);
    const tail = raw.slice(last);
    if (main.trim() && tail.trim()) {
      const count = (tail.match(/^\s*\[\d+\]/gm) || []).length;
      return { main: mdCore(main), tail: mdCore(tail), count };
    }
  }
  return { main: mdCore(raw), tail: "", count: 0 };
}

/* 流式中的正文（不打折叠，参考来源行按普通文本渲染） */
export const simpleHtml = mdCore;
