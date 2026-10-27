// Embedding worker: all inference happens here, inside the user's browser.
// WebGPU is preferred; if it is unavailable (or fails to initialise) we fall back to WASM.
import { env, pipeline } from "@huggingface/transformers";

const MODEL_ID = "Xenova/all-MiniLM-L6-v2";

// Model sources are tried in order. A local copy under /models makes the app fully offline.
const SOURCES = [
  { id: "local", remoteHost: null, localOnly: true },
  { id: "hf-mirror", remoteHost: "https://hf-mirror.com" },
  { id: "huggingface", remoteHost: "https://huggingface.co" },
];

env.allowRemoteModels = true;
env.useBrowserCache = true;
env.backends.onnx.wasm.numThreads = Math.min(4, Math.max(1, (navigator.hardwareConcurrency ?? 4) - 1));
env.backends.onnx.wasm.simd = true;

let extractor = null;
let active = null; // { device, dtype, source }

function post(msg) {
  self.postMessage(msg);
}

async function hasWebGPU() {
  if (!("gpu" in navigator)) return false;
  try {
    const adapter = await navigator.gpu.requestAdapter();
    return Boolean(adapter);
  } catch {
    return false;
  }
}

async function load(device, dtype, sourceId) {
  const source = SOURCES.find((s) => s.id === sourceId) ?? SOURCES[1];
  // local first when the caller asks for "auto"
  const tries = sourceId === "auto" ? SOURCES : [source];
  let lastErr = null;
  for (const s of tries) {
    try {
      // A local-only attempt must never fall through to the network: that keeps the
      // "offline copy first" probe instant instead of stalling on a large download.
      env.allowLocalModels = true;
      env.localModelPath = "/models/";
      env.allowRemoteModels = !s.localOnly;
      if (!s.localOnly) env.remoteHost = s.remoteHost;
      const t0 = performance.now();
      const pipe = await pipeline("feature-extraction", MODEL_ID, {
        device,
        dtype,
        progress_callback: (p) => {
          if (p.status === "progress" && p.file) {
            post({ type: "progress", file: p.file, loaded: p.loaded, total: p.total, progress: p.progress });
          }
        },
      });
      const loadMs = Math.round(performance.now() - t0);
      return { pipe, source: s.id, loadMs };
    } catch (err) {
      lastErr = err;
      if (s.localOnly) continue; // no local copy: silently move on
    }
  }
  throw lastErr ?? new Error("no model source reachable");
}

async function embedBatch(texts, batchSize) {
  const out = [];
  for (let i = 0; i < texts.length; i += batchSize) {
    const slice = texts.slice(i, i + batchSize);
    const tensor = await extractor(slice, { pooling: "mean", normalize: true });
    const [n, dim] = tensor.dims;
    const data = tensor.data;
    for (let r = 0; r < n; r++) out.push(Array.from(data.slice(r * dim, (r + 1) * dim)));
    post({ type: "embed-progress", done: Math.min(i + batchSize, texts.length), total: texts.length });
  }
  return out;
}

self.onmessage = async (event) => {
  const msg = event.data;
  try {
    if (msg.type === "init") {
      const wanted = msg.device ?? "auto";
      const webgpu = wanted === "wasm" ? false : await hasWebGPU();
      const plan = [];
      if (webgpu) {
        plan.push({ device: "webgpu", dtype: msg.dtype ?? "fp16" });
        plan.push({ device: "webgpu", dtype: "fp32" });
      }
      const wasmDtype = msg.dtype && msg.dtype !== "auto" ? msg.dtype : "q8";
      plan.push({ device: "wasm", dtype: wasmDtype === "fp16" || wasmDtype === "fp32" ? wasmDtype : "q8" });
      if (wanted === "wasm") plan.length = Math.max(1, plan.length - (webgpu ? 2 : 0));

      let lastErr = null;
      for (const step of plan) {
        try {
          const { pipe, source, loadMs } = await load(step.device, step.dtype, msg.sourceId ?? "auto");
          extractor = pipe;
          active = { ...step, source };
          post({
            type: "ready",
            backend: step.device,
            dtype: step.dtype,
            source,
            model: MODEL_ID,
            webgpuAvailable: webgpu,
            loadMs,
            dim: 384,
            fallback: wanted !== "auto",
          });
          return;
        } catch (err) {
          lastErr = err;
        }
      }
      throw lastErr ?? new Error("model initialisation failed");
    }

    if (msg.type === "embed") {
      if (!extractor) throw new Error("embedder not initialised");
      const t0 = performance.now();
      const vectors = await embedBatch(msg.texts, msg.batchSize ?? 8);
      post({
        type: "embedded",
        id: msg.id,
        vectors,
        embedMs: Math.round(performance.now() - t0),
        backend: active?.device,
        dtype: active?.dtype,
      });
    }
  } catch (err) {
    post({ type: "error", id: msg.id, message: String(err?.message ?? err) });
  }
};
