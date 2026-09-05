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

/* 末段「参考来源」清单默认收进 <details class="ref-fold">（legacy renderMd 语义） */
export function answerHtml(text: string): string {
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
      return (
        mdCore(main) +
        `<details class="ref-fold"><summary>参考来源${count ? `（${count} 条）` : ""}<span class="rf-caret">▶</span></summary><div class="rf-body">${mdCore(tail)}</div></details>`
      );
    }
  }
  return mdCore(raw);
}
