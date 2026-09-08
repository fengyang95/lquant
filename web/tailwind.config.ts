import type { Config } from 'tailwindcss';

/**
 * 「研报台」设计系统
 * A 股约定：涨=朱砂红，跌=青绿（与欧美相反）。朱砂同时是品牌主色。
 */
export default {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#F4F5F2',   // 瓷灰纸面（页面底）
        panel: '#FBFBF9',   // 面板底
        ink: {
          DEFAULT: '#22252B', // 墨（主文字）
          dim: '#5C6169',     // 次级文字
          faint: '#94989F',   // 弱文字
        },
        line: {
          DEFAULT: '#E3E3DC', // 发丝线
          strong: '#CDCDC4',  // 强分隔
        },
        up: '#C3352B',        // 朱砂 · 涨 / 品牌主色
        down: '#1E7C55',      // 青绿 · 跌
        flat: '#94989F',      // 平
        indigo: '#31589E',    // 靛 · 数据系列 / 链接
        gold: '#B08A3E',      // 金 · 警示 / 辅助系列
      },
      fontFamily: {
        song: ['"Songti SC"', 'STSong', '"Noto Serif SC"', 'SimSun', 'serif'],
        sans: ['-apple-system', '"PingFang SC"', '"Microsoft YaHei"', '"Helvetica Neue"', 'Arial', 'sans-serif'],
        mono: ['"SF Mono"', '"JetBrains Mono"', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      boxShadow: {
        // 纸面无投影：只保留一层极浅的「压印」给浮层（下拉、弹层）用
        lift: '0 2px 12px rgba(34, 37, 43, 0.08)',
      },
    },
  },
  plugins: [],
} satisfies Config;
