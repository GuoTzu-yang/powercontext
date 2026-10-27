// Download the ONNX model files used by the demo so the app can run fully offline.
//   node scripts/fetch-models.mjs            (hf-mirror.com, then huggingface.co)
//   node scripts/fetch-models.mjs --fp32     (also fetch the fp32 baseline, needed for the
//                                             quantization size/quality comparison)
import { mkdir, writeFile, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const MODEL = "Xenova/all-MiniLM-L6-v2";
const DEST = resolve(root, "app/public/models", MODEL);

const SOURCES = ["https://hf-mirror.com", "https://huggingface.co"];
const wantFp32 = process.argv.includes("--fp32");

const FILES = [
  "config.json",
  "tokenizer.json",
  "tokenizer_config.json",
  "onnx/model_quantized.onnx", // INT8
  "onnx/model_fp16.onnx", // FP16
  ...(wantFp32 ? ["onnx/model.onnx"] : []),
];

async function fetchOne(file) {
  const target = join(DEST, file);
  try {
    const s = await stat(target);
    if (s.size > 1024) {
      console.log(`skip  ${file} (${(s.size / 1048576).toFixed(2)} MB already present)`);
      return;
    }
  } catch {
    /* not present */
  }
  await mkdir(dirname(target), { recursive: true });
  let lastErr;
  for (const base of SOURCES) {
    const url = `${base}/${MODEL}/resolve/main/${file}`;
    try {
      const t0 = Date.now();
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = Buffer.from(await res.arrayBuffer());
      await writeFile(target, buf);
      console.log(`ok    ${file} ${(buf.length / 1048576).toFixed(2)} MB from ${base} in ${Date.now() - t0} ms`);
      return;
    } catch (err) {
      lastErr = err;
      console.log(`fail  ${file} from ${base}: ${err.message}`);
    }
  }
  throw lastErr;
}

for (const file of FILES) await fetchOne(file);
console.log(`\nmodels ready in ${DEST}`);
