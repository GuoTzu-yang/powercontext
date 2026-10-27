import "./style.css";
import { Embedder, chunkText, api, probeModelSize, type Dtype, type ReadyInfo } from "./embedder";
import { SAMPLE_DOCS, EVAL_QUERIES, type Doc } from "./data";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const dom = {
  backend: $<HTMLSpanElement>("badge-backend"),
  model: $<HTMLSpanElement>("badge-model"),
  db: $<HTMLSpanElement>("badge-db"),
  docs: $<HTMLSpanElement>("badge-docs"),
  sample: $<HTMLButtonElement>("btn-sample"),
  file: $<HTMLInputElement>("file-input"),
  paste: $<HTMLTextAreaElement>("paste"),
  batch: $<HTMLInputElement>("batch-size"),
  pasteBtn: $<HTMLButtonElement>("btn-paste"),
  reset: $<HTMLButtonElement>("btn-reset"),
  importStatus: $<HTMLSpanElement>("import-status"),
  importBar: $<HTMLDivElement>("import-bar"),
  query: $<HTMLInputElement>("query"),
  topk: $<HTMLInputElement>("topk"),
  search: $<HTMLButtonElement>("btn-search"),
  searchMeta: $<HTMLDivElement>("search-meta"),
  results: $<HTMLOListElement>("results"),
  quantBtn: $<HTMLButtonElement>("btn-quant"),
  quantStatus: $<HTMLSpanElement>("quant-status"),
  quantBody: $<HTMLTableElement>("quant-table").querySelector("tbody")!,
  perfBody: $<HTMLTableElement>("perf-table").querySelector("tbody")!,
};

const embedder = new Embedder();
let ready: ReadyInfo | null = null;
const perf: Record<string, number | string> = {};

function setBadge(el: HTMLSpanElement, text: string, cls: string) {
  el.textContent = text;
  el.className = `badge ${cls}`;
}

function perfRow(stage: string, value: number | string, note: string) {
  perf[stage] = value;
  const row = dom.perfBody.querySelector(`tr[data-stage="${stage}"]`);
  const html = `<td>${stage}</td><td class="num">${
    typeof value === "number" ? `${value.toFixed(1)} ms` : value
  }</td><td class="hint">${note}</td>`;
  if (row) row.innerHTML = html;
  else dom.perfBody.insertAdjacentHTML("beforeend", `<tr data-stage="${stage}">${html}</tr>`);
}

