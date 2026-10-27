#!/usr/bin/env bash
# 三段式冒烟测试：埋记忆 → 新会话召回 → 记忆隔离
# 用法: APP_KEY=app-xxxx DIFY_HOST=http://localhost ./05_smoke_test.sh
set -u
KEY="${APP_KEY:?set APP_KEY（app- 开头的 Service API Key）}"
HOST="${DIFY_HOST:-http://localhost}"
USER_A="${USER_A:-demo-user}"

ask() {  # $1=query $2=cid $3=user
  python3 - "$1" "$2" "$3" <<'PY' > /tmp/req.json
import json,sys
b={"inputs":{},"query":sys.argv[1],"response_mode":"blocking","user":sys.argv[3]}
if sys.argv[2]: b["conversation_id"]=sys.argv[2]
print(json.dumps(b,ensure_ascii=False))
PY
  curl -s --max-time 240 -X POST "$HOST/v1/chat-messages" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' --data @/tmp/req.json
}

show() { python3 -c "
import sys,json
t=sys.stdin.read()
try: d=json.loads(t)
except Exception: print('RAW:',t[:400]); raise SystemExit
if d.get('code'): print('ERROR:',json.dumps(d,ensure_ascii=False)[:400]); raise SystemExit
print('ANSWER:',(d.get('answer') or '').strip()[:400])
print('CID   :',d.get('conversation_id'))
print('TOKENS:',((d.get('metadata') or {}).get('usage') or {}).get('total_tokens'))
"; }

echo "########## 1) 新会话，埋记忆 ##########"
R1=$(ask '我是一名穿越到海底图书馆的旅人，随身带着一盏不会熄灭的铜灯。给我开个场。' '' "$USER_A")
echo "$R1" | show
CID=$(echo "$R1" | python3 -c "import sys,json;print(json.load(sys.stdin).get('conversation_id') or '')" 2>/dev/null)

echo
echo "########## 2) 全新会话召回（核心） ##########"
echo "期望：答案含「铜灯」+【记忆命中：是】+ CID 与上面不同"
ask '你还记得我随身带了什么吗？' '' "$USER_A" | show

echo
echo "########## 3) 同会话追问（对照） ##########"
ask '你还记得我随身带了什么吗？' "$CID" "$USER_A" | show

echo
echo "########## 4) 记忆隔离（换 user） ##########"
echo "期望：未提到铜灯（【记忆命中：否】）"
ask '你还记得我随身带了什么吗？' '' "another-user-$RANDOM" | show
