/* W6-S4：markdown 渲染（legacy _mdHtml/renderAnswerHtml 1:1 平移）。
 * 输出经 esc 转义的 HTML 字符串，配合 AnswerCard 的容器级事件委托（引用 chip data-cite）。
 * T-011 后追加：GFM 表格渲染（| 单元格 | + | --- | 分隔行）——此前表格被逐行 <br> 成纯文本。 */

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

/* 行内 + 块级渲染（不含围栏代码块；表格在其后单独解析） */
function inlineAndBlocks(text: string): string {
  let s = esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/^- (.+)$/gm, "<div>• $1</div>");
  s = renderTables(s);
  return s
    .replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1" title="跳转到引用来源">[$1]</span>')
    .replace(/\n/g, "<br>");
}

/* GFM 表格：连续的 | 行 + | --- | 分隔行 → <table>。
 * 在 <br> 拼接之前运行；表格行不参与 <br>，非表格行原样保留换行。
 * 单元格内容已经过 esc + 行内转换（加粗/引用 chip 照常渲染）。 */
function renderTables(html: string): string {
  const lines = html.split("\n");
  const isRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l: string) => /^\s*\|[\s:|\-\s]+\|\s*$/.test(l);
  const cells = (l: string) =>
    l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    if (isRow(lines[i]) && i + 1 < lines.length && isSep(lines[i + 1])) {
      const header = cells(lines[i]);
      i += 2;
      const body: string[][] = [];
      while (i < lines.length && isRow(lines[i])) {
        body.push(cells(lines[i]));
        i += 1;
      }
      out.push('<div class="md-table-wrap"><table class="md-table">');
      out.push("<thead><tr>" + header.map((c) => `<th>${c}</th>`).join("") + "</tr></thead>");
      out.push(
        "<tbody>" +
          body.map((r) => "<tr>" + header.map((_, j) => `<td>${r[j] ?? ""}</td>`).join("") + "</tr>").join("") +
          "</tbody>",
      );
      out.push("</table></div>");
    } else {
      out.push(lines[i]);
      i += 1;
    }
  }
  return out.join("\n");
}

function mdCore(text: string): string {
  const src = String(text || "");
  // 先按围栏代码块分段：代码块内部不做任何块级/表格解析（避免代码里的 | 被误判成表格）
  return src
    .split(/(```[\s\S]*?```)/g)
    .map((part) => {
      if (part.startsWith("```")) {
        return esc(part).replace(/^```([\s\S]*?)```$/, "<pre><code>$1</code></pre>");
      }
      return inlineAndBlocks(part);
    })
    .join("");
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
