import { initDatabase, addDocuments, hybridSearch, resetKbCollection, kbCount, closeDatabase } from "../server/db.mjs";

const vec = (n) => Array.from({ length: 384 }, (_, i) => (i === n ? 1 : 0.01));
await initDatabase("./.seekdb");
await resetKbCollection();

await addDocuments({ docs: [{ id: "a", title: "a", text: "seekdb hybrid search alpha", vector: vec(0) }] });
await addDocuments({ docs: [{ id: "b", title: "b", text: "browser wasm fallback beta", vector: vec(1) }] });
console.log("count after 2 incremental adds:", await kbCount());
const before = await hybridSearch({ vector: vec(0), text: "seekdb", k: 5 });
console.log("search before reset:", before.rows.map((r) => r.id).join(","));

const r = await resetKbCollection();
console.log("reset:", JSON.stringify(r));
const after = await hybridSearch({ vector: vec(0), text: "seekdb", k: 5 });
console.log("search after reset:", after.rows.length, "rows ->", after.rows.map((x) => x.id).join(",") || "(empty)");
console.log("count after reset:", await kbCount());
await closeDatabase();
process.exit(0);
