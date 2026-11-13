#!/usr/bin/env bash
# Add native DevMCP to the existing Nginx, without replacing Refit.
set -euo pipefail
domain="devmcp.davemafy.xyz"
if [[ $# -gt 0 ]]; then domain="$1"; fi
if [[ $EUID -ne 0 ]]; then echo "Run this with sudo." >&2; exit 1; fi
if [[ ! "$domain" =~ ^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$ ]]; then
  echo "Invalid hostname." >&2; exit 1
fi
command -v nginx >/dev/null
command -v curl >/dev/null
health="$(curl -fsS --max-time 8 http://127.0.0.1:8742/health)"
if ! printf '%s' "$health" | python3 -c 'import json,sys;x=json.load(sys.stdin);assert x["ready"] is True and x["vm_link"]["mode"]=="direct-vultr"'; then
  echo "Native DevMCP isn't ready." >&2; exit 1
fi

site=/etc/nginx/conf.d/devmcp-native.conf
marker='# managed by dev-mcp: configure-native-nginx.sh'
webroot=/var/www/devmcp-acme
dump="$(mktemp)"
tmp="$(mktemp)"
trap 'rm -f "$tmp" "$dump"' EXIT
nginx -T >"$dump" 2>&1
if ! grep -Fq 'include /etc/nginx/conf.d/*.conf;' "$dump"; then
  echo "Nginx isn't including /etc/nginx/conf.d/*.conf; inspect its config." >&2; exit 1
fi
if [[ -L "$site" ]]; then echo "Refusing symlinked site." >&2; exit 1; fi
if [[ -e "$site" ]] && ! grep -Fqx "$marker" "$site"; then
  echo "Site exists and is not managed by DevMCP; refusing overwrite." >&2; exit 1
fi
if [[ ! -e "$site" ]] && grep -Eq "server_name[[:space:]]+$domain([[:space:];]|$)" "$dump"; then
  echo "Another Nginx site already serves this hostname; refusing duplicate." >&2; exit 1
fi
install -d -m 0755 "$webroot/.well-known/acme-challenge"

http_site() {
cat <<EOF
$marker
server {
    listen 80;
    server_name $domain;
    location ^~ /.well-known/acme-challenge/ {
        root $webroot;
        default_type text/plain;
        try_files \$uri =404;
    }
    location / { return 404; }
}
EOF
}
https_site() {
http_site
cat <<EOF

server {
    listen 443 ssl;
    server_name $domain;
    ssl_certificate /etc/letsencrypt/live/$domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$domain/privkey.pem;
    client_max_body_size 12m;
    location / {
        proxy_pass http://127.0.0.1:8742;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}
EOF
}
if [[ ! -f "/etc/letsencrypt/live/$domain/fullchain.pem" ]]; then
  http_site >"$tmp"
  if [[ -e "$site" ]]; then cp "$site" "$site.bak"; fi
  cp "$tmp" "$site"
  if ! nginx -t; then
    if [[ -f "$site.bak" ]]; then cp "$site.bak" "$site"; else rm -f "$site"; fi
    echo "Nginx config failed; restored previous site." >&2
    exit 1
  fi
  systemctl reload nginx

  # Ensure the public Cloudflare route reaches this VM before requesting TLS.
  challenge="devmcp-test-$(date +%s)-$$"
  expected="native-devmcp-$challenge"
  printf '%s\n' "$expected" >"$webroot/.well-known/acme-challenge/$challenge"
  response="$(curl -LsS --max-time 25 "http://$domain/.well-known/acme-challenge/$challenge" || true)"
  rm -f "$webroot/.well-known/acme-challenge/$challenge"
  if [[ "$response" != "$expected" ]]; then
    echo "Cloudflare/DNS isn't forwarding the ACME challenge to this VM." >&2
    echo "Confirm the A record points to Vultr; try Cloudflare DNS-only mode for certificate issuance." >&2
    exit 1
  fi

  if ! command -v certbot >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot
  fi
  certbot certonly --webroot --webroot-path "$webroot" \
    --domain "$domain" --non-interactive --agree-tos \
    --register-unsafely-without-email --keep-until-expiring
fi

https_site >"$tmp"
if [[ -e "$site" ]]; then cp "$site" "$site.bak"; fi
cp "$tmp" "$site"
if ! nginx -t; then
  if [[ -f "$site.bak" ]]; then cp "$site.bak" "$site"; else rm -f "$site"; fi
  nginx -t && systemctl reload nginx
  echo "DevMCP TLS config failed; restored previous site." >&2
  exit 1
fi
systemctl reload nginx
echo "Local TLS check:"
curl -fsS --max-time 15 --resolve "$domain:443:127.0.0.1" "https://$domain/health"
printf '\nPublic TLS check:\n'
curl -fsS --max-time 20 "https://$domain/health"
printf '\nHTTPS configured. Connect ChatGPT: https://%s/mcp\n' "$domain"
