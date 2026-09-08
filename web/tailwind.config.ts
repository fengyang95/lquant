import type { Config } from 'tailwindcss';

// A 股约定：涨=红，跌=绿（与欧美相反）
export default {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        up: '#e5484d',
        down: '#30a46c',
        flat: '#8b8d98',
      },
    },
  },
  plugins: [],
} satisfies Config;
