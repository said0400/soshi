// worker/src/index.js — Cloudflare Worker: ناشر الفيديوهات (v1.0)
// ═══════════════════════════════════════════════════════════════════════════
// يعمل كل دقيقة (Cron Trigger):
//   1) يقرأ queue/*.json من R2. اسم الملف يبدأ بوقت النشر (UTC) فيُعرف المستحق
//      بدون قراءة محتواه:  queue/20261102T170000Z_ar_short_5.json
//   2) لكل عنصر حان موعده: يرسل Webhook إلى Make لكل منصة (youtube/facebook/...)
//      ويسجّل sent_at داخل الـ JSON بعد كل نجاح (فلا يتكرر الإرسال عند إعادة المحاولة).
//   3) اكتملت كل المنصات → ينقل الملف إلى sent/ ؛ فات أكثر من MAX_LATE_MIN → failed/
//   4) كل ساعة: يحذف ما مضى عليه RETENTION_HOURS (48) من videos/ و sent/ و failed/.
//      لا يلمس state/ أبداً (قواعد الإنتاج).
//
// الأسرار (wrangler secret put):  MAKE_WEBHOOK_URL_AR / _FR / _EN / _ES  (أو MAKE_WEBHOOK_URL)
//                                  MAKE_AUTH_TOKEN (اختياري: يُرسل في الحقل auth)
// ═══════════════════════════════════════════════════════════════════════════

const QUEUE = "queue/";
const SENT = "sent/";
const FAILED = "failed/";
const CLEAN_PREFIXES = ["videos/", "sent/", "failed/", "queue/"];   // ليس state/
const KEY_TIME = /^queue\/(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z_/;

const MAX_ITEMS_PER_RUN = 25;
const MAX_ATTEMPTS = 40;
const REQUEST_TIMEOUT_MS = 15000;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env, new Date(event.scheduledTime)));
  },
};

export function publishTimeFromKey(key) {
  const m = KEY_TIME.exec(key);
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
}

async function listAll(bucket, prefix) {
  const out = [];
  let cursor;
  do {
    const r = await bucket.list({ prefix, cursor, limit: 1000 });
    out.push(...r.objects);
    cursor = r.truncated ? r.cursor : undefined;
  } while (cursor);
  return out;
}

function webhookFor(env, lang) {
  return env[`MAKE_WEBHOOK_URL_${String(lang).toUpperCase()}`] || env.MAKE_WEBHOOK_URL || "";
}

async function postToMake(url, body, env, fetchFn) {
  const payload = { ...body };
  if (env.MAKE_AUTH_TOKEN) payload.auth = env.MAKE_AUTH_TOKEN;
  try {
    const r = await fetchFn(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (r.status >= 200 && r.status < 300) return { ok: true };
    return { ok: false, error: `HTTP ${r.status}` };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e).slice(0, 120) };
  }
}

async function moveItem(bucket, fromKey, toPrefix, item) {
  const name = fromKey.slice(QUEUE.length);
  await bucket.put(toPrefix + name, JSON.stringify(item), {
    httpMetadata: { contentType: "application/json" },
  });
  await bucket.delete(fromKey);
}

export async function publishDue(env, now, fetchFn = fetch) {
  const maxLateMs = Number(env.MAX_LATE_MIN || 180) * 60000;
  const objs = (await listAll(env.BUCKET, QUEUE)).sort((a, b) => (a.key < b.key ? -1 : 1));
  const stats = { checked: objs.length, due: 0, sent: 0, failed: 0, retry: 0 };
  let handled = 0;

  for (const obj of objs) {
    const at = publishTimeFromKey(obj.key);
    if (!at || at > now) continue;                       // لم يحن بعد (أو اسم غير صالح)
    if (handled++ >= MAX_ITEMS_PER_RUN) break;
    stats.due++;

    const o = await env.BUCKET.get(obj.key);
    if (!o) continue;
    let item;
    try { item = await o.json(); } catch {
      await moveItem(env.BUCKET, obj.key, FAILED, { error: "invalid json", key: obj.key });
      stats.failed++;
      continue;
    }

    const lateMs = now - at;
    const pending = Object.entries(item.platforms || {}).filter(([, p]) => !p.sent_at);
    if (lateMs > maxLateMs && pending.length) {
      item.failed_reason = `missed window (${Math.round(lateMs / 60000)} min late)`;
      await moveItem(env.BUCKET, obj.key, FAILED, item);
      stats.failed++;
      continue;
    }

    const url = webhookFor(env, item.lang);
    if (!url && pending.length) {
      // بلا رابط: سجّل السبب في الملف (ليظهر عند التشخيص) وانتقل للعنصر التالي
      for (const [, p] of pending) p.last_error = "webhook secret missing";
      await env.BUCKET.put(obj.key, JSON.stringify(item), {
        httpMetadata: { contentType: "application/json" },
      });
      stats.retry++;
      continue;
    }
    for (const [, p] of pending) {
      if ((p.attempts || 0) >= MAX_ATTEMPTS) continue;
      p.attempts = (p.attempts || 0) + 1;
      const r = await postToMake(url, p.body, env, fetchFn);
      if (r.ok) {
        p.sent_at = now.toISOString();
        p.last_error = null;
        stats.sent++;
      } else {
        p.last_error = r.error;
        stats.retry++;
      }
      // احفظ بعد كل منصة: لا يتكرر الإرسال لو انقطع التنفيذ
      await env.BUCKET.put(obj.key, JSON.stringify(item), {
        httpMetadata: { contentType: "application/json" },
      });
    }

    if (Object.values(item.platforms || {}).every((p) => p.sent_at)) {
      item.completed_at = now.toISOString();
      await moveItem(env.BUCKET, obj.key, SENT, item);
    }
  }
  return stats;
}

export async function cleanup(env, now) {
  const cutoff = now.getTime() - Number(env.RETENTION_HOURS || 48) * 3600000;
  let deleted = 0;
  for (const prefix of CLEAN_PREFIXES) {
    const old = (await listAll(env.BUCKET, prefix))
      .filter((o) => new Date(o.uploaded).getTime() < cutoff)
      .map((o) => o.key);
    for (let i = 0; i < old.length; i += 1000) {
      await env.BUCKET.delete(old.slice(i, i + 1000));
      deleted += Math.min(1000, old.length - i);
    }
  }
  return deleted;
}

export async function run(env, now = new Date(), fetchFn = fetch) {
  const stats = await publishDue(env, now, fetchFn);
  if (now.getUTCMinutes() === 0) stats.cleaned = await cleanup(env, now);   // مرة كل ساعة
  console.log(JSON.stringify({ at: now.toISOString(), ...stats }));
  return stats;
}
