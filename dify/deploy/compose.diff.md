# 部署差异说明（相对 Dify 1.18 官方 docker compose）

本作品**不修改 Dify 镜像、不改后端代码**，全部通过「插件 + 应用 DSL + 环境变量 + 少量部署侧参数」落地。
下面的差异是「跑通本作品」所需的最小集合。

---

## 1. 向量库：pgvector 替代 weaviate（已有事实）

Dify 1.18 官方 compose 默认走 weaviate。本环境使用 **pgvector**：

```dotenv
# .env
VECTOR_STORE=pgvector
```
配套服务为官方 compose 自带的 `pgvector/pgvector:pg16`（容器 `docker-pgvector-1`，healthy）。

**为什么可替代**
- 本作品的记忆能力由 **PowerContext** 承担，不依赖 Dify 知识库检索；
- Dify 侧只用 pgvector 满足其自身的向量存储需求（默认即可）；
- pgvector 与主库共用同一套 PostgreSQL 运维习惯，容器更少、启动更快，适合比赛环境反复重建。

**取舍（照实说）**
- 超大规模向量检索性能不如专用向量库，但本作品不涉及；
- 若要改用 weaviate，把 `VECTOR_STORE=weaviate` 并启用 compose 里的 `weaviate` 服务即可，应用 DSL 无需改动。

**验证 pgvector 生效**
```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep pgvector      # 期望 Up (healthy)
docker exec docker-db_postgres-1 psql -U postgres -d dify -c '\dx'  # 期望看到 vector 扩展
```

---

## 2. 插件：Marketplace 在线安装（无需离线包、无需关签名）

| 项 | 值 |
|---|---|
| 插件 | `langgenius/openai_api_compatible` **0.0.73** |
| 安装方式 | Marketplace 在线安装（API 见 README §3.2） |
| `FORCE_VERIFYING_SIGNATURE` | 保持 `true`（官方插件已签名，**未关闭校验**） |
| `PLUGIN_STORAGE_TYPE` | `local`（插件落到 `volumes/plugin_daemon/`） |

> 注意：Dify 1.18 控制台的「插件市场」页面路由变了（`GET /console/api/workspaces/current/plugin/marketplace` 返回 404），
> 但这**不影响安装** —— 安装走的是 `POST /console/api/workspaces/current/plugin/install/marketplace`。

---

## 3. 允许 HTTP 节点访问内网 PowerContext

Dify 的 HTTP Request 节点默认**经 `ssrf_proxy`（squid）出网**，而 squid 默认 `http_access deny to_private_networks`。
若 PowerContext 位于内网地址（如宿主网关、同网段服务），必须显式放行，否则 HTTP 节点报连接失败。

```dotenv
# .env —— 放行承载 PowerContext 的主机/网段（按你的实际地址改）
SSRF_PROXY_ALLOW_PRIVATE_IPS=<host-or-cidr>
# 例：宿主网关（Docker 默认 bridge 网关通常是 172.x.0.1）；或直接给网段
# SSRF_PROXY_ALLOW_PRIVATE_IPS=172.17.0.1
# SSRF_PROXY_ALLOW_PRIVATE_IPS=<your-docker-subnet>/16,10.0.0.0/8
```
```bash
docker compose up -d ssrf_proxy      # 重建生效
```
生成结果在容器内 `/etc/squid/dify_allow_private.conf`。

> 若把 PowerContext 直接部署进同一个 compose 网络（推荐），它就在 `docker_default` 网段内 —— 仍然需要这一步放行，
> 或者干脆让 PowerContext 与 Dify 同网络并接受 squid 的私网策略调整。请按你的安全边界选择。

---

## 4. PowerContext 地址：用环境变量，不写死

应用 DSL 里两个 HTTP 节点都引用同一个环境变量：

```yaml
# app.dsl.yml（节选）
workflow:
  environment_variables:
  - name: POWERCONTEXT_BASE_URL
    value: http://powercontext:8099        # 中性默认值，换环境只改这一处
...
    url: '{{#env.POWERCONTEXT_BASE_URL#}}/search'
    url: '{{#env.POWERCONTEXT_BASE_URL#}}/remember'
```

导入后在控制台「工作流 → 环境变量」里改这一项即可，**不需要编辑节点**。

三种典型填法：

| 场景 | `POWERCONTEXT_BASE_URL` | 额外要求 |
|---|---|---|
| PowerContext 与 Dify 同 compose 网络 | `http://powercontext:8099` | 无需额外配置（服务名直连） |
| PowerContext 在宿主/别的机器、地址可直达 | `http://<host>:8099` | 若为私网地址 → 需 §3 放行 |
| PowerContext 只能在目标机 127.0.0.1 访问 | `http://<宿主网关>:<隧道端口>` | 需 §3 放行 + `deploy/tunnel.sh` 起隧道 |

---

## 5. 可选：host.docker.internal（让地址更可移植）

如果希望用 `host.docker.internal` 而不是记网关 IP，可加一个 override：

```yaml
# deploy/compose.override.yaml（可选）
services:
  api:
    extra_hosts: ["host.docker.internal:host-gateway"]
  worker:
    extra_hosts: ["host.docker.internal:host-gateway"]
```
之后 `POWERCONTEXT_BASE_URL=http://host.docker.internal:<隧道端口>`。
（squid 侧仍需 §3 放行，因为它看到的仍是宿主的私网 IP。）

---

## 6. 与官方 compose 的差异清单（汇总）

| 类别 | 变更 | 是否必须 |
|---|---|---|
| 环境变量 | `VECTOR_STORE=pgvector` | 是（本环境既定方案） |
| 环境变量 | `SSRF_PROXY_ALLOW_PRIVATE_IPS=<承载 PowerContext 的私网地址>` | PowerContext 为私网地址时**必须** |
| 环境变量 | `SSRF_PROXY_ALLOW_PRIVATE_IPS` 置空 + 公网可达 PowerContext | 二选一 |
| 服务定义 | `api`/`worker` 加 `extra_hosts: host.docker.internal:host-gateway` | 可选（仅为地址可移植） |
| 镜像 | **无改动** | — |
| 代码 | **无改动** | — |
| 插件 | `openai_api_compatible:0.0.73`（Marketplace 安装） | 是 |
| 应用 | 导入 `app.dsl.yml` | 是 |

---

## 7. 重建/回滚

```bash
# 只重建受影响的容器
docker compose up -d ssrf_proxy
# 回滚 SSRF 放行（改回空值）
sed -i 's|^SSRF_PROXY_ALLOW_PRIVATE_IPS=.*|SSRF_PROXY_ALLOW_PRIVATE_IPS=|' .env && docker compose up -d ssrf_proxy
# 卸载插件（如需）
#  控制台 → 插件 → OpenAI-API-compatible → 卸载；或用
#  POST /console/api/workspaces/current/plugin/uninstall
```
