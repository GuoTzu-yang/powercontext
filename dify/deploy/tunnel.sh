#!/usr/bin/env bash
# ============================================================
# 可选本地适配：把「只能在目标机 127.0.0.1 访问」的 PowerContext
# 通过 SSH 隧道暴露给 Dify 容器。
#
# 为什么需要：PowerContext 桥默认只监听 127.0.0.1:8099，Dify 在 docker 里
#   访问不到；本脚本把远端 8099 映射到本机 18099，并绑定 0.0.0.0
#   （只绑 127.0.0.1 的话容器仍然连不上）。
#
# 若你的 PowerContext 本身对 Dify 可达（同一 compose 网络 / 公网 / 同网段），
#   不需要本脚本 —— 直接把 DSL 的 POWERCONTEXT_BASE_URL 指过去即可。
#
# 用法：
#   PC_HOST=1.2.3.4 ./tunnel.sh                 # 用 SSH key 登录
#   PC_HOST=1.2.3.4 SSHPASS='***' ./tunnel.sh   # 用密码（需 sshpass）
#   ./tunnel.sh stop                            # 停止隧道
# ============================================================
set -uo pipefail

PC_HOST="${PC_HOST:-<powercontext-host>}"
PC_USER="${PC_USER:-root}"
PC_PORT="${PC_PORT:-8099}"        # 远端 PowerContext 端口
LOCAL_PORT="${LOCAL_PORT:-18099}" # 本机映射端口（需与 DSL 里填的一致）
SSH_OPTS="-N -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3"

if [ "${1:-}" = "stop" ]; then
  pkill -f "${LOCAL_PORT}:127.0.0.1:${PC_PORT}" && echo "tunnel stopped" || echo "no tunnel running"
  exit 0
fi

if [ "$PC_HOST" = "<powercontext-host>" ]; then
  echo "请先设置 PC_HOST（PowerContext 所在机器）"; exit 1
fi

# 1) 起隧道（绑定 0.0.0.0，否则容器访问不到）
if [ -n "${SSHPASS:-}" ]; then
  command -v sshpass >/dev/null || { echo "需要 sshpass：apt-get install -y sshpass"; exit 1; }
  sshpass -e ssh $SSH_OPTS -L "0.0.0.0:${LOCAL_PORT}:127.0.0.1:${PC_PORT}" "${PC_USER}@${PC_HOST}"
else
  ssh $SSH_OPTS -L "0.0.0.0:${LOCAL_PORT}:127.0.0.1:${PC_PORT}" "${PC_USER}@${PC_HOST}"
fi
rc=$?
[ $rc -ne 0 ] && { echo "隧道启动失败 rc=$rc"; exit $rc; }
sleep 2

# 2) 本机自检
echo "--- local health ---"
curl -s -m 10 "http://127.0.0.1:${LOCAL_PORT}/health" || { echo "本机访问隧道失败"; exit 1; }
echo

# 3) 容器可达性自检（需要 docker 权限）
GW=$(docker network inspect "$(docker inspect docker-api-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{break}}{{end}}' 2>/dev/null)" \
      --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
if [ -n "${GW:-}" ]; then
  echo "--- docker host gateway = $GW ---"
  docker exec docker-api-1 python3 -c "
import urllib.request
print(urllib.request.urlopen('http://${GW}:${LOCAL_PORT}/health',timeout=8).read().decode())
" 2>&1 | tail -2
  echo
  echo "提示：把 DSL 的 POWERCONTEXT_BASE_URL 设为 http://${GW}:${LOCAL_PORT}"
  echo "     并在 .env 设 SSRF_PROXY_ALLOW_PRIVATE_IPS=${GW} 后 docker compose up -d ssrf_proxy"
fi
