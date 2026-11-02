// اختبار ذاتي للـ Worker ببدائل في الذاكرة:  node test.mjs
import assert from "node:assert/strict";
import { publishDue, cleanup, run, publishTimeFromKey } from "./src/index.js";

class MemBucket {
  constructor() { this.m = new Map(); }
  async put(k, v) { this.m.set(k, { v: typeof v === "string" ? v : String(v), uploaded: new Date() }); }
  async get(k) { const e = this.m.get(k); return e ? { json: async () => JSON.parse(e.v) } : null; }
  async delete(keys) { for (const k of [].concat(keys)) this.m.delete(k); }
  async list({ prefix }) {
    const objects = [...this.m].filter(([k]) => k.startsWith(prefix)).map(([key, e]) => ({ key, uploaded: e.uploaded }));
    return { objects, truncated: false };
  }
  age(k, hours) { this.m.get(k).uploaded = new Date(Date.now() - hours * 3600000); }
}
const item = (lang, over = {}) => JSON.stringify({
  lang, platforms: {
    youtube: { body: { platform: "youtube", idempotency_key: "k_yt" }, sent_at: null, attempts: 0 },
    facebook: { body: { platform: "facebook", idempotency_key: "k_fb" }, sent_at: null, attempts: 0 }, ...over },
});
const stamp = (d) => d.toISOString().replace(/[-:]/g, "").replace(/\.\d+/, "");
const now = new Date("2026-11-02T17:00:00Z");
const min = (n) => new Date(now.getTime() + n * 60000);

let calls = [];
const okFetch = async (url, o) => { calls.push({ url, body: JSON.parse(o.body) }); return { status: 200 }; };

// 1) key parsing
assert.equal(publishTimeFromKey("queue/20261102T170000Z_ar_short_5.json").toISOString(), "2026-11-02T17:00:00.000Z");
assert.equal(publishTimeFromKey("queue/garbage.json"), null);

// 2) due vs future vs missed
{
  const env = { BUCKET: new MemBucket(), MAKE_WEBHOOK_URL_AR: "https://make/ar", MAKE_AUTH_TOKEN: "tok", MAX_LATE_MIN: "180" };
  await env.BUCKET.put(`queue/${stamp(min(-1))}_ar_short_1.json`, item("ar"));        // مستحق
  await env.BUCKET.put(`queue/${stamp(min(+30))}_ar_short_2.json`, item("ar"));       // مستقبلي
  await env.BUCKET.put(`queue/${stamp(min(-200))}_ar_short_3.json`, item("ar"));      // فائت
  calls = [];
  const s = await publishDue(env, now, okFetch);
  assert.equal(s.sent, 2); assert.equal(calls.length, 2);
  assert.ok(calls.every((c) => c.url === "https://make/ar" && c.body.auth === "tok"));
  assert.deepEqual(calls.map((c) => c.body.platform).sort(), ["facebook", "youtube"]);
  assert.ok([...env.BUCKET.m.keys()].some((k) => k.startsWith("sent/") && k.endsWith("_ar_short_1.json")));
  assert.ok(env.BUCKET.m.has(`queue/${stamp(min(+30))}_ar_short_2.json`));             // بقي
  assert.ok([...env.BUCKET.m.keys()].some((k) => k.startsWith("failed/") && k.endsWith("_ar_short_3.json")));
  console.log("✅ due / future / missed");
}

// 3) إرسال جزئي: يوتيوب ينجح وفيسبوك يفشل → لا يتكرر يوتيوب عند المحاولة التالية
{
  const env = { BUCKET: new MemBucket(), MAKE_WEBHOOK_URL: "https://make/any" };
  const key = `queue/${stamp(min(-1))}_en_long_9.json`;
  await env.BUCKET.put(key, item("en"));
  let fbFails = true; calls = [];
  const f = async (url, o) => { const b = JSON.parse(o.body); calls.push(b.platform);
    return { status: b.platform === "facebook" && fbFails ? 500 : 200 }; };
  await publishDue(env, now, f);
  assert.deepEqual(calls.sort(), ["facebook", "youtube"]);
  let saved = JSON.parse(env.BUCKET.m.get(key).v);
  assert.ok(saved.platforms.youtube.sent_at && !saved.platforms.facebook.sent_at);
  assert.equal(saved.platforms.facebook.last_error, "HTTP 500");
  fbFails = false; calls = [];
  await publishDue(env, min(1), f);
  assert.deepEqual(calls, ["facebook"]);                                             // يوتيوب لم يُعَد
  assert.ok(!env.BUCKET.m.has(key) && [...env.BUCKET.m.keys()].some((k) => k.startsWith("sent/")));
  console.log("✅ partial failure → retry only the failed platform");
}

// 4) بلا webhook: لا يُرسل ولا يُنقل
{
  const env = { BUCKET: new MemBucket() };
  const key = `queue/${stamp(min(-1))}_fr_short_1.json`;
  await env.BUCKET.put(key, item("fr")); calls = [];
  await publishDue(env, now, okFetch);
  assert.equal(calls.length, 0); assert.ok(env.BUCKET.m.has(key));
  assert.equal(JSON.parse(env.BUCKET.m.get(key).v).platforms.youtube.last_error, "webhook secret missing");
  console.log("✅ missing webhook secret handled");
}

// 5) cleanup: يحذف > 48h ولا يلمس state/
{
  const env = { BUCKET: new MemBucket(), RETENTION_HOURS: "48" };
  for (const k of ["videos/ar/short/1_yt.mp4", "videos/ar/short/2_yt.mp4", "sent/a.json", "failed/b.json", "state/vsg_ar.db"]) await env.BUCKET.put(k, "x");
  for (const k of ["videos/ar/short/1_yt.mp4", "sent/a.json", "failed/b.json", "state/vsg_ar.db"]) env.BUCKET.age(k, 60);
  const n = await cleanup(env, new Date());
  assert.equal(n, 3);
  assert.deepEqual([...env.BUCKET.m.keys()].sort(), ["state/vsg_ar.db", "videos/ar/short/2_yt.mp4"]);
  console.log("✅ cleanup 48h keeps state/ and fresh files");
}

// 6) run(): التنظيف فقط عند الدقيقة 00
{
  const env = { BUCKET: new MemBucket() };
  await env.BUCKET.put("videos/x.mp4", "x"); env.BUCKET.age("videos/x.mp4", 100);
  await run(env, new Date("2026-11-02T17:05:00Z"), okFetch);
  assert.ok(env.BUCKET.m.has("videos/x.mp4"));
  await run(env, new Date("2026-11-02T18:00:00Z"), okFetch);
  assert.ok(!env.BUCKET.m.has("videos/x.mp4"));
  console.log("✅ hourly cleanup only at minute 00");
}
console.log("\nALL WORKER TESTS PASSED");
