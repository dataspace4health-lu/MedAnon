#!/bin/sh
# ---------------------------------------------------------------------------
# UI container entrypoint.
#
# Ensures a TLS certificate exists before starting nginx. The SPA's OIDC PKCE
# flow needs a secure context (HTTPS), so the UI edge MUST serve TLS.
#
# Precedence:
#   1. If a real cert is mounted at /etc/nginx/certs/tls.{crt,key}, use it.
#   2. Otherwise generate a self-signed cert covering TLS_SAN (first boot only;
#      persisted in the ui-certs volume so the fingerprint stays stable across
#      restarts  users only accept the browser warning once).
#
# For production, mount a CA-signed cert into /etc/nginx/certs to replace the
# self-signed one (no rebuild needed).
# ---------------------------------------------------------------------------
set -e

CERT_DIR=/etc/nginx/certs
CRT="$CERT_DIR/tls.crt"
KEY="$CERT_DIR/tls.key"

# Space/comma-separated host list the cert should be valid for. Include the
# public IP/hostname users reach the UI on, e.g. TLS_SAN="10.168.192.22 app.example.org".
TLS_SAN="${TLS_SAN:-localhost 127.0.0.1}"

if [ ! -f "$CRT" ] || [ ! -f "$KEY" ]; then
    echo "[entrypoint] No TLS cert found  generating self-signed cert for: $TLS_SAN"

    # Build subjectAltName: DNS entries for names, IP entries for dotted quads.
    san=""
    for h in $(echo "$TLS_SAN" | tr ',' ' '); do
        [ -z "$h" ] && continue
        case "$h" in
            *[!0-9.]*) san="${san}DNS:${h}," ;;   # contains non-digit/dot → DNS
            *)         san="${san}IP:${h}," ;;     # dotted quad → IP
        esac
    done
    san="${san%,}"   # strip trailing comma

    cn=$(echo "$TLS_SAN" | tr ',' ' ' | awk '{print $1}')
    openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
        -keyout "$KEY" -out "$CRT" \
        -subj "/CN=${cn:-medanon-ui}" \
        -addext "subjectAltName=${san}" 2>/dev/null
    echo "[entrypoint] Self-signed cert generated (SAN: ${san})."
else
    echo "[entrypoint] Using existing TLS cert at $CRT"
fi

exec nginx -g 'daemon off;'
