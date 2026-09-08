import './globals.css';
import Sidebar from '@/components/Sidebar';

export const metadata = { title: 'lquant · 量化研究台', description: 'A股量化研究平台' };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="bg-paper text-ink antialiased">
        <Sidebar />
        <main className="ml-44 min-h-screen px-6 py-6 lg:px-8">{children}</main>
      </body>
    </html>
  );
}
