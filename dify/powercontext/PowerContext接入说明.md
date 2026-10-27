# PowerContext 接入说明（接口 / 字段 / 失败降级）

本作品用 **PowerContext** 承担「应用记忆」：每轮对话**先检索、后生成、再写回**。
接入方式是 **HTTP Request 节点**（不使用插件），因为 PowerContext 官方提供的是 HTTP API，这正是赛题允许的三条路径之一。

---

## 1. 拓扑

```
Dify Chatflow
  ├─[1] HTTP POST {POWERCONTEXT_BASE_URL}/search    ← 取历史记忆
  ├─[2] LLM 节点（记忆注入 system prompt）           ← 生成叙事
  ├─[3] HTTP POST {POWERCONTEXT_BASE_URL}/remember  ← 写回本轮对话
  └─[4] Answer 节点                                  ← 输出
```

PowerContext 侧是一个 HTTP 桥（FastAPI，`uvicorn pc_bridge:app --host 0.0.0.0 --port 8099`），
底层是 PowerContext `builtin` 运行时 + SQLite（`sqlite+aiosqlite:////root/pc_bridge.db`），检索模式 `fts`。

---

## 2. 接口契约

### 2.1 `GET /health`
健康检查，同时暴露「scope 名 → scope_id」映射，便于确认应用是否真的建了 scope。

```json
// 200
{"ok": true,
 "scopes": {"story": "scp_53pfqc9h8aaavxwxm2f2w627b5",
            "dify-demo-user": "scp_39tcrr5r2c0x5739vnthqdc73n"}}
```

### 2.2 `POST /remember`
写入一条记忆。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `scope` | string | 是 | 记忆空间名。首次出现会自动创建 scope（幂等键 `scope-<name>`） |
| `text` | string | 是 | 记忆正文（纯文本） |

```json
// 请求
{"scope": "dify-demo-user", "text": "玩家：我带着铜灯 | 记忆馆员：好，我记住了"}
// 200
{"ok": true, "scope_id": "scp_39tcrr5r2c0x5739vnthqdc73n"}
```
写入方式为 `mode="append"`，条目 `kind="plot"`（剧情记忆）。

### 2.3 `POST /search`
检索相关记忆，返回**纯文本数组**。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `scope` | string | 是 | — | 与写入时一致 |
| `query` | string | 是 | — | 检索词 |
| `limit` | int | 否 | 5 | 返回条数上限 |

```json
// 请求
{"scope": "dify-demo-user", "query": "铜灯", "limit": 5}
// 200
{"hits": ["玩家：我带着铜灯 | 记忆馆员：好，我记住了"]}
// 无命中
{"hits": []}
```
检索前会先 `flush()`（最多 6 轮）确保刚写入的记忆可见；检索模式为 **FTS**（关键词）。

---

## 3. 本作品里的字段映射

| DSL 中的位置 | 值 | 说明 |
|---|---|---|
| 检索节点 URL | `{{#env.POWERCONTEXT_BASE_URL#}}/search` | 地址走环境变量，不硬编码 |
| 检索节点 `scope` | `dify-{{#sys.user_id#}}` | **按用户**分配记忆空间 → 同用户跨会话共享，不同用户隔离 |
| 检索节点 `query` | `{{#sys.query#}}` | 玩家本轮输入 |
| 检索节点 `limit` | `5` | 取 5 条足够注入 prompt |
| 写入节点 `scope` | `dify-{{#sys.user_id#}}` | 与检索一致 |
| 写入节点 `text` | `玩家：{{#sys.query#}} \| 记忆馆员：{{#llm.text#}}` | 把「问 + 答」成对写入，便于后续 FTS 召回 |
| LLM system prompt | 内含 `{{#pc_search.body#}}` | 把 `/search` 的原始 JSON 响应直接注入，由模型自行判断相关性 |

### 为什么 scope 绑 `sys.user_id` 而不是 `sys.conversation_id`
绑 `conversation_id` 的话，换会话就换 scope，记忆立刻失效 —— 那就退化成 Dify 自带的会话历史，拿不到「跨会话」这个赛题要点。
绑 `user_id` 才能让「关掉页面、开新会话」仍然记得。

### 记忆注入方式
`/search` 的响应体（`body`）原样注入 system prompt，由 LLM 判断相关性并**在回答末尾输出判定标记**：

