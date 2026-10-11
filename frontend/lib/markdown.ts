/* W6-S4：markdown 渲染（legacy _mdHtml/renderAnswerHtml 1:1 平移）。
 * 输出经 esc 转义的 HTML 字符串，配合 AnswerCard 的容器级事件委托（引用 chip data-cite）。
 * T-011 后追加：GFM 表格。
 * 2026-10-12 渲染器升级：有序列表、引用块（含 Obsidian callout 标记剥离）、水平线、
 * 任务列表、h4、斜体、删除线、行内链接（scheme 白名单防 javascript: XSS）与裸 URL 自动识别。
 * 管线顺序（防相互污染）：围栏分段 → esc → 任务/无序列表 → 标题 → 水平线 →
 * 分组块（有序列表/引用块/表格）→ 行内（代码/加粗/斜体/删除线/链接/URL/引用 chip）→ <br>。 */

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

/* ===== 分组块解析（逐行扫描，命中块的行不参与 <br> 拼接） ===== */

const isTableRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
const isTableSep = (l: string) => /^\s*\|[\s:|\-\s]+\|\s*$/.test(l);
const tableCells = (l: string) =>
  l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
const isOrderedItem = (l: string) => /^\s*\d{1,2}(?:[.][ \t]+|[、][ \t]?)/.test(l);
const isQuoteLine = (l: string) => /^&gt;/.test(l);

function renderTables(lines: string[], out: string[]): number {
  const cells = tableCells;
  const header = cells(lines[0]);
  let i = 2;
  const body: string[][] = [];
  while (i < lines.length && isTableRow(lines[i])) {
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
  return i; // 消费的行数（含表头与分隔行）
}

function renderOrderedList(lines: string[], out: string[]): number {
  let i = 0;
  const items: string[] = [];
  while (i < lines.length && isOrderedItem(lines[i])) {
    items.push(lines[i].replace(/^\s*\d{1,2}(?:[.][ \t]+|[、][ \t]?)/, ""));
    i += 1;
  }
  out.push("<ol class=\"md-ol\">" + items.map((it) => `<li>${it}</li>`).join("") + "</ol>");
  return i;
}

function renderQuote(lines: string[], out: string[]): number {
  let i = 0;
  const rows: string[] = [];
  while (i < lines.length && isQuoteLine(lines[i])) {
    // Obsidian callout 标记（[!note] 等）剥离；其余内容原样（已 esc，行内转换在后续 pass 补齐）
    rows.push(lines[i].replace(/^&gt;[ \t]?/, "").replace(/^\[![a-zA-Z]+\][ \t]*/, ""));
    i += 1;
  }
  out.push('<blockquote class="md-quote">' + rows.join("<br>") + "</blockquote>");
  return i;
}

/* 逐行分组：表格 / 有序列表 / 引用块（三类块互不嵌套，与 LLM 输出习惯一致） */
function groupBlocks(lines: string[]): string {
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const rest = lines.slice(i);
    if (isTableRow(lines[i]) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      i += renderTables(lines.slice(i), out);
      continue;
    }
    if (isOrderedItem(lines[i])) {
      i += renderOrderedList(rest, out);
      continue;
    }
    if (isQuoteLine(lines[i])) {
      i += renderQuote(rest, out);
      continue;
    }
    out.push(lines[i]);
    i += 1;
  }
  return out.join("\n");
}

/* ===== 行内渲染（在分组块之后运行：块产出的 HTML 内同样生效） ===== */

function renderInline(s: string): string {
  return s
    // 行内链接 [text](url)：scheme 白名单防 javascript: XSS；text 内的引用 chip 由后续 pass 处理
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    // 裸 URL 自动识别：不匹配 href 属性内；尾部中文/英文标点剔除出链接
    .replace(/(?<!["'=:])https?:\/\/[^\s<>"']+/g, (m) => {
      const trail = (m.match(/[.,;:!?、。，；：！？)）】》"']+$/) || [""])[0];
      const url = m.slice(0, m.length - trail.length);
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>${trail}`;
    })
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<i>$2</i>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1" title="跳转到引用来源">[$1]</span>');
}

/* 非代码段的完整渲染 */
function renderText(text: string): string {
  let s = esc(text)
    // 任务列表（在无序列表之前：- [x] / - [ ] 优先于普通 bullet）
    .replace(/^- \[x\][ \t]+(.+)$/gmi, "<div>☑ $1</div>")
    .replace(/^- \[[ xX]\][ \t]+(.+)$/gmi, "<div>☐ $1</div>")
    // 无序列表（- / * / + 三种标记）
    .replace(/^(?:-|\*|\+)[ \t]+(.+)$/gm, "<div>• $1</div>")
    // 标题（h4 必须在 h3 之前）
    .replace(/^#### (.+)$/gm, "<h4>$1</h4>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    // 水平线（整行 --- / *** / ___；表格分隔行带 | 不会命中）
    .replace(/^(?:-{3,}|\*{3,}|_{3,})[ \t]*$/gm, "<hr>");
  s = groupBlocks(s.split("\n"));
  s = renderInline(s);
  return s.replace(/\n/g, "<br>");
}

function mdCore(text: string): string {
  const src = String(text || "");
  // 先按围栏代码块分段：代码块内部不做任何块级/行内解析（避免代码里的 |、>、* 被误判）
  return src
    .split(/(```[\s\S]*?```)/g)
    .map((part) => {
      if (part.startsWith("```")) {
        return esc(part).replace(/^```([\s\S]*?)```$/, "<pre><code>$1</code></pre>");
      }
      return renderText(part);
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
