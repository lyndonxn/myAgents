import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MYAGENTS — 企业知识库问答",
  description: "本地优先的 Markdown 知识库问答 Agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
        {/* 主题在首帧前恢复，避免暗色用户闪白（与 legacy 行为一致） */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{const t=localStorage.getItem('myagents-theme');if(t)document.documentElement.dataset.theme=t;}catch(_e){}",
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
