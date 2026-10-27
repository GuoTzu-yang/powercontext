// End-to-end smoke test against the local API (no browser needed).
//   node scripts/smoke.mjs
// Uses deterministic pseudo-vectors, so it validates the seekdb plumbing
// (write -> refresh -> hybrid retrieval -> reset) independent of the model.
const BASE = process.env.API_BASE ?? "http://127.0.0.1:8791";
const DIM = 384;

const rand = (seed) => {
  let s = seed;
  return () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    return s / 0x7fffffff;
  };
};
const vec = (seed) => {
  const r = rand(seed);
  const v = Array.from({ length: DIM }, () => r() - 0.5);
  const n = Math.hypot(...v) || 1;
  return v.map((x) => x / n);
};

async function call(path, body) {
  const res = await fetch(BASE + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    throw new Error(`${path} -> HTTP ${res.status} non-JSON: ${text.slice(0, 300)}`);
  }
  if (!res.ok || json.ok === false) throw new Error(`${path} -> HTTP ${res.status}: ${json.error ?? text}`);
  return json;
}

const health = await call("/api/health");
console.log("health:", JSON.stringify(health.engine), "docs:", health.docs);

const docs = [
  { id: "s-1", title: "seekdb 混合检索", text: "seekdb 通过 DBMS_HYBRID_SEARCH 在库内融合向量与全文检索", vector: vec(1) },
  { id: "s-2", title: "WebGPU 降级", text: "浏览器不支持 WebGPU 时自动降级到 WASM 后端", vector: vec(2) },
  { id: "s-3", title: "INT8 量化", text: "INT8 量化把模型体积压缩到约四分之一", vector: vec(3) },
];
const added = await call("/api/docs", { docs });
console.log("add:", JSON.stringify(added));

const hit = await call("/api/search", { vector: vec(1), text: "混合检索", k: 3 });
console.log("search:", JSON.stringify(hit.rows.map((r) => ({ id: r.id, rank: r.rank, d: r.distance }))), `${hit.searchMs}ms`, hit.fusion);
if (hit.rows.length === 0) throw new Error("hybrid search returned no rows");
if (hit.rows[0].id !== "s-1") throw new Error(`unexpected top hit: ${hit.rows[0].id}`);

const reset = await call("/api/reset", {});
console.log("reset:", JSON.stringify(reset));
const after = await call("/api/health");
if (after.docs !== 0) throw new Error(`reset left ${after.docs} docs`);
console.log("SMOKE OK");
