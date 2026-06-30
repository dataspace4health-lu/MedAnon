# Build context: services/trust-gate/ui/
# Standalone static frontend for the Trust Gate service — no build toolchain.
# Static assets (index.html + styles.css + app.js) served by nginx and proxied
# to trust-gate:8400.
FROM nginx:1.27-alpine

RUN apk upgrade --no-cache

COPY index.html /usr/share/nginx/html/index.html
COPY styles.css /usr/share/nginx/html/styles.css
COPY app.js     /usr/share/nginx/html/app.js
COPY nginx.conf /etc/nginx/conf.d/default.conf

RUN chown -R nginx:nginx /var/cache/nginx /var/log/nginx /usr/share/nginx/html && \
    touch /var/run/nginx.pid && chown nginx:nginx /var/run/nginx.pid

USER nginx
EXPOSE 8401