```
【长期记忆检索结果（来自 PowerContext）】
{{#pc_search.body#}}

【回答规则】
1. 先判断检索结果中是否有与本次输入语义相关的内容。
2. 若有：必须自然引用该记忆，并在回答最后一行输出【记忆命中：是】。
3. 若无：当作故事新起点，并在回答最后一行输出【记忆命中：否】。
```
`【记忆命中：是/否】` 是给评审的**可判定信号**，不依赖主观阅读。

---

## 4. 失败降级（关键）

记忆是**增强**，不是**前置依赖**。任何一侧挂掉都不该让对话中断。
本作品对两个 HTTP 节点都设置了节点级异常处理：

```yaml
# app.dsl.yml（两个 HTTP 节点均有）
error_strategy: default-value
default_value:
- key: body
  type: string
  value: '{"hits": []}'      # 检索节点：降级为空记忆
- key: status_code
  type: number
  value: 0
- key: headers
  type: object
  value: {}
```

| 失败场景 | 行为 | 用户体验 |
|---|---|---|
| `/search` 超时 / 连不上 / 5xx | 走 `default_value` → `body` 变 `{"hits": []}` | 正常回答，但 `【记忆命中：否】`（无记忆可用） |
| `/remember` 失败 | 走 `default_value` → `body` 变 `{"ok": false}` | 本轮回答照常返回；代价是这轮没写进记忆 |
| PowerContext 整体不可达 | 两个节点都降级 | **对话不报错、不中断**，退化为「无记忆的普通对话」 |
| LLM 供应商不可用 | 无降级（记忆依赖生成结果） | 返回模型错误 —— 这是唯一的硬依赖 |

配套的重试策略（节点内已配）：`max_retries: 2`，`retry_interval: 1000ms`，指数退避关闭。
先重试、再降级，避免瞬时抖动导致丢记忆。

**已实测**：把 `POWERCONTEXT_BASE_URL` 指向不可达地址 `http://127.0.0.1:59999`，对话仍返回 200 与完整回答，`tokens=904`，标记为 `【记忆命中：否】`。见 `records/运行证据.md` R2 轮 5。

---

## 5. 可达性配置（三种场景）

| 场景 | `POWERCONTEXT_BASE_URL` | 额外要求 |
|---|---|---|
| 与 Dify 同一 compose 网络（**推荐**） | `http://powercontext:8099` | 无需 SSRF 放行 |
| 宿主 / 另一台机器、地址直达 | `http://<host>:8099` | 私网地址需 `SSRF_PROXY_ALLOW_PRIVATE_IPS` 放行 |
| 只能在目标机 127.0.0.1 访问 | `http://<宿主网关>:<隧道端口>` | 放行 + `deploy/tunnel.sh` |

> Dify 的 HTTP 节点默认经 `ssrf_proxy`(squid) 出网，squid 默认 `deny to_private_networks`。
> 所以「私网地址」一律需要 `.env` 里 `SSRF_PROXY_ALLOW_PRIVATE_IPS=<地址或网段>` 并 `docker compose up -d ssrf_proxy`。

---

## 6. 已知限制

1. **FTS 不是语义检索**：问法与写入口语完全不重叠时可能漏召回（如写入「铜灯」、提问「照明工具」）。要提升召回可在桥接侧把 `mode="fts"` 改成混合/向量模式。
2. **桥接未提供删除接口**：清空某个 scope 需在 PowerContext 主机删库/改桥接代码。若要「一键重置」，建议扩展 `POST /forget {scope}`。
3. **`body` 注入是原始 JSON 字符串**：模型需自行解析 `{"hits":[...]}`。若换成小模型，建议在桥接侧直接返回拼好的纯文本摘要。
4. **写入是 append**：同一轮重复执行会产生重复条目（幂等由 `idempotency_key=scope-<name>` 只管 scope，不管条目）。
5. **中文双引号**：HTTP 节点 JSON body 里 `query`/`text` 用模板直接拼接，若用户输入含英文双引号会破坏 JSON —— 见 README「已知限制」。

---

## 7. 未被本作品使用的接口

桥接仅暴露 `/health`、`/remember`、`/search` 三个端点；PowerContext 自身的更多能力（scope 管理、任务上下文等）本作品未使用，故不作说明。