function fmtBytes(n: number | null): string {
  if (!n) return "—";
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`;
}

async function refreshDbBadge() {
  try {
    const health = await api<{ engine: any; docs: number }>("/api/health");
    setBadge(
      dom.db,
      `seekdb ${health.engine.engine ?? ""} · ${health.engine.host}:${health.engine.port} · ${health.engine.analyzer}`,
      "ready",
    );
    dom.docs.textContent = `文档数：${health.docs}`;
    return health;
  } catch (err) {
    setBadge(dom.db, `seekdb：连接失败（${(err as Error).message}）`, "err");
    return null;
  }
}

async function bootBackend() {
  setBadge(dom.backend, "推理后端：加载模型中…", "pending");
  embedder.onProgress = (p) =>
    setBadge(dom.backend, `推理后端：加载模型 ${p.file} ${p.progress}%`, "pending");
  try {
    ready = await embedder.init({ device: "auto" });
    setBadge(
      dom.backend,
      ready.backend === "webgpu"
        ? `推理后端：WebGPU · ${ready.dtype}`
        : `推理后端：WASM（未检测到可用 WebGPU，已自动降级）· ${ready.dtype}`,
      ready.backend === "webgpu" ? "gpu" : "ready",
    );
    dom.model.textContent = `模型：${ready.model} · ${ready.dtype} · 来源 ${ready.source}`;
    perfRow("模型加载", ready.loadMs, `后端 ${ready.backend} / ${ready.dtype} / 来源 ${ready.source}`);
    return ready;
  } catch (err) {
    setBadge(dom.backend, `推理后端：初始化失败（${(err as Error).message}）`, "err");
    throw err;
  }
}

type IngestDoc = Doc & { vector: number[] };

async function ingest(docs: Doc[]) {
  if (!ready) return;
  const batchSize = Number(dom.batch.value) || 8;
  dom.sample.disabled = true;
  dom.pasteBtn.disabled = true;
  dom.importBar.style.width = "0%";
  dom.importStatus.textContent = `向量化 ${docs.length} 段…`;

  const t0 = performance.now();
  const { vectors, embedMs } = await embedder.embed(
    docs.map((d) => d.text),
    batchSize,
  );
  perfRow("文档向量化", embedMs, `${docs.length} 段 / batch=${batchSize} / ${ready.backend} ${ready.dtype}`);

  const payload: IngestDoc[] = docs.map((d, i) => ({ ...d, vector: vectors[i] }));
  dom.importStatus.textContent = "写入 seekdb…";
  const res = await api<{ added: number; total: number; writeMs: number; refreshMs: number; refresh: string }>(
    "/api/docs",
    { docs: payload },
  );
  dom.importBar.style.width = "100%";
  const e2e = performance.now() - t0;
  perfRow("写入 seekdb", res.writeMs, `incremental add ${res.added} 条`);
  perfRow("索引刷新", res.refreshMs, `refresh_index → ${res.refresh}`);
  perfRow("导入端到端", e2e, "点击导入到可检索");
  dom.importStatus.textContent = `已导入 ${res.added} 段，当前共 ${res.total} 段（刷新 ${res.refresh}）`;
  dom.sample.disabled = false;
  dom.pasteBtn.disabled = false;
  await refreshDbBadge();
}

async function doSearch() {
  if (!ready) return;
  const text = dom.query.value.trim();
  if (!text) return;
  dom.search.disabled = true;
  dom.searchMeta.textContent = "本地生成查询向量…";
  const t0 = performance.now();
  try {
    const { vectors, embedMs } = await embedder.embed([text], 1);
    perfRow("查询向量化", embedMs, `1 条查询 / ${ready.backend} ${ready.dtype}`);
    const res = await api<{ rows: any[]; searchMs: number; fusion: string }>("/api/search", {
      vector: vectors[0],
      text,
      k: Number(dom.topk.value) || 5,
    });
    perfRow("seekdb 检索", res.searchMs, res.fusion);
    perfRow("查询端到端", performance.now() - t0, "点击检索到结果渲染");
    dom.searchMeta.textContent = `返回 ${res.rows.length} 条 · 融合方式 ${res.fusion} · seekdb 耗时 ${res.searchMs} ms · 端到端 ${(
      performance.now() - t0
    ).toFixed(1)} ms`;
    dom.results.innerHTML = res.rows
      .map(
        (r) => `<li>
          <div class="title">${escapeHtml(r.title || r.id)}</div>
          <div class="meta">#${r.rank} · id=${escapeHtml(r.id)} · distance=${
            r.distance === null ? "—" : r.distance.toFixed(4)
          } · source=${escapeHtml(r.source || "-")}</div>
          <div class="text">${escapeHtml(r.text)}</div>
        </li>`,
      )
      .join("");
  } catch (err) {
    dom.searchMeta.textContent = `检索失败：${(err as Error).message}`;
  } finally {
    dom.search.disabled = false;
  }
}

function escapeHtml(s: string) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function ndcg(ranked: string[], gold: string[], k: number) {
  let dcg = 0;
  ranked.slice(0, k).forEach((id, i) => {
    if (gold.includes(id)) dcg += 1 / Math.log2(i + 2);
  });
  let idcg = 0;
  for (let i = 0; i < Math.min(gold.length, k); i++) idcg += 1 / Math.log2(i + 2);
  return idcg === 0 ? 0 : dcg / idcg;
}

function recall(ranked: string[], gold: string[], k: number) {
  return ranked.slice(0, k).some((id) => gold.includes(id)) ? 1 : 0;
}

const DTYPES: Dtype[] = ["fp32", "q8", "fp16"];

type QuantRow = { dtype: Dtype; size: number | null; ms: number | null; recall: number | null; ndcg: number | null; note: string };

async function runQuantComparison() {
  dom.quantBtn.disabled = true;
  dom.quantBody.innerHTML = "";
  const rows: QuantRow[] = [];

  // 1) 先把三档精度对应的模型文件体积测出来 —— 只依赖 HTTP 字节数，与运行时能否加载无关。
  for (const dtype of DTYPES) {
    dom.quantStatus.textContent = `探测 ${dtype} 模型体积…`;
    const size = await probeModelSize(dtype);
    rows.push({ dtype, size, ms: null, recall: null, ndcg: null, note: "待测" });
    renderQuantRows(rows);
  }

  // 2) 再逐档在同一后端（WASM）下跑评测集；某档运行时加载失败只标记该行，不影响其它档。
  for (const row of rows) {
    const dtype = row.dtype;
    try {
      dom.quantStatus.textContent = `切换到 WASM · ${dtype} …`;
      await embedder.init({ device: "wasm", dtype });
      dom.quantStatus.textContent = `向量化评测语料（${dtype}）…`;
      const { vectors, embedMs } = await embedder.embed(
        SAMPLE_DOCS.map((d) => d.text),
        8,
      );
      await api("/api/eval/index", {
        tag: dtype,
        docs: SAMPLE_DOCS.map((d, i) => ({ id: d.id, title: d.title, text: d.text, vector: vectors[i] })),
      });
      dom.quantStatus.textContent = `在 seekdb 中检索评测查询（${dtype}）…`;
      let r3 = 0;
      let n3 = 0;
      for (const item of EVAL_QUERIES) {
        const { vectors: qv } = await embedder.embed([item.query], 1);
        const res = await api<{ ids: string[] }>("/api/eval/query", { tag: dtype, vector: qv[0], k: 3 });
        r3 += recall(res.ids, item.gold, 3);
        n3 += ndcg(res.ids, item.gold, 3);
      }
      row.ms = embedMs;
      row.recall = r3 / EVAL_QUERIES.length;
      row.ndcg = n3 / EVAL_QUERIES.length;
      row.note = `已测（${EVAL_QUERIES.length} 条查询 @3）`;
    } catch (err) {
      row.note = `该精度在本机运行时加载失败：${(err as Error).message.slice(0, 80)}`;
    }
    renderQuantRows(rows);
  }

  dom.quantStatus.textContent = "对比完成，正在恢复默认后端…";
  ready = await embedder.init({ device: "auto" });
  setBadge(
    dom.backend,
    ready.backend === "webgpu"
      ? `推理后端：WebGPU · ${ready.dtype}`
      : `推理后端：WASM（未检测到可用 WebGPU，已自动降级）· ${ready.dtype}`,
    ready.backend === "webgpu" ? "gpu" : "ready",
  );
  const okRows = rows.filter((r) => r.recall !== null).length;
  let hybridNote = "";
  // 纯向量检索与「向量+全文 RRF 混合检索」在同一评测集上对比：说明混合检索为什么值得。
  try {
    const health = await api<{ docs: number }>("/api/health");
    if (health.docs > 0) {
      await embedder.init({ device: "wasm", dtype: "q8" });
      let hit = 0;
      for (const item of EVAL_QUERIES) {
        const { vectors: qv } = await embedder.embed([item.query], 1);
        const res = await api<{ rows: { id: string }[] }>("/api/search", {
          vector: qv[0],
          text: item.query,
          k: 3,
        });
        if (res.rows.some((r) => item.gold.includes(r.id))) hit += 1;
      }
      hybridNote = `｜混合检索（向量+全文 RRF，q8）Recall@3 = ${((hit / EVAL_QUERIES.length) * 100).toFixed(0)}%`;
      ready = await embedder.init({ device: "auto" });
    } else {
      hybridNote = "｜导入示例语料后可额外测得混合检索 Recall@3";
    }
  } catch {
    /* 混合检索对比是可选项，失败不影响量化表格 */
  }
  dom.quantStatus.textContent = `对比完成：${okRows}/${rows.length} 档在当前运行时跑通；指标来自 seekdb 向量检索的排序结果${hybridNote}`;
  dom.quantBtn.disabled = false;
}

function renderQuantRows(rows: QuantRow[]) {
  const base = rows.find((r) => r.dtype === "fp32")?.size ?? null;
  dom.quantBody.innerHTML = rows
    .map((r) => {
      const ratio = base && r.size ? (base / r.size).toFixed(2) : "—";
      return `<tr>
        <td>${r.dtype === "q8" ? "q8 (INT8)" : r.dtype}</td>
        <td class="num">${fmtBytes(r.size)}</td>
        <td class="num">${ratio === "—" ? "—" : `${ratio}×`}</td>
        <td class="num">${r.ms === null ? "—" : `${r.ms.toFixed(0)} ms`}</td>
        <td class="num">${r.recall === null ? "—" : `${(r.recall * 100).toFixed(0)}%`}</td>
        <td class="num">${r.ndcg === null ? "—" : r.ndcg.toFixed(3)}</td>
        <td class="hint">${escapeHtml(r.note)}</td>
      </tr>`;
    })
    .join("");
}

// ---- wiring ----
dom.sample.addEventListener("click", () => ingest(SAMPLE_DOCS));

dom.pasteBtn.addEventListener("click", () => {
  const chunks = chunkText(dom.paste.value);
  if (!chunks.length) {
    dom.importStatus.textContent = "没有可导入的文本";
    return;
  }
  const stamp = Date.now().toString(36);
  ingest(
    chunks.map((text, i) => ({
      id: `paste-${stamp}-${i}`,
      title: `粘贴文本 #${i + 1}`,
      text,
      source: "paste",
    })),
  );
});

