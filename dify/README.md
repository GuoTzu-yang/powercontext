# Dify × OceanBase ｜「剧情记忆助手」—— 带长期记忆的互动剧情 Chatflow

> 赛题：**Dify × OceanBase（Just for Fun）**
> 技术选型：**Dify（必选）＋ PowerContext（记忆）**；向量库用 **pgvector**
> 状态：**已端到端跑通**，含「换新会话仍记得」的实测证据（`records/运行证据.md`）

---

## 0. 一句话玩法

「记忆馆员」和你共创一段连续冒险。关键是**跨会话记忆**：

1. 你在会话 A 随口说「我随身带着一盏不会熄灭的铜灯」；
2. 关掉页面，开**全新会话 B**，只问「你还记得我随身带了什么吗？」；
3. 它仍然答得出铜灯，而且故事接着上次的世界状态往下长。

记忆不在 Dify 会话历史里（换会话就丢），而是每轮**写入 PowerContext**、下一轮**先检索再生成**。

### 技术结合怎么落实（分工表）

| 角色 | 承担 | 落在应用里的位置 |
|---|---|---|
| **Dify**（必选） | 流程编排、模型调用、外部服务调用、结果输出 | Chatflow 5 节点：`开始 → PowerContext 检索(HTTP) → 叙事 LLM → PowerContext 写入(HTTP) → 直接回复` |
| **PowerContext**（记忆） | 长期记忆读写与检索 | `POST /search` 生成前取记忆并注入 system prompt；`POST /remember` 写回本轮问答 |
| MiniMax（模型） | 生成叙事 | 经插件 `openai_api_compatible` 接入 `MiniMax-M3.1-Flash` |

> 作品声明用 **PowerContext** 承载应用记忆，故按规则无需再用 seekdb；pgvector 仅用于 Dify 自身存储（见 `deploy/compose.diff.md`）。

---

## 1. 依赖与版本

| 组件 | 版本 / 规格 |
|---|---|
| Dify | **1.18.0**，docker compose 自部署，15 个容器 |
| 模型供应商插件 | `langgenius/openai_api_compatible` **0.0.73**（Marketplace 在线安装） |
| 模型 | **MiniMax-M3.1-Flash**（OpenAI 兼容：`https://api.minimax.chat/v1`）⚠️ 推理模型，`max_tokens` ≥ 2048 |
| PowerContext | HTTP 桥（FastAPI，`uvicorn pc_bridge:app --port 8099`），PowerContext `builtin` + SQLite，检索 `fts` |
| 向量库 | `pgvector/pgvector:pg16`（`VECTOR_STORE=pgvector`，非 weaviate） |
| 数据库 | PostgreSQL 16（另有 `dify_plugin` 库给插件守护进程） |

---

## 2. 关键设计（评审先看这里）

**记忆 scope = `dify-{{#sys.user_id#}}`**
绑 `user_id` 而不是 `conversation_id` —— 否则换会话就换 scope，记忆立刻失效，退化成 Dify 自带会话历史，拿不到「跨会话」这个要点。

**记忆注入 + 可判定信号**
`/search` 的响应体原样注入 LLM 的 system prompt，并要求模型在回答末尾输出 `【记忆命中：是】` / `【记忆命中：否】`。
→ 评审不必主观读小说，看这个标记就能判定记忆有没有生效。

**失败降级（记忆是增强，不是前置依赖）**
两个 HTTP 节点均设 `error_strategy: default-value`：检索失败→空记忆、写入失败→跳过，**对话绝不中断**。已实测（`records/运行证据.md` R2 轮 5）。

**地址不硬编码**
两个节点的 URL 都是 `{{#env.POWERCONTEXT_BASE_URL#}}/search|/remember`，默认值 `http://powercontext:8099`。
**DSL 中不含任何隧道 IP / 内网临时地址**，换环境只改这一个环境变量。

---

## 3. 从零部署

### 3.1 起 Dify 1.18
```bash
cd <dify-source>/docker
cp .env.example .env          # 按 deploy/.env.example 增补
docker compose up -d
docker compose ps             # 期望全部 Up；db_postgres / pgvector / sandbox healthy
```
控制台 `http://<host>`，首次走 `/setup` 建管理员。

### 3.2 装模型供应商插件（在线，无需离线包）
> 实测：Dify 1.18 控制台的插件市场**页面**路由变了（`GET /console/api/workspaces/current/plugin/marketplace` → 404），
> 但 **Marketplace 安装接口可用**，所以不需下载 `.difypkg`、不需关签名校验。

