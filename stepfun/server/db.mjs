// seekdb lifecycle + collection management for the mini knowledge base.
// Only storage / retrieval lives here: no embedding is computed on the server.
import { seekdb } from "@oceanbase/seekdb-node";
import {
  SeekdbClient,
  FulltextIndexConfig,
  VectorIndexConfig,
} from "seekdb";

export const DIM = 384; // all-MiniLM-L6-v2 sentence embedding size
export const KB_COLLECTION = "kb_docs";
const FULLTEXT_ANALYZER = "ngram";
const NGRAM_SIZE = 2; // works for CJK and for latin bigrams

let handle = null;
let client = null;
let openPromise = null;
let endpoint = null;
let serverVersion = null;
let dbDirPath = null;

/** Point the module at a data directory and open seekdb lazily/once. */
export async function initDatabase(dbDir) {
  dbDirPath = dbDir;
  return openDatabase({ dbDir });
}

function collectionConfig() {
  return {
    hnsw: { dimension: DIM, distance: "l2" },
    fulltextConfig: { analyzer: FULLTEXT_ANALYZER, properties: { ngram_token_size: NGRAM_SIZE } },
  };
}

export async function openDatabase({ dbDir }) {
  if (openPromise) return openPromise;
  dbDirPath = dbDir ?? dbDirPath;
  openPromise = (async () => {
    const t0 = Date.now();
    handle = await seekdb.open(dbDirPath, {
      parameters: { memory_budget: "2G", mysql_port_mode: "random" },
    });
    endpoint = handle.connectionOptions();
    if (endpoint.transport !== "tcp") {
      throw new Error(`unexpected seekdb transport: ${endpoint.transport}`);
    }
    client = new SeekdbClient({
      host: endpoint.host,
      port: endpoint.port,
      user: endpoint.user,
      password: "",
      database: "test",
    });
    try {
      const rows = await client.execute("SELECT VERSION() AS v");
      serverVersion = rows?.[0]?.v ?? null;
    } catch {
      serverVersion = "unknown";
    }
    await ensureCollection(KB_COLLECTION);
    return { bootMs: Date.now() - t0, endpoint, serverVersion };
  })();
  return openPromise;
}

export function dbInfo() {
  return {
    transport: endpoint?.transport ?? null,
    host: endpoint?.host ?? null,
    port: endpoint?.port ?? null,
    user: endpoint?.user ?? null,
    engine: serverVersion,
    dim: DIM,
    analyzer: `${FULLTEXT_ANALYZER}(${NGRAM_SIZE})`,
  };
}

// seekdb-js keeps collection metadata in `sdk_collections`; a v2 collection table is
// named `c$v2$<collection_id>`. We touch these names only to recover from an orphaned
// metadata row (see recoverOrphanMetadata), never to implement retrieval ourselves.
const META_TABLE = "sdk_collections";
const V2_PREFIX = "c$v2$";
const V1_PREFIX = "c$v1$";

async function metadataRows(name) {
  try {
    const rows = await client.execute(
      `SELECT collection_id FROM ${META_TABLE} WHERE collection_name = ?`,
      [name],
    );
    return rows ?? [];
  } catch {
    return [];
  }
}

/**
 * seekdb v1.4 can leave a `sdk_collections` row behind while its table is already gone
 * (for example after an interrupted drop). Recreating the same collection name then
 * fails with "Duplicate entry ... for key 'idx_name'". Remove only the metadata rows
 * whose backing table no longer exists.
 */
async function recoverOrphanMetadata(name) {
  const rows = await metadataRows(name);
  let removed = 0;
  for (const row of rows) {
    const table = `${V2_PREFIX}${row.collection_id}`;
    const present = await client.execute(`SHOW TABLES LIKE ?`, [table]);
    if (!present || present.length === 0) {
      await client.execute(`DELETE FROM ${META_TABLE} WHERE collection_id = ?`, [row.collection_id]);
      removed += 1;
    }
  }
  const legacy = `${V1_PREFIX}${name}`;
  const legacyPresent = await client.execute(`SHOW TABLES LIKE ?`, [legacy]);
  if (!legacyPresent || legacyPresent.length === 0) {
    await client.execute(`DELETE FROM ${META_TABLE} WHERE collection_name = ?`, [name]);
  }
  return removed;
}

async function ensureCollectionNamed(name, configuration) {
  const exists = await client.hasCollection({ name });
  if (exists) return client.getCollection({ name, embeddingFunction: null });
  await recoverOrphanMetadata(name);
  return client.createCollection({ name, embeddingFunction: null, configuration });
}

async function ensureCollection(name) {
  return ensureCollectionNamed(name, collectionConfig());
}

/**
 * Empty a collection in place.
 * seekdb-js `deleteCollection()` drops the table via name+collectionId metadata; in
 * seekdb v1.4 the metadata row can outlive the table, so recreating the same name fails
 * with a duplicate metadata entry. Deleting the rows keeps collection identity stable,
 * which is exactly what "reset the knowledge base" should mean here.
 */
