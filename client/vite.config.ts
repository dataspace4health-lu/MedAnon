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
  const keycloakPort = env.KEYCLOAK_PORT || "8180"
  const trustGatePort = env.TRUST_GATE_PORT || "8400"

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
        // Trailing slash so the SPA routes /trust-gate, /trust-profiles,
        // /trust-history are NOT proxied — only the API under /trust/.
        "/trust/": {
          target: `http://localhost:${trustGatePort}`,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/trust/, ""),
        },
        // Keycloak OIDC — only used in dev when OIDC_ISSUER points at a
        // /auth-relative path. In production nginx proxies /auth/ directly.
        "/auth": {
          target: `http://localhost:${keycloakPort}`,
          changeOrigin: true,
        },
      },
    },
  }
})