```bash
export DIFY_BASE=http://localhost DIFY_ADMIN_EMAIL=<admin> DIFY_ADMIN_PASSWORD=<pwd>
./scripts/01_login.sh                      # 产出 /tmp/dify_ck.txt + /tmp/dify_csrf.txt
./scripts/02_install_plugin.sh             # 在线装 openai_api_compatible:0.0.73
```
等价的手工命令：
```bash
POST /console/api/workspaces/current/plugin/install/marketplace
{"plugin_unique_identifiers":["langgenius/openai_api_compatible:0.0.73@4cf123ffaa4556f079bed6c47f3a462cc825504b859b183239e74eb19136a4b5"]}
```
`FORCE_VERIFYING_SIGNATURE` 保持默认 `true`（官方插件已签名，**未关闭校验**）。

### 3.3 配 MiniMax 供应商
```bash
export MINIMAX_API_KEY=<your-key>
./scripts/03_config_provider.sh            # 建模型 + 真实调用校验
```
provider id 是插件作用域的：`langgenius/openai_api_compatible/openai_api_compatible`。

| 字段 | 值 | 为什么 |
|---|---|---|
| `endpoint_url` | `https://api.minimax.chat/v1` | OpenAI 兼容端点 |
| `model` | `MiniMax-M3.1-Flash` | `MiniMax-M2` 已下线 |
| `max_tokens_to_sample` | `8192` | **推理模型**先烧 reasoning tokens；给太小会 `content` 空、`finish_reason=length` |
| `agent_thought_support` | `not_supported` | 关掉思考模式开关，避免端点差异 |
| `compatibility_mode` | `strict` | 只发标准 OpenAI 字段 |
| `stream_include_usage` | `disabled` | MiniMax 对非标准字段容错有限 |
| `user_identity_support` | `no_support` | 同上 |
| `context_size` | `128000` | — |

校验返回 `{"result":"success","error":null}` 即打通（会真实打到 MiniMax）。

### 3.4 让 Dify 能访问 PowerContext（二选一）

**(a) 推荐：PowerContext 与 Dify 同 compose 网络**
`POWERCONTEXT_BASE_URL=http://powercontext:8099`，无需额外配置（仍需留意 squid 私网策略，见 `deploy/compose.diff.md` §3）。

**(b) PowerContext 只在目标机 127.0.0.1 可达（本环境情况）**
```bash
export PC_HOST=<powercontext-host> SSHPASS=<ssh-pwd-or-omit>
./deploy/tunnel.sh                          # 把远端 8099 映射到本机 18099，并打印宿主网关
# 然后 .env 加：SSRF_PROXY_ALLOW_PRIVATE_IPS=<宿主网关>  并 docker compose up -d ssrf_proxy
# DSL 环境变量改成：http://<宿主网关>:18099
```
> ⚠️ 这一步是**可选的本地适配**，仅因本环境 PowerContext 不对外监听。
> 隧道脚本会自检「本机 127.0.0.1:18099」与「容器→宿主网关」两段连通性。

### 3.5 导入应用
```bash
./scripts/04_import_app.sh                 # 导入 app.dsl.yml + 发布 + 建 Service API Key
```
导入后到控制台把 **环境变量 `POWERCONTEXT_BASE_URL`** 改成你的地址（不用改节点）：
> 应用 → 工作流 → 右上「环境变量」→ `POWERCONTEXT_BASE_URL` → 发布。

### 3.6 体验入口
- **控制台**：打开应用「剧情记忆助手」直接聊；点「新对话」即新会话。
- **Service API**（脚本/自动化）：
```bash
curl -s -X POST http://<host>/v1/chat-messages \
  -H "Authorization: Bearer app-<your-app-key>" -H 'Content-Type: application/json' \
  -d '{"inputs":{},"query":"我随身带着一盏不会熄灭的铜灯","response_mode":"blocking","user":"demo-user"}'
```
带 `conversation_id` 续聊；**不带**才是新会话（也是验证跨会话记忆的方法）。

---

## 4. 验证（最短路径）

```bash
export APP_KEY=app-<your-app-key> DIFY_HOST=http://localhost
./scripts/05_smoke_test.sh
```
期望：
1. 埋记忆轮 → 有叙事 + `【记忆命中：否】` + `tokens>0`
2. **新会话召回** → 含「铜灯」+ `【记忆命中：是】` + `CID` 与上一轮不同
3. 同会话追问 → `【记忆命中：是】`（对照）
4. 换 `user` → 未泄露铜灯（scope 隔离）

