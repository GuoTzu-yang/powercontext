// Storage/retrieval API for the on-device knowledge base.
// The server never runs inference: it only moves vectors the browser already computed
// into seekdb, and asks seekdb to perform the hybrid retrieval.
import express from "express";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { existsSync } from "node:fs";
import {
  initDatabase,
  dbInfo,
  addDocuments,
  hybridSearch,
  kbCount,
  resetKbCollection,
  indexEvalDocs,
  queryEval,
  closeDatabase,
  DIM,
} from "./db.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const dbDir = resolve(root, process.env.SEEPDB_DIR ?? ".seekdb");
const port = Number(process.env.PORT ?? 8791);

const app = express();
app.use(express.json({ limit: "64mb" }));

function fail(res, code, err) {
  res.status(code).json({ ok: false, error: String(err?.message ?? err) });
}

app.get("/api/health", async (_req, res) => {
  try {
    res.json({ ok: true, engine: dbInfo(), docs: await kbCount(), dim: DIM, serverTime: Date.now() });
  } catch (err) {
    fail(res, 500, err);
  }
});

app.post("/api/reset", async (_req, res) => {
  try {
    res.json({ ok: true, ...(await resetKbCollection()) });
  } catch (err) {
    fail(res, 500, err);
  }
});

app.post("/api/docs", async (req, res) => {
  const docs = req.body?.docs;
  if (!Array.isArray(docs) || docs.length === 0) return fail(res, 400, "docs[] required");
  for (const d of docs) {
    if (!d.id || !Array.isArray(d.vector) || d.vector.length !== DIM) {
      return fail(res, 400, `doc ${d?.id ?? "?"} needs a ${DIM}-dim vector computed in the browser`);
    }
  }
  try {
    const t0 = Date.now();
    const result = await addDocuments({ docs });
    res.json({ ok: true, ...result, totalMs: Date.now() - t0 });
  } catch (err) {
    fail(res, 500, err);
  }
});

app.post("/api/search", async (req, res) => {
  const { vector, text = "", k = 10, rrfWindow = 60, rrfConstant = 60 } = req.body ?? {};
  if (!Array.isArray(vector) || vector.length !== DIM) {
    return fail(res, 400, `a ${DIM}-dim query vector is required (computed in the browser)`);
  }
  try {
    const result = await hybridSearch({ vector, text, k, rrfWindow, rrfConstant });
    res.json({ ok: true, ...result, queryText: text });
  } catch (err) {
    fail(res, 500, err);
  }
});

app.post("/api/eval/index", async (req, res) => {
  const { tag, docs } = req.body ?? {};
  if (!tag || !Array.isArray(docs)) return fail(res, 400, "tag and docs[] required");
  try {
    res.json({ ok: true, ...(await indexEvalDocs({ tag, docs })) });
  } catch (err) {
    fail(res, 500, err);
  }
});

app.post("/api/eval/query", async (req, res) => {
  const { tag, vector, k = 5 } = req.body ?? {};
  if (!tag || !Array.isArray(vector)) return fail(res, 400, "tag and vector required");
  try {
    res.json({ ok: true, ...(await queryEval({ tag, vector, k })) });
  } catch (err) {
    fail(res, 500, err);
  }
});

const dist = join(root, "server", "public");
if (existsSync(dist)) {
  app.use(express.static(dist));
  app.get("*", (_req, res) => res.sendFile(join(dist, "index.html")));
} else {
  app.get("/", (_req, res) =>
    res.type("text/plain").send("UI bundle missing. Run `npm run build`, or `npm run dev` for the Vite dev server."),
  );
}

const boot = await initDatabase(dbDir);
console.log(
  `[seekdb] engine=${boot.serverVersion} endpoint=${boot.endpoint.host}:${boot.endpoint.port} boot=${boot.bootMs}ms dir=${dbDir}`,
);
app.listen(port, "127.0.0.1", () => console.log(`[api] http://127.0.0.1:${port}`));

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    await closeDatabase();
    process.exit(0);
  });
}
