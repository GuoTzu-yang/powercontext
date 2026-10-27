#!/usr/bin/env bash
# 从 Dify Marketplace 在线安装 openai_api_compatible 插件，并轮询到装完
# 前置：先跑 01_login.sh
set -eu
BASE="${DIFY_BASE:-http://localhost}/console/api"
CSRF=$(cat /tmp/dify_csrf.txt)
PKG="${PLUGIN_PKG:-langgenius/openai_api_compatible:0.0.73@4cf123ffaa4556f079bed6c47f3a462cc825504b859b183239e74eb19136a4b5}"

echo "=== install $PKG ==="
resp=$(curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/workspaces/current/plugin/install/marketplace" \
  -d "{\"plugin_unique_identifiers\":[\"$PKG\"]}")
echo "$resp"
TASK=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('task_id') or '')")
[ -n "$TASK" ] || { echo "没有拿到 task_id，可能已安装或接口变更"; exit 1; }

echo "=== poll task $TASK ==="
for i in $(seq 1 20); do
  out=$(curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" \
    "$BASE/workspaces/current/plugin/tasks?page=1&page_size=5")
  st=$(echo "$out" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for t in d.get('tasks') or []:
    if t.get('id')=='$TASK':
        print(t.get('status'))
        for p in t.get('plugins') or []:
            print('  ',p.get('plugin_id'),p.get('status'),'::',(p.get('message') or '')[:200],file=sys.stderr)
")
  echo "poll $i: $st"
  case "$st" in success|failed) break;; esac
  sleep 8
done

echo "=== installed plugins ==="
curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" "$BASE/workspaces/current/plugin/list" | head -c 600
