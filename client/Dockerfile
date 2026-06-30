# Stage 1: Build
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Serve
FROM nginx:1.27-alpine
# Upgrade Alpine packages to pick up OS-level security patches (Trivy CRITICAL/HIGH)
# openssl is needed by the entrypoint to generate a self-signed TLS cert.
RUN apk upgrade --no-cache && apk add --no-cache openssl
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint-tls.sh
RUN mkdir -p /etc/nginx/certs && \
    chmod +x /docker-entrypoint-tls.sh && \
    chown -R nginx:nginx /var/cache/nginx /var/log/nginx /usr/share/nginx/html /etc/nginx/certs && \
    touch /var/run/nginx.pid && chown nginx:nginx /var/run/nginx.pid
USER nginx
EXPOSE 8501
ENTRYPOINT ["/docker-entrypoint-tls.sh"]
