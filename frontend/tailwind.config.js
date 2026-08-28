/** @type {import('tailwindcss').Config} */
// ESM 配置：依赖 frontend/package.json "type": "module" 与 tailwindcss@3.4 ESM 支持；如需兼容 CJS 工具链可另提供 module.exports 回落（当前无需）
export default {
  // content 指向 frontend 根的相对路径，Vite 构建以 frontend 为根时生效；动态类通过下方 safelist 兜底，避免 purge
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  safelist: [
    // ensure dynamically composed status classes are not purged (e.g. Chat/Live/Monitor 中的 bg-amber-400/15 等由三元静态字面量组成，safelist 仅为保险)
    "bg-emerald-400/10","bg-amber-400/10","bg-red-400/10","border-emerald-400/20","border-amber-400/20","border-red-400/20","text-emerald-200","text-amber-200","text-red-200"
  ],
  theme: {
    extend: {
      colors: {
        // brand tokens — ink/mist are fully custom; amber/slate extend defaults intentionally (amber-300 remaps to tailwind amber-200 for warmer highlight)
        ink: {
          900: "#0B0E14",
          800: "#121722",
          700: "#1A2133",
          600: "#242F47",
          500: "#2E3B56"
        },
        amber: {
          300: "#FDE68A", // = tailwind amber-200, intentionally warmer for mist contrast
          400: "#FBBF24",
          500: "#F59E0B",
          600: "#D97706"
        },
        mist: "#E6EAF2",
        // slate-300/400 match tailwind defaults explicitly for design-token clarity; extend merges, so no purge risk
        slate: {
          300: "#CBD5E1",
          400: "#94A3B8"
        }
      },
      fontFamily: {
        display: ["Space Grotesk", "system-ui", "sans-serif"],
        serif: ["Noto Serif SC", "serif"],
        mono: ["JetBrains Mono", "monospace"]
      },
      boxShadow: {
        glow: "0 0 40px rgba(245,158,11,0.15)",
        card: "0 20px 60px rgba(0,0,0,0.35)"
      }
    }
  },
  plugins: []
}
