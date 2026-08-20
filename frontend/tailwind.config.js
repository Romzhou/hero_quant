/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          900: "#0B0E14",
          800: "#121722",
          700: "#1A2133",
          600: "#242F47",
          500: "#2E3B56"
        },
        amber: {
          300: "#FDE68A",
          400: "#FBBF24",
          500: "#F59E0B",
          600: "#D97706"
        },
        mist: "#E6EAF2",
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
