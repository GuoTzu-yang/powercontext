// Thin typed wrapper around the embedding Web Worker.
import EmbedWorker from "./worker.ts?worker";

export type Backend = "webgpu" | "wasm";
export type Dtype = "fp32" | "fp16" | "q8" | "q4";

export type ReadyInfo = {
  backend: Backend;
  dtype: Dtype;
  source: string;
  model: string;
  webgpuAvailable: boolean;
  loadMs: number;
  dim: number;
  fallback: boolean;
};

type Pending = { resolve: (v: any) => void; reject: (e: unknown) => void };

export class Embedder {
  private worker: Worker;
  private pending = new Map<string, Pending>();
  private seq = 0;
  ready: ReadyInfo | null = null;
  onProgress: ((p: { file: string; progress: number }) => void) | null = null;
  onEmbedProgress: ((p: { done: number; total: number }) => void) | null = null;

  constructor() {
    this.worker = new EmbedWorker();
    this.worker.onmessage = (event) => this.handle(event.data);
  }

  private handle(msg: any) {
    if (msg.type === "progress") {
      this.onProgress?.({ file: msg.file, progress: Math.min(100, Math.round((msg.progress ?? 0) * 100)) });
      return;
    }
    if (msg.type === "embed-progress") {
      this.onEmbedProgress?.({ done: msg.done, total: msg.total });
      return;
    }
    if (msg.type === "ready") {
      this.ready = msg as ReadyInfo;
      this.settle("init", { ok: true, info: this.ready });
      return;
    }
    if (msg.type === "embedded") {
      this.settle(msg.id, { ok: true, vectors: msg.vectors, embedMs: msg.embedMs });
      return;
    }
    if (msg.type === "error") {
      this.settle(msg.id ?? "init", { ok: false, error: msg.message });
    }
  }

  private settle(id: string, payload: any) {
    const p = this.pending.get(id);
    if (!p) return;
    this.pending.delete(id);
    payload.ok ? p.resolve(payload) : p.reject(new Error(payload.error));
  }

  private call<T>(id: string, message: any): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as any, reject });
      this.worker.postMessage(message);
    });
  }

  async init(opts: { device?: "auto" | Backend; dtype?: Dtype | "auto"; sourceId?: string } = {}) {
    const res = await this.call<{ info: ReadyInfo }>("init", { type: "init", ...opts });
    this.ready = res.info;
    return res.info;
  }

  async embed(texts: string[], batchSize = 8): Promise<{ vectors: number[][]; embedMs: number }> {
    const id = `e${++this.seq}`;
    const res = await this.call<{ vectors: number[][]; embedMs: number }>(id, {
      type: "embed",
      id,
      texts,
      batchSize,
    });
    return { vectors: res.vectors, embedMs: res.embedMs };
  }

  dispose() {
    this.worker.terminate();
  }
}

/** Split pasted/uploaded text into overlapping chunks suitable for embedding. */
export function chunkText(text: string, maxLen = 220, overlap = 40): string[] {
  const clean = text.replace(/\r\n/g, "\n").trim();
  if (!clean) return [];
  const paragraphs = clean.split(/\n{2,}|\n/).map((p) => p.trim()).filter(Boolean);
  const chunks: string[] = [];
  for (const para of paragraphs) {
    if (para.length <= maxLen) {
      chunks.push(para);
      continue;
    }
    for (let start = 0; start < para.length; start += maxLen - overlap) {
      chunks.push(para.slice(start, start + maxLen));
      if (start + maxLen >= para.length) break;
    }
  }
  return chunks.slice(0, 400);
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  if (!res.ok || json.ok === false) throw new Error(json.error ?? `HTTP ${res.status}`);
  return json as T;
}

/** Byte size of one precision's ONNX weights, probed from local copy or the model host. */
export async function probeModelSize(dtype: Dtype): Promise<number | null> {
  const file =
    dtype === "q8" ? "onnx/model_quantized.onnx" : dtype === "fp16" ? "onnx/model_fp16.onnx" : dtype === "q4" ? "onnx/model_q4.onnx" : "onnx/model.onnx";
  // local copy lives at /models/<model>/<file>; remote hosts use the HF resolve path
  const bases = [
    { base: "/models/Xenova/all-MiniLM-L6-v2/", file },
    { base: "https://hf-mirror.com/Xenova/all-MiniLM-L6-v2/resolve/main/", file },
    { base: "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/", file },
  ];
  for (const { base, file: f } of bases) {
    try {
      const res = await fetch(base + f, { method: "HEAD" });
      if (!res.ok) continue;
      const len = Number(res.headers.get("content-length"));
      if (len > 0) return len;
      const probe = await fetch(base + f, { headers: { range: "bytes=0-0" } });
      const range = probe.headers.get("content-range");
      const total = range ? Number(range.split("/")[1]) : 0;
      if (total > 0) return total;
    } catch {
      /* try next base */
    }
  }
  return null;
}