async function tableCount(col) {
  const rows = await client.execute(`SELECT COUNT(*) AS c FROM \`${V2_PREFIX}${col.collectionId}\``);
  return Number(rows?.[0]?.c ?? 0);
}

async function clearCollection(col) {
  const before = await tableCount(col);
  if (before === 0) return 0;
  await client.execute(`DELETE FROM \`${V2_PREFIX}${col.collectionId}\``);
  return before;
}

export async function getKbCollection() {
  await openDatabase({ dbDir: dbDirPath });
  return client.getCollection({ name: KB_COLLECTION, embeddingFunction: null });
}

export async function resetKbCollection() {
  await openDatabase({ dbDir: dbDirPath });
  const col = await ensureCollection(KB_COLLECTION);
  const before = await col.count();
  const removed = await clearCollection(col);
  const after = await col.count();
  try {
    await col.refresh_index();
  } catch {
    /* optional */
  }
  return { name: KB_COLLECTION, before, removed, count: after, dim: DIM };
}

export async function addDocuments({ docs }) {
  const t0 = Date.now();
  const col = await getKbCollection();
  await col.add({
    ids: docs.map((d) => d.id),
    embeddings: docs.map((d) => d.vector),
    documents: docs.map((d) => d.text),
    metadatas: docs.map((d) => ({ title: d.title ?? "", source: d.source ?? "", ...(d.meta ?? {}) })),
  });
  const writeMs = Date.now() - t0;
  const t1 = Date.now();
  let refresh = "skipped";
  try {
    await col.refresh_index();
    refresh = "ok";
  } catch (err) {
    refresh = `unsupported: ${err?.message ?? err}`;
  }
  return {
    added: docs.length,
    total: await col.count(),
    writeMs,
    refreshMs: Date.now() - t1,
    refresh,
  };
}

export async function hybridSearch({ vector, text, k = 10, rrfWindow = 60, rrfConstant = 60 }) {
  const t0 = Date.now();
  const col = await getKbCollection();
  const options = {
    knn: { queryEmbeddings: vector, nResults: Math.max(k, 10) },
    rank: { rrf: { rankWindowSize: rrfWindow, rankConstant: rrfConstant } },
    nResults: k,
    // hybridSearch builds search_parm and lets seekdb run DBMS_HYBRID_SEARCH; no client side fusion
  };
  if (text && text.trim()) {
    options.query = { whereDocument: { $contains: text.trim() }, nResults: Math.max(k, 10) };
  }
  const res = await col.hybridSearch(options);
  const ids = res.ids?.[0] ?? [];
  const documents = res.documents?.[0] ?? [];
  const metadatas = res.metadatas?.[0] ?? [];
  const distances = res.distances?.[0] ?? [];
  const rows = ids.map((id, i) => ({
    id,
    text: documents[i] ?? "",
    title: metadatas?.[i]?.title ?? "",
    source: metadatas?.[i]?.source ?? "",
    distance: typeof distances[i] === "number" ? distances[i] : null,
    rank: i + 1,
  }));
  return { rows, searchMs: Date.now() - t0, fusion: "rrf(seekdb DBMS_HYBRID_SEARCH)" };
}

export async function kbCount() {
  const col = await getKbCollection();
  return col.count();
}

// --- evaluation collections (used only for the quantization quality comparison) ---
export async function indexEvalDocs({ tag, docs }) {
  await openDatabase({ dbDir: dbDirPath });
  const name = `kb_eval_${tag}`;
  const col = await ensureCollectionNamed(name, { hnsw: { dimension: DIM, distance: "l2" } });
  await clearCollection(col);
  await col.add({
    ids: docs.map((d) => d.id),
    embeddings: docs.map((d) => d.vector),
    documents: docs.map((d) => d.text ?? ""),
    metadatas: docs.map((d) => ({ title: d.title ?? "" })),
  });
  try {
    await col.refresh_index();
  } catch {
    /* optional */
  }
  return { collection: name, count: await col.count() };
}

export async function queryEval({ tag, vector, k = 5 }) {
  await openDatabase({ dbDir: dbDirPath });
  const col = await client.getCollection({ name: `kb_eval_${tag}`, embeddingFunction: null });
  const res = await col.query({ queryEmbeddings: vector, nResults: k, include: ["documents", "distances", "metadatas"] });
  return {
    ids: res.ids?.[0] ?? [],
    documents: res.documents?.[0] ?? [],
    distances: res.distances?.[0] ?? [],
  };
}

export async function closeDatabase() {
  try {
    if (client?.close) await client.close();
  } catch {
    /* ignore */
  }
  try {
    handle?.close();
  } catch {
    /* ignore */
  }
  handle = null;
  client = null;
  openPromise = null;
}

export { FulltextIndexConfig, VectorIndexConfig };
