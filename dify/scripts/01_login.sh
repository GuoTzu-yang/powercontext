#!/usr/bin/env bash
# 登录控制台，产出 cookie + CSRF，供后续脚本复用
# 用法: DIFY_ADMIN_EMAIL=... DIFY_ADMIN_PASSWORD=... ./01_login.sh
set -eu
BASE="${DIFY_BASE:-http://localhost}/console/api"
EMAIL="${DIFY_ADMIN_EMAIL:?set DIFY_ADMIN_EMAIL}"
PASS="${DIFY_ADMIN_PASSWORD:?set DIFY_ADMIN_PASSWORD}"

# Dify 1.18 登录接口要求 password 为 base64（注意不是明文）
PB64=$(printf '%s' "$PASS" | base64 -w0)

rm -f /tmp/dify_ck.txt
code=$(curl -s -c /tmp/dify_ck.txt -o /tmp/dify_login.json -w '%{http_code}' \
  -X POST "$BASE/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PB64\",\"language\":\"zh-Hans\",\"remember_me\":true}")

CSRF=$(awk '/csrf_token/{print $7}' /tmp/dify_ck.txt)
printf '%s' "$CSRF" > /tmp/dify_csrf.txt

echo "login HTTP=$code  csrf_len=${#CSRF}"
[ "$code" = "200" ] || { echo "登录失败:"; cat /tmp/dify_login.json; exit 1; }
echo "cookie=/tmp/dify_ck.txt  csrf=/tmp/dify_csrf.txt"
