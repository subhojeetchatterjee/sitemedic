import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      animation: {
        pulse: "pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
      fontFamily: {
        mono: ["var(--font-mono)", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
