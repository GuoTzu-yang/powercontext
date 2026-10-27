# 实测数据（RESULTS）

环境：Windows 11 x64 · Node v24.19.0 · Edge 156.0.4314.42（headless，无可用 WebGPU → 自动降级 WASM 单线程）
· seekdb v1.4.0（`@oceanbase/seekdb-node` 本地进程，`memory_budget=2G`）· 语料 12 段 / 查询 6 条（`app/src/data.ts`）

## 1. 量化对比（同一后端 WASM，仅精度不同）

| 精度 | 模型文件体积（实测字节） | 压缩比 vs fp32 | 12 段向量化 | Recall@3 | nDCG@3 |
| --- | --- | --- | --- | --- | --- |
| fp32（基线） | 90,387,606 B / 86.2 MB | 1.00× | 运行时加载失败* | — | — |
| q8（INT8） | 22,972,370 B / 21.9 MB | **3.93×** | **349 ms** | **50%** | **0.377** |
| fp16 | 45,297,825 B / 43.2 MB | **2.00×** | **605 ms** | 50% | 0.355 |

\* fp32 在本机 headless + WASM 单线程下报
`Can't create a session. ERROR_CODE: 7, ERROR_MESSAGE: Failed to load model because protobuf parsing failed`
（体积/内存所限）。应用把该行标记为「该精度在本机运行时加载失败」，不影响其余两档与整表。

质量口径：6 条标注查询，向量写入 seekdb 独立集合（`kb_eval_<precision>`），
用 **seekdb 向量检索**的 Top-3 结果计算 Recall@3 / nDCG@3（非客户端排序）。

## 2. 分阶段耗时（页面计时，q8 / WASM）

| 阶段 | 实测 | 说明 |
| --- | --- | --- |
| 模型加载（热） | 227 / 266 / 273 / 309 ms | 本地副本 + 浏览器缓存 |
| 模型加载（冷，首次下载 fp16 43 MB） | 4114 ms | hf-mirror，一次性 |
| 文档向量化 | 350 ms（另一次 439 ms） | 12 段 / batch=8 / wasm q8 |
| 写入 seekdb | 22 ms（另一次 29 ms） | 增量 `add()` 12 条 |
| 索引刷新 | 805 ms（另一次 452 / 634 ms） | `refresh_index()` → ok |
| 导入端到端 | 1183 ms（另一次 940 ms） | 点击导入 → 可检索 |
| 查询向量化 | 11 ms | 1 条查询 |
| seekdb 混合检索 | 72 ms（另一次 16 / 35 / 67 ms） | `DBMS_HYBRID_SEARCH` + RRF |
| 查询端到端 | 88 ms | 点击检索 → 结果渲染 |

## 3. 检索质量：纯向量 vs 混合（同一评测集，q8）

| 检索方式 | Recall@3 | 备注 |
| --- | --- | --- |
| 纯向量（seekdb KNN） | 50% | MiniLM 为英文模型，中文短查询语义覆盖有限 |
| **向量 + 全文 RRF 混合（seekdb `DBMS_HYBRID_SEARCH`）** | **83%** | 同一套文档、同一条库内 SQL 路径，仅 `search_parm` 增加 `query` 分支 |

混合检索是题面要求的能力，也是本应用默认的检索路径（不带 `query` 时退化为纯向量）。

## 4. 功能验证记录（浏览器实拍）

- 徽标：`推理后端：WASM（未检测到可用 WebGPU，已自动降级）· q8` / `模型：Xenova/all-MiniLM-L6-v2 · q8 · 来源 local` /
  `seekdb 5.7.25-OceanBase seekdb-v1.4.0.0 · 127.0.0.1:33570 · ngram(2)` / `文档数：12`
- 导入：`已导入 12 段，当前共 12 段（刷新 ok）`
- 检索（`浏览器不支持 WebGPU 时怎么办？`）：返回 5 条，`融合方式 rrf(seekdb DBMS_HYBRID_SEARCH)`，
  Top1 `WebGPU 推理`、Top2 `WASM 降级`、Top3 `Transformers.js`
- 重置：`文档数` 归 0，再检索 0 条（`scripts/check-reset.mjs`：重置前 `a,b`、重置后空）
- 控制台：仅 ONNX Runtime 的 `crossOriginIsolated` / 单线程提示，无报错、无外部推理请求

## 5. 命令行复现

```
$ node scripts/smoke.mjs
health: {"transport":"tcp","host":"127.0.0.1","port":33570,"engine":"5.7.25-OceanBase seekdb-v1.4.0.0","dim":384,"analyzer":"ngram(2)"} docs: 0
add: {"ok":true,"added":3,"total":3,"writeMs":16,"refreshMs":634,"refresh":"ok","totalMs":650}
search: [{"id":"s-1","rank":1,"d":0.0328},...] 35ms rrf(seekdb DBMS_HYBRID_SEARCH)
reset: {"ok":true,"name":"kb_docs","before":3,"removed":3,"count":0,"dim":384}
SMOKE OK
```
