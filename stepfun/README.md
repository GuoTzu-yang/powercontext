# seekdb × 阶跃星辰 · 端侧推理迷你知识库

> 2026 数据库大赛生态题（题组 600073 / 子题 800510）交付物。
> 浏览器本地向量化 → 写入 seekdb → **seekdb 库内**向量 + 全文混合检索（RRF 融合）。

一个可在 Chrome / Edge 最新版直接使用的迷你知识库：

1. 导入文本（内置 12 条示例语料 / 粘贴文本 / 上传 .txt·.md）；
2. 在**用户设备浏览器内**用 Transformers.js + ONNX Runtime Web 把每段文本编码为 384 维向量（WebGPU 优先，自动降级 WASM，并在界面明确显示当前后端）；
3. 把向量、原文与元数据写入 seekdb（本地 seekdb 进程，`@oceanbase/seekdb-node` 自带原生运行时，无需另外安装数据库）；
4. 输入查询后，查询向量同样在浏览器本地生成，随后交给 seekdb 执行 **向量 + 全文混合检索**（`DBMS_HYBRID_SEARCH` + RRF），返回排序结果与原文。

全程没有任何云端推理请求：服务端只做「搬运向量 + 调 seekdb」两件事。

---

## 1. 环境要求

| 项 | 要求 |
| --- | --- |
| Node.js | **≥ 22**（`@oceanbase/seekdb-node` 要求 v22+，实测 v24.19.0） |
| 浏览器 | Chrome / Edge 最新版（WebGPU 可选，缺失时自动降级 WASM） |
| 操作系统 | Windows x64 / Linux x64 / Linux arm64 / macOS arm64（跟随 seekdb 原生包） |
| 内存 | 建议 ≥ 4 GB（seekdb 首次初始化分配 `memory_budget=2G`） |
| 磁盘 | 依赖 + 模型约 400 MB（其中 seekdb 原生运行时约 63 MB） |

依赖版本（`package.json` 锁定）：

- `@huggingface/transformers` ^3.7.5（Transformers.js，含 ONNX Runtime Web）
- `@oceanbase/seekdb-node` 1.4.0（本地 seekdb 生命周期 + 随包原生运行时）
- `seekdb` 1.4.0（seekdb-js SDK，Server 模式接入）
- `express` ^4.21.2、`vite` ^6.0.11、`typescript` ^5.7.3

> 仓库内 `.npmrc` 已固定 `legacy-peer-deps=true`：`seekdb@1.4.0` 声明了一个可选 peer
> `@seekdb/default-embed`，其发布元数据引用了 `workspace:` 版本，正常 `npm install` 会报
> `EUNSUPPORTEDPROTOCOL`。本项目不使用该默认 embedding（向量全部由浏览器产生），因此关闭
> peer 自动安装。

## 2. 安装与启动

```bash
cd stepfun
npm install

# 可选但推荐：把模型预取到本地，之后完全离线运行（约 66 MB: INT8 21.9 + FP16 43.2）
node scripts/fetch-models.mjs
# 需要跑量化对比基线时再加 --fp32（额外约 90 MB）
node scripts/fetch-models.mjs --fp32

# 开发模式（Vite HMR + API 服务）
npm run dev

# 生产模式（推荐用于验证/评审）
npm run build
npm start
```

启动后打开 <http://127.0.0.1:8791/>（`npm run dev` 时为 <http://localhost:5273/>）。

启动日志会打印 seekdb 的真实端点，例如：

```
[seekdb] engine=5.7.25-OceanBase seekdb-v1.4.0.0 endpoint=127.0.0.1:27377 boot=4444ms dir=.../.seekdb
[api] http://127.0.0.1:8791
```

数据目录默认 `stepfun/.seekdb`，可用环境变量 `SEEPDB_DIR` 指定；端口用 `PORT` 指定。

> Windows 提示：如遇 esbuild 在 `%TEMP%` 删除临时文件被拒（部分安全软件导致），把
> `TEMP`/`TMP` 指向工作区目录后重新 `npm run build` 即可。

## 3. 数据初始化

首次启动会自动完成，无需手工建库：

1. `seekdb.open(.seekdb)` 拉起本地 seekdb（首次初始化较慢，之后秒级），返回 TCP 端点；
2. `seekdb` SDK 以 Server 模式连接，创建集合 `kb_docs`：
   - 向量列 384 维、`distance=l2`、HNSW 索引；
   - 全文索引 `ngram(2)`（中英文都能切，CJK 友好）；
3. 页面顶部徽标显示 seekdb 版本、端点与分词器。

集合结构（由 seekdb-js 落成的表）：

| 字段 | 说明 |
| --- | --- |
| `id` | 文档块 ID（示例语料为稳定 slug，文件/粘贴为 `file-*` / `paste-*`） |
| `embedding` | 384 维 float 向量，HNSW + l2 |
| `document` | 原文块（≤220 字，40 字重叠切块） |
| `metadata.title / source` | 展示与过滤用元数据；全文索引建在 `document` 上 |

## 4. 验证步骤（5 分钟）

1. **本地推理**：打开页面，等待徽标出现 `推理后端：WebGPU · fp16`（支持 WebGPU）或
   `推理后端：WASM（未检测到可用 WebGPU，已自动降级）· q8`。模型来源显示 `local` 表示走
   本地副本，`hf-mirror` / `huggingface` 表示在线拉取。
2. **导入**：点击「导入内置示例语料（12 条）」，出现
   `已导入 12 段，当前共 12 段（刷新 ok）`，右上角 `文档数：12`。
3. **混合检索**：输入 `浏览器不支持 WebGPU 时怎么办？`，点「检索」。预期（实测一致）：
   - `#1 WebGPU 推理`、`#2 WASM 降级`、`#3 Transformers.js`；
   - 结果上方显示 `融合方式 rrf(seekdb DBMS_HYBRID_SEARCH)`、`seekdb 耗时 72 ms`。
