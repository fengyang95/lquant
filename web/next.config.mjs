/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // standalone 输出供 ./lquant.sh build 打发行包（自带精简 node_modules）
  output: 'standalone',
  async rewrites() {
    const api = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
    return [
      { source: '/api/:path*', destination: `${api}/api/:path*` },
      { source: '/ws/:path*', destination: `${api}/ws/:path*` },
    ];
  },
};
export default nextConfig;
