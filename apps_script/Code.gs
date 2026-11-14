/**
 * Code.gs — Google Apps Script: صندوق النشر (v1.0)
 * ═══════════════════════════════════════════════════════════════════════════
 * يُلصق داخل Google Sheet (Extensions → Apps Script). ثلاث وظائف:
 *
 *  1) doPost   : Web App يستقبل من GitHub Actions:
 *                  {secret, action:"enqueue", item:{...}}  ← يضيف صفاً لكل فيديو
 *                  {secret, action:"exists",  id:"..."}    ← هل الموعد مُنتَج مسبقاً؟
 *  2) tick     : كل دقيقة. لكل صف pending حان موعده يرسل Webhook واحداً إلى Make
 *                (payload كامل يحمل المنصتين) ثم يسجّل sent / يعيد المحاولة / failed.
 *  3) trim     : يومياً. يحذف الصفوف المنتهية الأقدم من 14 يوماً.
 *
 * Script Properties (Project Settings → Script properties):
 *   APPS_SCRIPT_SECRET : سرّ مشترك مع GitHub (نفس APPS_SCRIPT_SECRET هناك)
 *   MAKE_WEBHOOK_URL   : رابط Custom webhook للسيناريو الواحد في Make
 *   MAKE_AUTH_TOKEN    : (اختياري) يُرسل في الحقل auth ليفلتره Make
 *
 * التفعيل: شغّل setup() مرة واحدة (يطلب الصلاحيات وينشئ المشغّلات)، ثم
 *          Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone.
 * ═══════════════════════════════════════════════════════════════════════════
 */

const SHEET_NAME = "queue";
const HEADERS = ["id", "lang", "mode", "number", "title", "publish_at", "status",
                 "attempts", "last_error", "sent_at", "created_at", "payload"];
const C = { ID: 0, LANG: 1, MODE: 2, NUMBER: 3, TITLE: 4, PUBLISH_AT: 5, STATUS: 6,
            ATTEMPTS: 7, LAST_ERROR: 8, SENT_AT: 9, CREATED_AT: 10, PAYLOAD: 11 };

const MAX_LATE_MIN = 180;     // بعدها يُعتبر الفيديو فائتاً
const MAX_ATTEMPTS = 10;
const MAX_PER_TICK = 12;
const KEEP_DAYS = 14;

function props_() { return PropertiesService.getScriptProperties(); }

function sheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(HEADERS);
    sh.setFrozenRows(1);
  }
  return sh;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function toDate_(v) {
  return v instanceof Date ? v : new Date(String(v));
}

function findRow_(sh, id) {
  const values = sh.getDataRange().getValues();
  for (let i = 1; i < values.length; i++) {
    if (String(values[i][C.ID]) === String(id)) return i + 1;      // رقم الصف (1-based)
  }
  return -1;
}

// ── 1) Web App ─────────────────────────────────────────────────────────────
function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (!req.secret || req.secret !== props_().getProperty("APPS_SCRIPT_SECRET")) {
      return json_({ ok: false, error: "unauthorized" });
    }
    if (req.action === "exists") {
      return json_({ ok: true, exists: findRow_(sheet_(), req.id) > 0 });
    }
    if (req.action === "enqueue") return json_(enqueue_(req.item));
    return json_({ ok: false, error: "unknown action" });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

function enqueue_(item) {
  if (!item || !item.id || !item.payload) return { ok: false, error: "bad item" };
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sh = sheet_();
    if (findRow_(sh, item.id) > 0) return { ok: true, duplicate: true };
    const row = sh.getLastRow() + 1;
    const rng = sh.getRange(row, 1, 1, HEADERS.length);
    rng.setNumberFormat("@");                       // نص: يمنع تحويل التواريخ تلقائياً
    rng.setValues([[
      item.id, item.lang, item.content_mode, String(item.video_number), item.title,
      item.publish_at, "pending", "0", "", "", item.created_at || "",
      JSON.stringify(item.payload),
    ]]);
    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}

