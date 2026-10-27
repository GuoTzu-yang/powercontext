# 一页技术说明 · seekdb × 阶跃星辰（端侧 AI 推理与混合检索）

> 交付物：`stepfun/` 下可运行 Web 应用（Vite + TypeScript）、后端（Express + seekdb-js）、
> 复现脚本与本文档。所有数字来自本机实测（见 `RESULTS.md`）。

## 1. 模型与量化

| 项 | 内容 |
| --- | --- |
| 模型 | `Xenova/all-MiniLM-L6-v2`（Hugging Face `sentence-transformers/all-MiniLM-L6-v2` 的 ONNX 导出），384 维句向量，Apache-2.0 |
| 获取方式 | 浏览器本地加载：优先 `/models/Xenova/all-MiniLM-L6-v2/` 本地副本，缺失时依次回退 `hf-mirror.com` → `huggingface.co`（`scripts/fetch-models.mjs` 可离线预取） |
| 精度方案 | **INT8（q8）** 与 **FP16（fp16）** 两套权重并附带 **FP32 基线**用于对比；WASM 默认 q8，WebGPU 默认 fp16 |
| 量化方式 | 使用 Hugging Face 官方对该模型的动态量化导出件（ONNX Runtime quantize 生成的 `model_quantized.onnx`）与半精度导出件（`model_fp16.onnx`），非本项目自训 |
| 体积（实测字节） | fp32 90,387,606 B（86.2 MB）→ q8 22,972,370 B（**21.9 MB，压缩比 3.93×**）；fp16 45,297,825 B（**43.2 MB，压缩比 2.00×**） |
| 质量变化（实测） | 同一 WASM 后端、同 12 段语料 + 6 条标注查询、指标基于 seekdb 向量检索排序：**q8 Recall@3 50% / nDCG@3 0.377**，**fp16 Recall@3 50% / nDCG@3 0.355**（q8 略优于 fp16，符合 MiniLM 在中英混排语料上的表现） |
| 未测到的部分 | 本机（Windows + headless Edge + WASM 单线程）加载 86 MB fp32 时 ONNX Runtime 报 `Failed to load model because protobuf parsing failed`，因此 fp32 只报告体积，不报告质量；应用会把该行标记为「该精度在本机运行时加载失败」而不是整表失败，换到内存更宽裕的桌面浏览器通常可加载 |

**为什么量化没有显著掉点**：q8 与 fp16 都是后训练量化/降精度，MiniLM 这类小模型对 8-bit 动态量化不敏感；
两者 Recall@3 相同，nDCG 差 0.02。真正的质量瓶颈不是量化，而是**单塔向量对中文短查询的语义覆盖**（见第 4 节混合检索）。

## 2. 端侧运行

- **运行时**：`@huggingface/transformers` ^3.7.5（Transformers.js）+ ONNX Runtime Web；推理在专用 **Web Worker** 中进行，主线程只做 UI。
- **WebGPU 优先**：Worker 内 `navigator.gpu.requestAdapter()` 探测；可用则 `pipeline(..., { device: "webgpu", dtype: "fp16" })`，失败再退 `webgpu + fp32`。
- **自动降级 WASM**：探测失败或 WebGPU 初始化报错时改用 `{ device: "wasm", dtype: "q8" }`；界面徽标明确写出
  「推理后端：WebGPU · fp16」或「推理后端：WASM（未检测到可用 WebGPU，已自动降级）· q8」，并显示模型来源（local / hf-mirror / huggingface）。
- **验证结果**：本机 Edge 156 headless 无可用 GPU → 自动落到 WASM q8，核心流程（导入 → 向量化 → 写库 → 混合检索）全部正常；有 GPU 的桌面环境走 WebGPU fp16（代码路径与降级分支同源，仅 device/dtype 不同）。
- **禁止云端推理**：仓库内没有任何 `fetch` 指向推理 API；DevTools 网络面板只有本站 `/api/*`、本地 `assets/ort-wasm-*.wasm` 与 `models/*`。ONNX Runtime WASM 由本地静态资源提供（21.6 MB），不走 CDN。

## 3. seekdb 使用

