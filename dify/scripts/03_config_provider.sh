#!/usr/bin/env bash
# 配置 MiniMax 供应商（openai_api_compatible）并做一次真实调用校验
# 用法: MINIMAX_API_KEY=<your-key> ./03_config_provider.sh
# 前置：01_login.sh + 02_install_plugin.sh
set -eu
BASE="${DIFY_BASE:-http://localhost}/console/api"
CSRF=$(cat /tmp/dify_csrf.txt)
PROV='langgenius/openai_api_compatible/openai_api_compatible'
KEY="${MINIMAX_API_KEY:?set MINIMAX_API_KEY}"
MODEL="${MINIMAX_MODEL:-MiniMax-M3.1-Flash}"
URL="${MINIMAX_BASE_URL:-https://api.minimax.chat/v1}"

python3 - "$KEY" "$MODEL" "$URL" <<'PY' > /tmp/modelcred.json
import json,sys
key,model,url=sys.argv[1],sys.argv[2],sys.argv[3]
print(json.dumps({"model":model,"model_type":"llm","name":model.lower().replace('.','-'),
 "credentials":{
   "api_key":key,"endpoint_url":url,"mode":"chat",
   "context_size":"128000",
   "max_tokens_to_sample":"8192",          # 推理模型必须给足
   "agent_thought_support":"not_supported",# 关掉思考模式开关
   "compatibility_mode":"strict",          # 只发标准字段
   "api_type":"chat_completions","token_param_name":"auto",
   "stream_include_usage":"disabled","user_identity_support":"no_support",
   "function_calling_type":"no_call","stream_function_calling":"not_supported",
   "vision_support":"no_support","web_search_support":"not_supported",
   "structured_output_support":"not_supported","stream_mode_auth":"not_use"}},
 ensure_ascii=False))
PY

echo "=== create model credential ==="
curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/workspaces/current/model-providers/$PROV/models/credentials" \
  --data @/tmp/modelcred.json -w ' HTTP:%{http_code}\n'

echo "=== validate (真的会打到 MiniMax) ==="
curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/workspaces/current/model-providers/$PROV/models/credentials/validate" \
  --data @/tmp/modelcred.json -w ' HTTP:%{http_code}\n'

echo "=== models ==="
curl -s -b /tmp/dify_ck.txt -H "X-CSRF-Token: $CSRF" \
  "$BASE/workspaces/current/model-providers/$PROV/models?model_type=llm" | head -c 500