dom.paste.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) dom.pasteBtn.click();
});

dom.file.addEventListener("change", async () => {
  const files = Array.from(dom.file.files ?? []);
  const docs: Doc[] = [];
  for (const file of files) {
    const text = await file.text();
    chunkText(text).forEach((chunk, i) =>
      docs.push({
        id: `file-${file.name}-${i}`.replace(/\s+/g, "_"),
        title: `${file.name} #${i + 1}`,
        text: chunk,
        source: `file:${file.name}`,
      }),
    );
  }
  if (docs.length) await ingest(docs.slice(0, 200));
});

dom.reset.addEventListener("click", async () => {
  dom.reset.disabled = true;
  try {
    await api("/api/reset", {});
    dom.results.innerHTML = "";
    dom.searchMeta.textContent = "";
    dom.importStatus.textContent = "知识库已重置";
    dom.importBar.style.width = "0%";
    await refreshDbBadge();
  } finally {
    dom.reset.disabled = false;
  }
});

dom.search.addEventListener("click", doSearch);
dom.query.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});
dom.quantBtn.addEventListener("click", () => runQuantComparison().catch((err) => {
  dom.quantStatus.textContent = `量化对比失败：${err.message}`;
  dom.quantBtn.disabled = false;
}));

(async () => {
  await refreshDbBadge();
  try {
    await bootBackend();
  } catch {
    /* badge already shows the failure */
  }
})();

// expose a tiny hook for automated verification
(window as any).__kb = { embedder, ingest, doSearch, runQuantComparison, get ready() { return ready; } };
