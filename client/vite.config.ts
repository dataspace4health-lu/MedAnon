import path from "path"
import { defineConfig, loadEnv } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "")
  const anonymizerPort = env.ANONYMIZER_PORT || "8000"
  const hapiPort = env.HAPI_PORT || "8081"
  const hapiTargetPort = env.HAPI_TARGET_PORT || "8082"
  const uiPort = env.UI_PORT || "8501"

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      port: Number(uiPort),
      proxy: {
        "/api": {
          target: `http://localhost:${anonymizerPort}`,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ""),
        },
        "/fhir-target": {
          target: `http://localhost:${hapiTargetPort}/fhir`,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/fhir-target/, ""),
        },
        "/fhir": {
          target: `http://localhost:${hapiPort}/fhir`,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/fhir/, ""),
        },
      },
    },
  }
})