完整判定口径见 `samples/验证样例-三轮对话.md`；实际跑出的文本/usage 见 `records/运行证据.md`。

### 「评审从干净环境复现」最短命令序列
```bash
# 0) 起 Dify（见 3.1），已含 pgvector
# 1) 登录 + 装插件 + 配模型（三连）
DIFY_BASE=http://localhost DIFY_ADMIN_EMAIL=<admin> DIFY_ADMIN_PASSWORD=<pwd> ./scripts/01_login.sh
./scripts/02_install_plugin.sh
MINIMAX_API_KEY=<your-key> ./scripts/03_config_provider.sh
# 2) 起 PowerContext 可达（同网络填服务名；否则用隧道）
./deploy/tunnel.sh        # 仅当 PowerContext 不对外监听
# 3) 导入 + 拿 key
./scripts/04_import_app.sh
# 4) 冒烟
APP_KEY=app-xxx ./scripts/05_smoke_test.sh
```

---

## 5. 已知限制（照实写）

1. **推理模型开销**：`MiniMax-M3.1-Flash` 单轮 400~700 reasoning tokens，`max_tokens` 已配 8192；给太小会返回空 content。
2. **HTTP 节点 JSON body 必须单条目**：Dify 1.18 的 `body.type=json` 要求 `data` **恰好一个** item，整段 JSON 作为一个 value，否则报 `json body type should have exactly one item`。因此 `query` 里若含英文双引号会破坏 JSON 转义 —— 正常中文输入不受影响。
3. **记忆检索是 FTS 关键词匹配**，非语义向量检索；问法与写入口语完全不重叠时可能漏召回。
4. **写入是 append**：同一轮重复执行会产生重复条目（幂等键只管 scope）。
5. **桥接无删除接口**：清空某 scope 需在 PowerContext 主机删库/改代码（`samples/` 给了替代方案：换 `user`）。
6. **PowerContext 可达性是环境相关**：本环境靠隧道 + squid 私网放行；生产建议放进同一 compose 网络。
7. **未启用 embedding/rerank**：不做文档类 RAG；记忆不依赖 Dify 知识库。

---

## 6. 目录结构

```
dify/
├── README.md                              # 本文件
├── app.dsl.yml                            # ★ 控制台导出的权威 DSL（Chatflow，5 节点，地址走环境变量）
├── samples/
│   └── 验证样例-三轮对话.md                 # 输入 → 预期 → 判定口径 → 重置方法
├── records/
│   └── 运行证据.md                         # 真实 token/usage/conversation_id + 降级实测（已脱敏）
├── powercontext/
│   └── PowerContext接入说明.md              # 接口契约、字段映射、失败降级、可达性三场景
├── deploy/
│   ├── compose.diff.md                     # 相对官方 compose 的差异 + pgvector 替代 weaviate
│   ├── compose.override.yaml               # 可选：host.docker.internal
│   ├── .env.example                        # 需追加的环境变量（占位值）
│   └── tunnel.sh                           # 可选：PowerContext SSH 隧道（一键 + 自检）
├── scripts/
│   ├── 01_login.sh                         # 登录（password 需 base64）+ 取 CSRF
│   ├── 02_install_plugin.sh                # Marketplace 在线装插件 + 轮询任务
│   ├── 03_config_provider.sh               # 配 MiniMax 供应商 + 真实校验
│   ├── 04_import_app.sh                    # 导入 DSL + 发布 + 建 API Key
│   └── 05_smoke_test.sh                    # 四段冒烟（埋记忆/召回/对照/隔离）
└── fallback/
    └── minimax_openai_proxy.py             # 备选：OpenAI 兼容反代（插件装不上时用）
```

**导入顺序**：`app.dsl.yml` → 配 `POWERCONTEXT_BASE_URL` → 发布 → 建 API Key。只需 1 个 DSL 文件。

---

## 7. 配置里的密钥

本仓库**不含任何真实密钥**。所有示例一律占位：`MINIMAX_API_KEY=<your-key>`、`app-<your-app-key>`、`SSHPASS=<ssh-pwd>`、`POWERCONTEXT_BASE_URL=http://powercontext:8099`。
真实密钥只存在于运行环境的 Dify 供应商凭据与本机 `.env`，不进入仓库。
