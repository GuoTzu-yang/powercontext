#!/usr/bin/env bash
# 导入 app.dsl.yml（可选覆盖已存在的应用并发布），并创建 Service API Key
# 用法: ./04_import_app.sh [app_id_to_overwrite]
# 前置：01_login.sh + 03_config_provider.sh
set -eu
BASE="${DIFY_BASE:-http://localhost}/console/api"
CSRF=$(cat /tmp/dify_csrf.txt)
DSL="${DSL_PATH:-$(dirname "$0")/../app.dsl.yml}"
APP="${1:-}"

[ -f "$DSL" ] || { echo "找不到 DSL: $DSL"; exit 1; }

python3 - "$DSL" "$APP" <<'PY' > /tmp/import.json
import json,sys
c=open(sys.argv[1],encoding='utf-8').read()
p={"mode":"yaml-content","yaml_content":c,"name":"剧情记忆助手"}
if sys.argv[2]: p["app_id"]=sys.argv[2]
print(json.dumps(p,ensure_ascii=False))
PY

echo "=== import ==="
resp=$(curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/apps/imports" --data @/tmp/import.json)
echo "$resp"
APP_ID=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('app_id') or '')")
[ -n "$APP_ID" ] || exit 1

echo "=== publish ==="
curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/apps/$APP_ID/workflows/publish" -d '{}' -w ' HTTP:%{http_code}\n'

echo "=== create service api key ==="
curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/apps/$APP_ID/api-keys" -d '{"type":"app","name":"eco-dify"}' -w ' HTTP:%{http_code}\n'

echo
echo "APP_ID=$APP_ID"
echo "把上面的 token 存到环境变量 KEY，然后跑 05_smoke_test.sh"