- **接入方式**：`@oceanbase/seekdb-node` v1.4.0（随包附带 seekdb v1.4.0 原生运行时，免安装数据库）以 `seekdb.open(.seekdb)` 拉起本地 seekdb 进程并返回 TCP 端点；再用 `seekdb`（seekdb-js）SDK 以 **Server 模式** 连接，做集合管理与检索。
- **集合结构**（`kb_docs`）：`id`（文本）、`embedding`（`VECTOR(384)`，HNSW + `l2`）、`document`（原文块）、`metadata.title/source`；全文索引建在 `document` 上，分词器 `ngram(ngram_token_size=2)`（中英文都能切）。
- **混合检索在库内完成**：`collection.hybridSearch({ query: { whereDocument: { $contains } }, knn: { queryEmbeddings }, rank: { rrf } })`，
  SDK 实际执行 `SET @search_parm='…'` + `SELECT DBMS_HYBRID_SEARCH.GET_SQL(...)` 再执行其返回的 SQL —— 向量与全文的融合由 seekdb 的 `DBMS_HYBRID_SEARCH` + RRF 完成，客户端不拼两路结果。
- **增量写入与索引刷新**：每次导入调用 `add()` 只写新增块（不重建集合），随后 `refresh_index()`，返回 `refresh: "ok"`；实测刷新后新内容立即可检索（`文档数` 累加、`refresh ok`）。
- **集合/会话状态**：页面顶部持续显示集合所在端点、引擎版本、分词器与文档数；「重置知识库」在库内清空文档并刷新，重置后检索返回 0 条（`scripts/check-reset.mjs` 可复现）。
- **踩坑与修复（写进 README）**：seekdb v1.4 的 `deleteCollection()` 可能残留 `sdk_collections` 元数据行，导致同名重建报 `Duplicate entry`；本项目改为启动时清理「元数据存在但表已不存在」的孤儿行，重置改走库内 `DELETE`。

## 4. 状态管理与优化

| 优化项 | 做法 | 实测 |
| --- | --- | --- |
| 模型加载 | Worker 预热 + 浏览器缓存 + 本地副本优先 | 首次 309–4114 ms（冷/热），缓存后 **227–309 ms** |
| embedding 批处理 | 文档按 batch=8 送入 pipeline（`pooling: mean, normalize: true`） | 12 段 **350 ms**（q8/WASM/单线程） |
| 写入与刷新分离计时 | `add()` 与 `refresh_index()` 独立统计 | 写入 **22 ms** + 刷新 **805 ms**；导入端到端 **1183 ms** |
| 查询延迟 | 单条查询向量化 + seekdb 混合检索 | 查询向量化 **11 ms**，seekdb 检索 **72 ms**，端到端 **88 ms** |
| 检索质量 | 用混合检索替代纯向量 | 同一评测集：纯向量 q8 Recall@3 **50%** → 混合（RRF）Recall@3 **83%** |
| 首次 vs 预热 | 分阶段计时表分别记录本次值；模型加载首次冷启动 4.1 s、预热 0.23 s | 见上表 |

**为什么混合检索值得**：查询是中文短句，MiniLM 是英文训练的句向量模型，纯向量召回只能覆盖 3/6 条查询的前 3 名；
加入 ngram 全文腿用 RRF 融合后提升到 5/6（83%），这正是题面要求「向量+全文混合检索」的价值所在，也说明我们没有用客户端拼结果——同一份语料、同一条 SQL 路径，只有 `search_parm` 里是否带 `query` 的区别。

**局限**：WASM 为单线程（页面未开 `crossOriginIsolated`，ONNX Runtime 会提示后自动单线程），若要更高吞吐需给响应加 COOP/COEP；
fp32 基线在本机运行时加载失败（体积/内存所致），换桌面浏览器可复测；全文分词固定 ngram(2)，如以英文为主可切换 `space`。

## 复现

```bash
cd stepfun && npm install
node scripts/fetch-models.mjs --fp32   # 可选，离线预取 q8/fp16/fp32
npm run build && npm start             # http://127.0.0.1:8791/
node scripts/smoke.mjs                 # 无浏览器冒烟：写入→刷新→混合检索→重置
```
