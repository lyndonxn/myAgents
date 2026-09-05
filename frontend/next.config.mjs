/** Next.js 配置。
 * 默认：静态导出（out/），由本地 Python 服务器（8787）直接伺服，API 同源零跨域。
 * NEXT_DEV_PROXY=1（npm run dev）：切到 dev 服务器（3000），/api 反向代理到 8787 的
 * Python 后端——浏览器视角仍同源，后端无需任何 CORS 改动。
 */
const nextConfig =
  process.env.NEXT_DEV_PROXY === '1'
    ? {
        async rewrites() {
          return [{ source: '/api/:path*', destination: 'http://127.0.0.1:8787/api/:path*' }];
        },
      }
    : {
        output: 'export',
        images: { unoptimized: true },
      };

export default nextConfig;
