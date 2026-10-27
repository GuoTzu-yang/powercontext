export type Doc = { id: string; title: string; text: string; source: string };

// Built-in demo corpus: 12 short passages. Content is intentionally mixed Chinese/English
// so the ngram full-text leg and the vector leg both have work to do.
export const SAMPLE_DOCS: Doc[] = [
  {
    id: "ob-arch",
    title: "OceanBase 架构",
    text: "OceanBase 是原生分布式关系数据库，采用 Shared-Nothing 架构，数据按分区打散到多台 OBServer，通过 Paxos 协议在多个副本间同步日志，实现高可用与强一致。",
    source: "sample/corpus",
  },
  {
    id: "ob-zone",
    title: "Zone 与副本",
    text: "Zone 是 OceanBase 中的故障隔离单元，通常对应一个机房或机架。同一分区的多个副本分布在不同 Zone 上，多数派副本可用时集群即可继续提供读写服务。",
    source: "sample/corpus",
  },
  {
    id: "seekdb-intro",
    title: "seekdb 简介",
    text: "seekdb 是面向 AI 应用的轻量级数据库，兼容 MySQL 协议，内置向量检索与全文检索能力，支持在单机或嵌入式环境中运行，适合个人知识库与 RAG 场景。",
    source: "sample/corpus",
  },
  {
    id: "seekdb-hybrid",
    title: "seekdb 混合检索",
    text: "seekdb 通过 DBMS_HYBRID_SEARCH 存储过程在库内完成向量检索与全文检索的融合，支持 RRF 排名融合算法，调用方只需要构造 search_parm 参数并执行返回的 SQL。",
    source: "sample/corpus",
  },
  {
    id: "seekdb-vector",
    title: "向量索引",
    text: "向量列使用 VECTOR(dim) 类型声明，可建立 HNSW 索引并指定距离度量，l2 表示欧氏距离，cosine 表示余弦距离。写入后调用 refresh_index 可以让新数据立即可检索。",
    source: "sample/corpus",
  },
  {
    id: "webgpu",
    title: "WebGPU 推理",
    text: "WebGPU 是浏览器提供的现代图形与计算 API，可以把神经网络算子下发到 GPU 执行。在端侧 embedding 推理中，WebGPU 通常比 WASM 快数倍，但需要浏览器与显卡驱动支持。",
    source: "sample/corpus",
  },
  {
    id: "wasm-fallback",
    title: "WASM 降级",
    text: "当浏览器不支持 WebGPU，或者 adapter 请求失败时，应用应自动降级到 WASM 后端继续推理。WASM 后端兼容性最好，可以配合 INT8 量化把模型体积压到原来的四分之一左右。",
    source: "sample/corpus",
  },
  {
    id: "quantization",
    title: "模型量化",
    text: "INT8 量化把 float32 权重映射到 8 位整数，模型体积约为原来的 1/4；FP16 量化用半精度浮点表示权重，体积约为 1/2。量化会带来少量精度损失，需要用 Recall@K 等指标评估。",
    source: "sample/corpus",
  },
  {
    id: "transformersjs",
    title: "Transformers.js",
    text: "Transformers.js 让 Hugging Face 的模型可以直接在浏览器里运行，基于 ONNX Runtime Web 执行推理，支持 WebGPU 与 WASM 两种后端，并通过 dtype 参数选择模型精度。",
    source: "sample/corpus",
  },
  {
    id: "minilm",
    title: "all-MiniLM-L6-v2",
    text: "all-MiniLM-L6-v2 是一个轻量的句向量模型，输出 384 维 embedding，采用对比学习训练，适合语义检索与聚类任务，模型文件小、推理速度快，常用于端侧场景。",
    source: "sample/corpus",
  },
  {
    id: "rrf",
    title: "RRF 融合",
    text: "Reciprocal Rank Fusion 是一种无需训练的排名融合方法，对每一路检索结果按 1/(k+rank) 累加得分，k 常取 60。RRF 可以稳定地把向量召回与关键词召回的优点结合起来。",
    source: "sample/corpus",
  },
  {
    id: "rag",
    title: "RAG 流程",
    text: "检索增强生成先把文档切分成块并向量化入库，用户提问时检索最相关的若干块作为上下文，再交给大模型生成回答。整个过程的关键是检索质量与端到端延迟。",
    source: "sample/corpus",
  },
];

export type EvalItem = { query: string; gold: string[] };

// Small labelled query set used for the quantization quality comparison.
export const EVAL_QUERIES: EvalItem[] = [
  { query: "混合检索是怎么在数据库内完成的", gold: ["seekdb-hybrid"] },
  { query: "浏览器不支持 WebGPU 时怎么办", gold: ["wasm-fallback"] },
  { query: "INT8 量化后模型体积变化", gold: ["quantization"] },
  { query: "HNSW 索引支持哪些距离度量", gold: ["seekdb-vector"] },
  { query: "排名融合算法 RRF 的公式", gold: ["rrf"] },
  { query: "384 维句向量模型", gold: ["minilm"] },
];