// ── 2) الإرسال إلى Make ────────────────────────────────────────────────────
function sendToMake_(payload) {
  const url = props_().getProperty("MAKE_WEBHOOK_URL");
  if (!url) return { ok: false, error: "MAKE_WEBHOOK_URL not set" };
  const body = Object.assign({}, payload);
  const token = props_().getProperty("MAKE_AUTH_TOKEN");
  if (token) body.auth = token;
  try {
    const r = UrlFetchApp.fetch(url, {
      method: "post", contentType: "application/json",
      payload: JSON.stringify(body), muteHttpExceptions: true,
    });
    const code = r.getResponseCode();
    return code >= 200 && code < 300 ? { ok: true } : { ok: false, error: "HTTP " + code };
  } catch (err) {
    return { ok: false, error: String(err).slice(0, 150) };
  }
}

function tick() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) return;                  // تشغيل سابق لم ينتهِ
  try {
    const sh = sheet_();
    const values = sh.getDataRange().getValues();
    const now = new Date();

    const due = [];
    for (let i = 1; i < values.length; i++) {
      if (String(values[i][C.STATUS]) !== "pending") continue;
      const at = toDate_(values[i][C.PUBLISH_AT]);
      if (isNaN(at.getTime())) { due.push({ i, at: new Date(0), bad: true }); continue; }
      if (at <= now) due.push({ i, at });
    }
    due.sort(function (a, b) { return a.at - b.at; });

    due.slice(0, MAX_PER_TICK).forEach(function (d) {
      const row = d.i + 1;
      const v = values[d.i];
      const nowIso = now.toISOString();
      let status = "pending", attempts = Number(v[C.ATTEMPTS]) || 0;
      let lastError = "", sentAt = "";

      if (d.bad) {
        status = "failed"; lastError = "invalid publish_at";
      } else if ((now - d.at) / 60000 > MAX_LATE_MIN) {
        status = "failed";
        lastError = "missed window (" + Math.round((now - d.at) / 60000) + " min late)";
      } else {
        let payload;
        try { payload = JSON.parse(String(v[C.PAYLOAD])); } catch (e) { payload = null; }
        if (!payload) {
          status = "failed"; lastError = "invalid payload";
        } else {
          attempts += 1;
          const r = sendToMake_(payload);
          if (r.ok) { status = "sent"; sentAt = nowIso; }
          else {
            lastError = r.error;
            if (attempts >= MAX_ATTEMPTS) status = "failed";
          }
        }
      }
      // status, attempts, last_error, sent_at  (أعمدة متجاورة G..J)
      sh.getRange(row, C.STATUS + 1, 1, 4).setValues([[status, String(attempts), lastError, sentAt]]);
    });
  } finally {
    lock.releaseLock();
  }
}

// ── 3) التنظيف ─────────────────────────────────────────────────────────────
function trim() {
  const sh = sheet_();
  const values = sh.getDataRange().getValues();
  const cutoff = new Date(Date.now() - KEEP_DAYS * 86400000);
  for (let i = values.length - 1; i >= 1; i--) {         // من الأسفل كي لا تتزحزح الصفوف
    const st = String(values[i][C.STATUS]);
    if (st !== "sent" && st !== "failed") continue;
    const at = toDate_(values[i][C.PUBLISH_AT]);
    if (!isNaN(at.getTime()) && at < cutoff) sh.deleteRow(i + 1);
  }
}

// ── الإعداد (مرة واحدة) ────────────────────────────────────────────────────
function setup() {
  sheet_();
  ScriptApp.getProjectTriggers().forEach(function (t) {
    const f = t.getHandlerFunction();
    if (f === "tick" || f === "trim") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("tick").timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger("trim").timeBased().everyDays(1).atHour(3).create();
  Logger.log("✅ triggers created. Now: Deploy → New deployment → Web app (Execute as: Me, Access: Anyone)");
}