4. **增量写入**：再点一次导入（或粘贴一段新文本导入），`文档数` 累加，新内容立刻可检索
   （`refresh_index` 返回 ok）。
5. **重置**：点「重置知识库」，`文档数` 归 0，再检索返回 0 条 —— 连续导入/切换后状态一致。
6. **量化对比**：点「运行量化对比」，表格会填出 fp32 / q8(INT8) / fp16 三行的
   模型体积、压缩比、12 段向量化耗时、Recall@3、nDCG@3（指标基于 seekdb 向量检索的排序结果）。
7. **无云端推理自证**：DevTools → Network 过滤 `XHR/fetch`，只有本站 `/api/*`；
   Worker 侧只有 `assets/ort-wasm-*.wasm` 与本地 `models/*` 请求。

命令行自动冒烟（不需要浏览器，验证 seekdb 侧写入 → 刷新 → 混合检索 → 重置）：

```bash
node scripts/smoke.mjs
# health: {...} docs: 0
# add: {"ok":true,"added":3,...,"refresh":"ok"}
# search: [{"id":"s-1","rank":1,...}] 16ms rrf(seekdb DBMS_HYBRID_SEARCH)
# reset: {"ok":true,...}
# SMOKE OK
```

## 5. 复现测试的数据

- 评测语料：`app/src/data.ts` 中的 `SAMPLE_DOCS`（12 段，中英混排，覆盖 WebGPU/WASM/量化/seekdb/RRF 等主题）。
- 评测查询与标注：同文件 `EVAL_QUERIES`（6 条查询，各带 gold 文档 ID），用于计算 Recall@3 / nDCG@3。
- 想换成自己的语料：把 `.txt`/`.md` 拖进页面即可；切块逻辑见 `chunkText()`。

## 6. 已知限制

1. **没有浏览器内 WASM 版 seekdb**：npm 上 `seekdb` 只有 Node 原生绑定（`@seekdb/js-bindings` /
   `@oceanbase/seekdb-node`），没有可在浏览器跑的 WASM 构建。题面允许「嵌入式或本地 server」，
   因此本项目采用「浏览器推理 + 本地 seekdb 进程」：**推理位置**在用户设备，**存储与检索**在
   seekdb，均符合要求。
2. 无 WebGPU 的环境（如无头浏览器、老显卡）会走 WASM 单线程；若页面未开启
   `crossOriginIsolated`，ONNX Runtime 会打印多线程不可用提示并自动单线程运行（功能不受影响，
   吞吐略低）。要开多线程需给响应加 `Cross-Origin-Opener-Policy` / `Cross-Origin-Embedder-Policy`。
3. 全文索引用 `ngram(2)`，对中文友好但对超长英文词的召回不如 `space` 分词；如需英文优先，
   把 `server/db.mjs` 里的 `FULLTEXT_ANALYZER` 改成 `space`。
4. 单查询上限 20 条；切块上限 400 段/次（避免一次导入过长文本）。批量导入大语料建议分批。
5. 首次运行若无本地模型副本，需联网从模型源下载（hf-mirror → huggingface），耗时取决于网络。
6. `refresh_index()` 在 seekdb v1.4 上约 450–805 ms/次，导入后同步调用以保证「刷新后立即可检索」；
   大批量导入时可改为最后统一刷新（代码里 `addDocuments` 已把写入与刷新耗时分开计时）。
7. **验证环境**：全部实拍证据在 **Windows 11 x64 + Edge 156（headless，自动降级 WASM q8）** 上取得；
   Linux x64 / Linux arm64 / macOS arm64 走同一套依赖（`@oceanbase/seekdb-node-linux-x64-gnu` /
   `-linux-arm64-gnu` / `-darwin-arm64`），但本机未能重复实测（提交用的服务器没有 Node 运行时）。
8. fp32（86.2 MB）在 headless + WASM 单线程下 `protobuf parsing failed`，量化对比表会把该行标注为
   「该精度在本机运行时加载失败」；换内存更宽裕的桌面浏览器通常可加载并补上基线质量指标。

## 7. 目录结构

```
stepfun/
├─ app/                    # 前端（Vite + TS，无框架）
│  ├─ index.html
│  ├─ public/models/       # fetch-models 预取的 ONNX 模型（可选，离线用）
│  └─ src/
│     ├─ worker.ts         # 推理 Worker：WebGPU 优先 / WASM 降级、dtype 选择、批处理
│     ├─ embedder.ts       # Worker 封装、切块、API 封装、模型体积探测
│     ├─ main.ts           # UI：导入 / 检索 / 量化对比 / 分阶段计时
│     ├─ data.ts           # 示例语料 + 评测查询与标注
│     └─ style.css
├─ server/
│  ├─ index.mjs            # Express：/api/health /api/docs /api/search /api/reset /api/eval/*
│  ├─ db.mjs               # seekdb 生命周期、集合结构、混合检索、重置、评测集合
│  └─ public/              # vite build 产物（npm start 时静态托管）
├─ scripts/
│  ├─ fetch-models.mjs     # 模型预取（local → hf-mirror → huggingface）
│  ├─ smoke.mjs            # 无需浏览器的端到端冒烟
│  ├─ check-state.mjs      # 增量写入/删除/重置的状态检查
│  └─ check-reset.mjs      # 重置前后检索结果对比
├─ TECH-NOTE.md            # 一页技术说明（模型与量化 / 端侧运行 / seekdb / 状态与优化）
├─ RESULTS.md              # 实测数据（体积、耗时、Recall/nDCG）
├─ vite.config.ts
└─ package.json
```
