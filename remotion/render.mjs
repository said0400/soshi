// remotion/render.mjs — Video Renderer v7.0
// ═══════════════════════════════════════════════════════════════════════════
// Usage: node render.mjs <manifest.json> <output.mp4>
//
// Pipeline (one process):
//   1) كل clip: crop/scale + إبطاء جمالي بحسب التاغ + zoom تدريجي + تدرج لوني بحسب التاغ
//   2) xfade: انتقال مرتبط بتاغ الجملة القادمة، مركّز على حد الجملة (بلا انزياح)
//   3) Short: كلمات متحركة (Playwright → PNG) | Long: ترجمة ASS (كلمة نشطة ملوّنة)
//   4) دمج الصوت (m4a) + ميتاداتا نظيفة (-map_metadata -1)
//
// Manifest:
//   content_mode, lang, width, height, audio, duration_s,
//   clips:[{video,duration,tag}], aligned:[{tag,start,end,words:[{word,start,end}]}],
//   title, display_title, emoji_left, emoji_right, power_words[], custom_hook,
//   has_hook, bg_style ("video"|"cinematic"|"blur"), primary_emotion,
//   stage ("full" | "bg")   ← "bg" يتخطى الكلمات (للاختبار)
// ═══════════════════════════════════════════════════════════════════════════

import {
  readFileSync, writeFileSync, mkdirSync, copyFileSync,
  existsSync, symlinkSync, rmSync, unlinkSync,
} from "fs";
import { spawnSync } from "child_process";
import { tmpdir } from "os";
import { join } from "path";

const [manifestPath, outputPath] = process.argv.slice(2);
if (!manifestPath || !outputPath) {
  console.error("Usage: node render.mjs <manifest.json> <output.mp4>");
  process.exit(1);
}

let M;
try { M = JSON.parse(readFileSync(manifestPath, "utf-8")); }
catch (e) { console.error(`❌ Cannot read manifest: ${e.message}`); process.exit(1); }

const {
  content_mode = "short", lang = "ar", audio, clips = [], aligned = [],
  title = "", display_title = title, emoji_left = "🔥", emoji_right = "💥",
  power_words = [], custom_hook = "", has_hook = false,
  bg_style = "video", primary_emotion = "", stage = "full",
} = M;

const isLong = content_mode === "long";
const W = M.width  || (isLong ? 1920 : 1080);
const H = M.height || (isLong ? 1080 : 1920);
const FPS = M.fps || 30;

// ═══════════════════════════════════════════════════════════════════════════
// أدوات عامة
// ═══════════════════════════════════════════════════════════════════════════

const TMP = join(tmpdir(), `vsg_${process.pid}_${Date.now()}`);
mkdirSync(TMP, { recursive: true });

let _cleaned = false;
function cleanup() {
  if (_cleaned) return;
  _cleaned = true;
  try { rmSync(TMP, { recursive: true, force: true }); } catch {}
}
process.on("exit", cleanup);
process.on("SIGINT",  () => { cleanup(); process.exit(130); });
process.on("SIGTERM", () => { cleanup(); process.exit(143); });
process.on("uncaughtException",  (e) => { console.error("❌", e.message); cleanup(); process.exit(1); });
process.on("unhandledRejection", (r) => { console.error("❌", r);         cleanup(); process.exit(1); });

function createRng(seed) {
  let s = (seed >>> 0) || 1;
  return () => {
    s = Math.imul(s ^ (s >>> 15), s | 1);
    s ^= s + Math.imul(s ^ (s >>> 7), s | 61);
    return ((s ^ (s >>> 14)) >>> 0) / 0x100000000;
  };
}
const rng = createRng(
  [...(title || "x")].reduce((a, c) => (a * 31 + c.charCodeAt(0)) | 0, 7) + lang.charCodeAt(0));

function ff(args, label) {
  const r = spawnSync("ffmpeg", ["-hide_banner", "-loglevel", "error", ...args],
    { stdio: ["ignore", "pipe", "pipe"], maxBuffer: 64 * 1024 * 1024 });
  if (r.status !== 0) {
    console.log(`  ⚠️ ffmpeg[${label}] failed: ${(r.stderr || "").toString().slice(-600)}`);
  }
  return r.status === 0;
}

const _durCache = new Map();
function probeDuration(fp) {
  if (!fp || !existsSync(fp)) return 0;
  if (_durCache.has(fp)) return _durCache.get(fp);
  const r = spawnSync("ffprobe", ["-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", fp], { stdio: ["ignore", "pipe", "pipe"] });
  const d = parseFloat(r.stdout.toString().trim()) || 0;
  _durCache.set(fp, d);
  return d;
}

const rm = (...fs) => fs.forEach((f) => { try { unlinkSync(f); } catch {} });
const pad = (n, l = 3) => String(n).padStart(l, "0");
const esc = (s) => (s || "").toString().replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
const isArabic = (t) => /[\u0600-\u06FF]/.test(t);
const fontFor = (t) => isArabic(t)
  ? `"Noto Naskh Arabic","Amiri",serif` : `"Noto Sans","DejaVu Sans",sans-serif`;
const normWord = (w) => (w || "").toString()
  .replace(/[.,!?؟،;:"'(){}[\]<>«»…¿¡]/g, "").trim().toLowerCase();

const TOTAL = (() => {
  const a = probeDuration(audio);
  return a > 1 ? a : (M.duration_s || 0);
})();
if (!(TOTAL > 0)) { console.error("❌ audio duration unknown"); process.exit(1); }
const TOTAL_FRAMES = Math.ceil(TOTAL * FPS);

console.log(`🎬 ${content_mode.toUpperCase()} ${lang.toUpperCase()} ${W}×${H} | ${TOTAL.toFixed(2)}s | ${clips.length} clips`);

// ═══════════════════════════════════════════════════════════════════════════
// أنماط بصرية بحسب التاغ
// ═══════════════════════════════════════════════════════════════════════════

// إبطاء جمالي: >1 أبطأ
const SLOW = {
  emotional: 1.30, calm: 1.30, wisdom: 1.25, pause: 1.35, whisper: 1.30,
  dramatic: 1.25, storytelling: 1.15, desire: 1.20,
  shock: 1.0, urgency: 1.0, hook: 1.0, climax: 1.0, tension: 1.05, revelation: 1.05,
};
const slowOf = (tag) => SLOW[tag] ?? 1.12;

// تدرج لوني (eq + colorbalance) بحسب المزاج
const GRADE = {
  punchy: "eq=contrast=1.22:saturation=1.05:brightness=-0.02,colorbalance=rs=0.04:bs=-0.03",
  cool:   "eq=contrast=1.08:saturation=0.78:brightness=-0.02,colorbalance=bs=0.05:rs=-0.03",
  warm:   "eq=contrast=1.10:saturation=0.95:brightness=-0.01,colorbalance=rs=0.05:bs=-0.04",
  dark:   "eq=contrast=1.22:saturation=0.82:brightness=-0.05",
  clean:  "eq=contrast=1.12:saturation=0.92:brightness=-0.02",
};
const TAG_GRADE = {
  shock: "punchy", climax: "punchy", urgency: "punchy", hook: "punchy", tension: "dark",
  revelation: "punchy", dramatic: "dark",
  calm: "cool", wisdom: "cool", whisper: "cool", pause: "cool", intrigue: "cool",
  emotional: "warm", desire: "warm", inspiration: "warm", storytelling: "warm",
};
const gradeOf = (tag) => GRADE[TAG_GRADE[tag] || "clean"];

// لمسة الفيلم العامة (تُطبّق مرة واحدة على الناتج النهائي)
function filmLook() {
  if (bg_style === "cinematic") {
    return "curves=r='0/0 0.5/0.48 1/0.93':g='0/0 0.5/0.45 1/0.88':b='0/0.04 0.5/0.44 1/0.84',eq=contrast=1.08:saturation=0.9";
  }
  if (bg_style === "blur") {
    return "curves=r='0/0 0.5/0.52 1/0.96':g='0/0 0.5/0.49 1/0.92':b='0/0.02 0.5/0.47 1/0.88',gblur=sigma=0.4";
  }
  return "curves=r='0/0 0.5/0.51 1/0.96':g='0/0 0.5/0.49 1/0.93':b='0/0.02 0.5/0.47 1/0.90'";
}

// ═══════════════════════════════════════════════════════════════════════════
// الانتقالات (xfade) — مرتبطة بتاغ الجملة القادمة
// ═══════════════════════════════════════════════════════════════════════════

// [transition, duration]
const TAG_TRANSITIONS = {
  shock:      [["fadewhite", 0.16], ["pixelize", 0.22], ["zoomin", 0.22]],
  climax:     [["fadewhite", 0.18], ["circleopen", 0.28], ["zoomin", 0.24]],
  hook:       [["zoomin", 0.20], ["wipeleft", 0.22]],
  revelation: [["circleopen", 0.34], ["fadeblack", 0.40], ["radial", 0.36]],
  urgency:    [["slideleft", 0.20], ["wipeleft", 0.20], ["wiperight", 0.20]],
  tension:    [["smoothleft", 0.26], ["slideup", 0.24], ["fadeblack", 0.30]],
  dramatic:   [["fadeblack", 0.45], ["distance", 0.38], ["circleclose", 0.36]],
  emotional:  [["fade", 0.55], ["fadegrays", 0.55], ["dissolve", 0.50]],
  calm:       [["fade", 0.60], ["dissolve", 0.55], ["smoothup", 0.50]],
  wisdom:     [["fade", 0.55], ["fadegrays", 0.55], ["smoothleft", 0.50]],
  whisper:    [["dissolve", 0.55], ["fade", 0.55]],
  pause:      [["fadeblack", 0.55], ["fade", 0.50]],
  inspiration:[["smoothup", 0.34], ["circleopen", 0.34], ["slideup", 0.28]],
  confident:  [["wipeleft", 0.24], ["slideright", 0.24], ["smoothright", 0.28]],
  cta:        [["zoomin", 0.24], ["smoothup", 0.30]],
  information:[["smoothleft", 0.30], ["fade", 0.32], ["wipeleft", 0.26]],
  default:    [["fade", 0.35], ["smoothleft", 0.32], ["wipeleft", 0.28]],
};

function pickTransition(prev, tag) {
  const pool = TAG_TRANSITIONS[tag] || TAG_TRANSITIONS.default;
  const opts = pool.filter(([t]) => t !== prev);
  const [name, dur] = (opts.length ? opts : pool)[Math.floor(rng() * (opts.length || pool.length))];
  return { name, dur: isLong ? Math.max(dur, 0.4) : dur };
}

// ═══════════════════════════════════════════════════════════════════════════
// 1) خطة الـ clips
// ═══════════════════════════════════════════════════════════════════════════

function buildPlan() {
  const valid = clips.filter((c) => c && c.video && existsSync(c.video));
  if (!valid.length) { console.error("❌ no valid clips"); process.exit(1); }

  // طابق مجموع المدد مع مدة الصوت
  const sum = valid.reduce((a, c) => a + c.duration, 0);
  const k = sum > 0 ? TOTAL / sum : 1;
  let t = 0;
  const plan = valid.map((c, i) => {
    const d = Math.max(0.6, c.duration * k);
    const e = { i, video: c.video, tag: c.tag || "information", start: t, dur: d };
    t += d;
    return e;
  });
  // صحّح الفرق على الأخير
  const last = plan[plan.length - 1];
  last.dur = Math.max(0.6, last.dur + (TOTAL - t));

  // انتقالات: boundary b بين i-1 و i. صفر على حدود المجموعات (قص مباشر)
  const GROUP = isLong ? 12 : 40;
  let prev = null;
  plan.forEach((c, i) => {
    c.group = Math.floor(i / GROUP);
    c.xIn = 0; c.trans = null;
    if (i === 0 || c.group !== plan[i - 1].group) return;
    const tr = pickTransition(prev, c.tag);
    prev = tr.name;
    const maxX = Math.min(plan[i - 1].dur, c.dur) * 0.6;
    c.xIn = Math.max(0.08, Math.min(tr.dur, maxX));
    c.trans = tr.name;
  });
  // طول كل clip = المدة + نصف الانتقال على الجانبين
  plan.forEach((c, i) => {
    const xOut = plan[i + 1] && plan[i + 1].group === c.group ? plan[i + 1].xIn : 0;
    c.len = c.dur + c.xIn / 2 + xOut / 2;
    c.from = c.start - c.xIn / 2;      // بداية الـ clip على خط الزمن
  });
  return plan;
}

// ═══════════════════════════════════════════════════════════════════════════
// 2) معالجة clip واحد
// ═══════════════════════════════════════════════════════════════════════════

function renderClip(c, outFile) {
  const slow = slowOf(c.tag);
  const need = c.len / slow + 0.15;
  const src = probeDuration(c.video);
  const loop = src > 0 && src < need * 1.02 ? ["-stream_loop", "-1"] : [];
  const ss = src > need + 1.5 ? ["-ss", Math.min(1.0, (src - need) / 4).toFixed(2)] : [];

  // zoom تدريجي: داخل للزوجي، خارج للفردي
  const z0 = c.i % 2 === 0 ? 1.0 : 1.07;
  const dz = c.i % 2 === 0 ? 0.07 : -0.07;
  const zx = `(${z0}+${dz}*t/${c.len.toFixed(3)})`;

  const vf = [
    `scale=${W}:${H}:force_original_aspect_ratio=increase`,
    `crop=${W}:${H}`,
    "setsar=1",
    `setpts=${slow}*PTS`,
    `fps=${FPS}`,
    `scale=w='2*trunc(${W}*${zx}/2)':h='2*trunc(${H}*${zx}/2)':eval=frame`,
    `crop=${W}:${H}`,
    gradeOf(c.tag),
    "unsharp=5:5:0.45:5:5:0.0",
    "vignette=PI/5",
    "noise=alls=3:allf=t",
    "format=yuv420p",
  ].join(",");

  const base = ["-y", ...loop, ...ss, "-i", c.video, "-t", c.len.toFixed(3), "-an",
    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", String(FPS)];

  if (ff([...base, "-vf", vf, outFile], `clip${c.i}`) && existsSync(outFile)) return true;

  // fallback مبسّط
  const simple = `scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},setsar=1,fps=${FPS},format=yuv420p`;
  return ff([...base, "-vf", simple, outFile], `clip${c.i}-simple`) && existsSync(outFile);
}

// ═══════════════════════════════════════════════════════════════════════════
// 3) تجميع المجموعات (xfade) ثم الدمج
// ═══════════════════════════════════════════════════════════════════════════

function buildGroup(files, items, outFile, gIdx) {
  if (files.length === 1) { copyFileSync(files[0], outFile); return true; }
  const inputs = files.flatMap((f) => ["-i", f]);
  const filters = [];
  let cur = "[0:v]";
  for (let k = 1; k < files.length; k++) {
    const c = items[k];
    const off = (c.start - c.xIn / 2) - (items[0].start - items[0].xIn / 2);
    const lbl = k === files.length - 1 ? "[vout]" : `[x${k}]`;
    filters.push(`${cur}[${k}:v]xfade=transition=${c.trans}:duration=${c.xIn.toFixed(3)}:offset=${Math.max(0, off).toFixed(3)}${lbl}`);
    cur = lbl;
  }
  const ok = ff(["-y", ...inputs, "-filter_complex", filters.join(";"), "-map", "[vout]",
    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", String(FPS),
    outFile], `group${gIdx}`);
  if (ok && existsSync(outFile)) return true;

  // fallback: قص مباشر
  console.log(`  ⚠️ group ${gIdx}: xfade failed → hard cuts`);
  const list = join(TMP, `grp_${gIdx}.txt`);
  writeFileSync(list, files.map((f) => `file '${f.replace(/'/g, "'\\''")}'`).join("\n"));
  return ff(["-y", "-f", "concat", "-safe", "0", "-i", list, "-c:v", "libx264", "-preset", "fast",
    "-crf", "18", "-pix_fmt", "yuv420p", outFile], `group${gIdx}-concat`);
}

function buildBackground() {
  const plan = buildPlan();
  console.log(`\n🎞️  ${plan.length} clips`);
  plan.slice(0, 4).forEach((c) =>
    console.log(`   [${c.i + 1}] ${c.start.toFixed(2)}s +${c.dur.toFixed(2)}s [${c.tag}]` +
      `${c.trans ? ` ← ${c.trans} ${c.xIn.toFixed(2)}s` : ""}`));

  const files = [];
  for (const c of plan) {
    const f = join(TMP, `c_${pad(c.i)}.mp4`);
    process.stdout.write(`  [${c.i + 1}/${plan.length}] ${c.len.toFixed(1)}s `);
    if (!renderClip(c, f)) {
      if (files.length) { copyFileSync(files[files.length - 1], f); process.stdout.write("↺copy\n"); }
      else { console.error("❌ first clip failed"); process.exit(1); }
    } else process.stdout.write("✓\n");
    files.push(f);
  }

  const groupOut = [];
  const nGroups = plan[plan.length - 1].group + 1;
  for (let g = 0; g < nGroups; g++) {
    const idx = plan.map((c, i) => (c.group === g ? i : -1)).filter((i) => i >= 0);
    const out = join(TMP, `g_${g}.mp4`);
    if (!buildGroup(idx.map((i) => files[i]), idx.map((i) => plan[i]), out, g)) {
      console.error(`❌ group ${g} failed`); process.exit(1);
    }
    groupOut.push(out);
    idx.forEach((i) => rm(files[i]));
  }

  if (groupOut.length === 1) return groupOut[0];
  const list = join(TMP, "groups.txt");
  writeFileSync(list, groupOut.map((f) => `file '${f.replace(/'/g, "'\\''")}'`).join("\n"));
  const all = join(TMP, "bg_all.mp4");
  if (!ff(["-y", "-f", "concat", "-safe", "0", "-i", list, "-c", "copy", all], "concat-groups")) {
    console.error("❌ concat groups failed"); process.exit(1);
  }
  return all;
}

// ═══════════════════════════════════════════════════════════════════════════
// 4) Long: ترجمة ASS (سطر بعدّة كلمات، الكلمة النشطة ملوّنة)
// ═══════════════════════════════════════════════════════════════════════════

const ASS_TAG_COLOR = {   // BGR
  shock: "&H0000D7FF", urgency: "&H000022FF", intrigue: "&H0000D7FF", emotional: "&H00AB8FFF",
  inspiration: "&H0000D7FF", wisdom: "&H00FFB182", desire: "&H0047B3FF", calm: "&H00EADE80",
  revelation: "&H0000D7FF", climax: "&H0000D7FF", dramatic: "&H009A9AEF", tension: "&H004370FF",
  default: "&H0000D7FF",
};

const assTime = (s) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  const cs = Math.min(99, Math.floor((s % 1) * 100));
  return `${h}:${pad(m, 2)}:${pad(sec, 2)}.${pad(cs, 2)}`;
};
const assText = (t) => (t || "").toString().replace(/\\/g, "＼").replace(/[{}]/g, "")
  .replace(/\r?\n/g, " ");

function buildAss() {
  const font = isArabic(display_title + (aligned[0]?.sentence || "")) ? "Noto Naskh Arabic" : "Noto Sans";
  const wordSize = Math.round(H * 0.058);
  const titleSize = Math.round(H * 0.034);
  const header = [
    "[Script Info]", "ScriptType: v4.00+", `PlayResX: ${W}`, `PlayResY: ${H}`,
    "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, " +
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, " +
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    `Style: Word,${font},${wordSize},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,4,2,2,${Math.round(W * 0.08)},${Math.round(W * 0.08)},${Math.round(H * 0.10)},1`,
    `Style: Title,${font},${titleSize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,8,40,40,${Math.round(H * 0.035)},1`,
    "", "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
  ].join("\r\n");

  const ev = [];
  ev.push(`Dialogue: 0,${assTime(0)},${assTime(TOTAL)},Title,,0,0,0,,${assText(`${emoji_left} ${display_title} ${emoji_right}`)}`);

  const MAX_WORDS = 6;
  const MAX_CHARS = isLong ? 38 : 26;
  for (const seg of aligned) {
    const words = (seg.words || []).filter((w) => w.word && w.end > w.start);
    if (!words.length) continue;
    const color = ASS_TAG_COLOR[seg.tag] || ASS_TAG_COLOR.default;

    // قسّم الجملة إلى أسطر
    const lines = [];
    let cur = [];
    let chars = 0;
    for (const w of words) {
      if (cur.length && (cur.length >= MAX_WORDS || chars + w.word.length > MAX_CHARS)) {
        lines.push(cur); cur = []; chars = 0;
      }
      cur.push(w); chars += w.word.length + 1;
    }
    if (cur.length) lines.push(cur);

    for (const line of lines) {
      line.forEach((w, wi) => {
        const start = w.start;
        const end = wi < line.length - 1 ? line[wi + 1].start : Math.max(w.end, w.start + 0.2);
        const txt = line.map((x, xi) => {
          const word = assText(x.word);
          const power = powerSet.has(normWord(x.word));
          if (xi === wi) return `{\\c${power ? "&H0000D7FF" : color}\\b1\\fscx112\\fscy112}${word}{\\r}`;
          return word;
        }).join(" ");
        ev.push(`Dialogue: 1,${assTime(start)},${assTime(end)},Word,,0,0,0,,${txt}`);
      });
    }
  }
  return header + "\r\n" + ev.join("\r\n") + "\r\n";
}

const powerSet = new Set(power_words.map(normWord).filter((w) => w.length >= 2));
const isPower = (w) => powerSet.has(normWord(w));

// ═══════════════════════════════════════════════════════════════════════════
// 5) Short: كلمات متحركة (Playwright)
// ═══════════════════════════════════════════════════════════════════════════

// [color, glow, scale, strokeW, brightness]
const WORD_STYLE = {
  shock: ["#FFFFFF", "rgba(255,50,50,0.9)", 1.30, 5, 1.4],
  climax: ["#FFFFFF", "rgba(255,100,50,0.95)", 1.35, 6, 1.5],
  revelation: ["#FFFFFF", "rgba(255,255,200,0.9)", 1.25, 5, 1.45],
  urgency: ["#FF2200", "rgba(255,34,0,0.8)", 1.20, 4, 1.3],
  tension: ["#FF7043", "rgba(255,112,67,0.75)", 1.15, 4, 1.25],
  dramatic: ["#EF9A9A", "rgba(239,154,154,0.7)", 1.12, 4, 1.15],
  hook: ["#FFD700", "rgba(255,215,0,0.8)", 1.20, 5, 1.3],
  intrigue: ["#FFD700", "rgba(255,215,0,0.7)", 1.0, 4, 1.0],
  inspiration: ["#FFD700", "rgba(255,215,0,0.8)", 1.15, 4, 1.3],
  emotional: ["#FF8FAB", "rgba(255,143,171,0.7)", 0.95, 4, 1.0],
  desire: ["#FFB347", "rgba(255,179,71,0.7)", 1.0, 4, 1.1],
  confident: ["#FFFFFF", "rgba(255,255,255,0.6)", 1.10, 5, 1.2],
  powerful: ["#ECEFF1", "rgba(236,239,241,0.65)", 1.12, 5, 1.2],
  wisdom: ["#82B1FF", "rgba(130,177,255,0.6)", 0.90, 3, 0.95],
  calm: ["#80DEEA", "rgba(128,222,234,0.5)", 0.85, 3, 0.9],
  pause: ["#B0BEC5", "rgba(176,190,197,0.4)", 0.80, 2, 0.85],
  whisper: ["#CE93D8", "rgba(206,147,216,0.6)", 0.88, 3, 0.9],
  curiosity: ["#FFF176", "rgba(255,241,118,0.6)", 1.02, 4, 1.05],
  storytelling: ["#FFCC80", "rgba(255,204,128,0.5)", 0.95, 3, 1.0],
  cta: ["#FFD700", "rgba(255,215,0,0.8)", 1.15, 4, 1.3],
  information: ["#FFFFFF", "rgba(255,255,255,0.35)", 1.0, 4, 1.0],
};
const wordStyle = (tag) => WORD_STYLE[tag] || WORD_STYLE.information;

// وميض عند حد الجملة: [color, frames, shake, boost]
const FLASH = {
  shock: ["rgba(255,255,255,1.0)", 9, 18, 1.12], climax: ["rgba(255,255,255,0.95)", 11, 20, 1.18],
  revelation: ["rgba(255,255,200,0.9)", 10, 14, 1.15], urgency: ["rgba(220,0,0,0.85)", 7, 12, 1.08],
  tension: ["rgba(255,100,0,0.5)", 8, 10, 1.08], dramatic: ["rgba(180,0,0,0.6)", 12, 10, 1.10],
  hook: ["rgba(255,255,255,0.6)", 7, 8, 1.06], default: ["rgba(255,255,255,0.15)", 6, 0, 1.0],
};

const INTRO = Math.floor(1.0 * FPS), OUTRO = Math.floor(1.0 * FPS), HOOK = Math.floor(3.0 * FPS);

function buildWordList() {
  const words = [];
  for (const seg of aligned) {
    for (const x of seg.words || []) {
      const s = parseFloat(x.start), e = parseFloat(x.end);
      if (!x.word || !x.word.trim() || isNaN(s) || isNaN(e) || e <= s) continue;
      words.push({ word: x.word.trim(), start: s, end: e, tag: seg.tag || "information", power: isPower(x.word) });
    }
  }
  return words.sort((a, b) => a.start - b.start);
}

function buildFrameState(words) {
  const map = new Array(TOTAL_FRAMES).fill(null);
  let wi = 0;
  for (let f = 0; f < TOTAL_FRAMES; f++) {
    const t = f / FPS;
    while (wi < words.length - 1 && words[wi].end <= t) wi++;
    const w = words[wi];
    if (w && t >= w.start && t < w.end) {
      map[f] = { word: w.word, tag: w.tag, power: w.power,
        progress: (t - w.start) / Math.max(w.end - w.start, 0.001) };
    }
  }
  return map;
}

function buildFlashMap() {
  const m = new Map();
  for (let i = 0; i < aligned.length - 1; i++) {
    const et = parseFloat(aligned[i].end || 0);
    if (et <= 0) continue;
    const cfg = FLASH[aligned[i + 1].tag] || FLASH.default;   // الجملة القادمة تحدد الوميض
    const ef = Math.floor(et * FPS);
    for (let k = 0; k < cfg[1]; k++) {
      const fr = ef + k;
      if (fr >= 0 && fr < TOTAL_FRAMES && !m.has(fr)) m.set(fr, { cfg, p: k / Math.max(cfg[1] - 1, 1) });
    }
  }
  return m;
}

function wordAnim(p, sm) {
  if (p < 0.15) { const t = p / 0.15, e = 1 - (1 - t) ** 2; return { s: sm * (0.6 + e * 0.48), o: Math.min(1, t * 3), y: (1 - e) * 30 }; }
  if (p > 0.85) { const t = (p - 0.85) / 0.15; return { s: sm * (1 - t * 0.05), o: 1 - t * 0.3, y: 0 }; }
  return { s: sm, o: 1, y: 0 };
}

const FONT_TABLE = [[2, 170, 160], [4, 150, 140], [6, 130, 120], [9, 110, 102], [12, 92, 86], [99, 76, 72]];
function fontSize(word, ar, sm) {
  const row = FONT_TABLE.find(([m]) => word.length <= m) || FONT_TABLE[FONT_TABLE.length - 1];
  return Math.max(60, Math.min(220, Math.round((ar ? row[1] : row[2]) * sm)));
}

const HOOK_DEFAULT = { ar: "🔴 لا تتجاوز هذا", fr: "🔴 Ne rate pas ça", en: "🔴 Don't skip this", es: "🔴 No te lo saltes" };
const hookText = () => (custom_hook && custom_hook.trim()) || HOOK_DEFAULT[lang] || HOOK_DEFAULT.en;

function titleAnim(f) {
  if (f < INTRO) { const t = f / INTRO, e = 1 - (1 - t) ** 3; return { o: e, y: (1 - e) * -80 }; }
  if (f >= TOTAL_FRAMES - OUTRO) { const t = (f - (TOTAL_FRAMES - OUTRO)) / OUTRO; return { o: 1 - t * t, y: t * t * -60 }; }
  return { o: 1, y: 0 };
}

function flashEffect(fl, f) {
  if (!fl) return { fo: 0, fc: "rgba(0,0,0,0)", sx: 0, sy: 0, sc: 1 };
  const [color, , shake, boost] = fl.cfg;
  const p = fl.p;
  const fo = Math.max(0, Math.min(1, p < 0.3 ? p / 0.3 : 1 - (p - 0.3) / 0.7));
  const s = shake * (1 - p);
  return { fo, fc: color, sx: Math.sin(f * 2.3) * s, sy: Math.cos(f * 1.7) * s,
    sc: boost > 1 && p < 0.5 ? 1 + (boost - 1) * (1 - p * 2) : 1 };
}

function stateKey(st, f, fl) {
  if (f < INTRO) return `i${f}`;
  if (f >= TOTAL_FRAMES - OUTRO) return `o${f}`;
  if (fl) return `t${f}`;
  const h = f < HOOK && has_hook ? "h" : "n";
  if (!st) return `e_${h}`;
  const p = st.progress;
  const b = p < 0.15 ? `pop${Math.floor(p * 15)}` : p > 0.85 ? `fd${Math.floor((p - 0.85) * 30)}` : "hold";
  let hash = 0;
  for (const ch of st.word) hash = ((hash << 5) - hash + ch.charCodeAt(0)) | 0;
  return `w${(hash >>> 0).toString(16)}_${st.tag}_${st.power ? 1 : 0}_${h}_${b}`;
}

function buildHTML(p) {
  const { word, tag, power, hook, f, progress, fl } = p;
  const ar = word ? isArabic(word) : false;
  const [cw, cg, sm, sw, br] = power ? ["#FFF", "rgba(255,23,68,0.9)", 1.15, 2, 1.5] : wordStyle(tag);
  const wa = word ? wordAnim(progress, sm) : { s: 1, o: 0, y: 0 };
  const ta = titleAnim(f);
  const fe = flashEffect(fl, f);
  const fs = fontSize(word || "", ar, sm);
  const font = word ? fontFor(word) : `"Noto Sans",sans-serif`;
  const dir = ar ? "rtl" : "ltr";
  const td = isArabic(display_title) ? "rtl" : "ltr";
  const tfs = isArabic(display_title) ? 52 : 46;
  const ht = hookText();
  const top = Math.max(H * 0.3, Math.min(Math.floor(H * 0.54), H - fs * 1.2 - 30));
  const xf = `translate(-50%,calc(-50% + ${wa.y.toFixed(1)}px)) translate(${fe.sx.toFixed(2)}px,${fe.sy.toFixed(2)}px) scale(${(wa.s * fe.sc).toFixed(4)})`;
  const pill = power
    ? "background:linear-gradient(135deg,#FF1744,#D50000);padding:24px 60px;border-radius:9999px;border:3px solid rgba(255,255,255,0.3);box-shadow:0 0 60px rgba(255,23,68,0.8),0 0 120px rgba(255,23,68,0.4);"
    : "";
  const glow = power ? "" : `color:${cw};text-shadow:0 0 ${sm * 40}px ${cg},0 0 ${sm * 60}px ${cg};filter:brightness(${br});`;

  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:${W}px;height:${H}px;overflow:hidden;background:transparent}
.ot{position:absolute;top:0;left:0;right:0;height:40%;background:linear-gradient(to bottom,rgba(0,0,0,.9),rgba(0,0,0,.55) 50%,transparent)}
.ob{position:absolute;bottom:0;left:0;right:0;height:45%;background:linear-gradient(to top,rgba(0,0,0,.92),rgba(0,0,0,.5) 65%,transparent)}
.fl{position:absolute;inset:0;background:${fe.fc};opacity:${fe.fo.toFixed(4)};z-index:50}
.tc{position:absolute;top:410px;left:50%;width:92%;max-width:980px;direction:${td};text-align:center;z-index:30;
transform:translateX(-50%) translateY(${ta.y.toFixed(2)}px);opacity:${ta.o.toFixed(4)}}
.tc::after{content:'';display:block;margin:16px auto 0;width:120px;height:4px;border-radius:2px;background:linear-gradient(90deg,transparent,#FF1744,transparent)}
.tt{font-family:${fontFor(display_title)};font-size:${tfs}px;font-weight:900;color:#FFF;display:inline-flex;align-items:center;justify-content:center;gap:14px;
line-height:1.3;-webkit-text-stroke:2px rgba(0,0,0,.8);paint-order:stroke fill;text-shadow:0 0 30px rgba(255,23,68,.6),0 4px 20px rgba(0,0,0,.9)}
.hb{position:absolute;top:${isArabic(display_title) ? 290 : 270}px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,rgba(220,0,0,.95),rgba(160,0,0,.95));
color:#fff;font-family:${fontFor(ht)};font-size:${isArabic(ht) ? 32 : 28}px;font-weight:900;padding:12px 38px;border-radius:9999px;z-index:25;white-space:nowrap;
direction:${isArabic(ht) ? "rtl" : "ltr"};border:2px solid rgba(255,120,120,.4);box-shadow:0 0 50px rgba(220,0,0,.7)}
.wc{position:absolute;left:50%;top:${top}px;transform:${xf};opacity:${wa.o.toFixed(4)};direction:${dir};text-align:center;z-index:10;width:95%;max-width:1020px}
.wp{display:inline-block;${pill}}
.wt{font-family:${font};font-size:${fs}px;font-weight:900;color:#FFF;line-height:1.15;letter-spacing:${ar ? 1 : 3}px;display:block;word-break:break-word;
-webkit-text-stroke:${sw}px rgba(0,0,0,.9);paint-order:stroke fill;${glow}}
</style></head><body><div class="ot"></div><div class="ob"></div><div class="fl"></div>
<div class="tc"><div class="tt"><span>${emoji_left}</span><span>${esc(display_title)}</span><span>${emoji_right}</span></div></div>
${hook ? `<div class="hb">${esc(ht)}</div>` : ""}
${word ? `<div class="wc"><div class="wp"><span class="wt">${esc(word)}</span></div></div>` : ""}
</body></html>`;
}

async function renderWordsMov() {
  const { chromium } = await import("playwright");
  const words = buildWordList();
  const fsm = buildFrameState(words);
  const fm = buildFlashMap();
  console.log(`\n📝 words: ${words.length}`);

  const unique = new Map();
  for (let f = 0; f < TOTAL_FRAMES; f++) {
    const fl = fm.get(f) || null;
    const k = stateKey(fsm[f], f, fl);
    if (!unique.has(k)) {
      const st = fsm[f];
      unique.set(k, { word: st?.word || null, tag: st?.tag || "information", power: !!st?.power,
        hook: has_hook && f < HOOK, f, progress: st ? st.progress : 0.5, fl });
    }
  }
  console.log(`📸 ${unique.size} unique states`);

  const browser = await chromium.launch({ headless: true, args: [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--font-render-hinting=none"] });
  const cache = new Map();
  try {
    const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, locale: "en-US" });
    const page = await ctx.newPage();
    let n = 0;
    for (const [k, s] of unique) {
      const hp = join(TMP, `${k}.html`);
      writeFileSync(hp, buildHTML(s), "utf-8");
      await page.goto(`file://${hp}`, { waitUntil: "load", timeout: 15000 });
      await page.evaluate(() => Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, 4000))]));
      if (n === 0) await page.waitForTimeout(600);        // أول صفحة: انتظر الخطوط
      const pp = join(TMP, `${k}.png`);
      await page.screenshot({ path: pp, type: "png", omitBackground: true });
      cache.set(k, pp);
      rm(hp);
      if (++n % 100 === 0) console.log(`  ${n}/${unique.size}`);
    }
  } finally {
    try { await browser.close(); } catch {}
  }

  const dir = join(TMP, "frames");
  mkdirSync(dir, { recursive: true });
  const empty = cache.get("e_n") || cache.get("e_h") || [...cache.values()][0];
  for (let f = 0; f < TOTAL_FRAMES; f++) {
    const src = cache.get(stateKey(fsm[f], f, fm.get(f) || null)) || empty;
    const dst = join(dir, `frame_${pad(f, 6)}.png`);
    try { symlinkSync(src, dst); } catch { copyFileSync(src, dst); }
  }
  const mov = join(TMP, "words.mov");
  if (!ff(["-y", "-framerate", String(FPS), "-i", join(dir, "frame_%06d.png"),
    "-c:v", "png", "-pix_fmt", "rgba", mov], "frames-mov")) throw new Error("frames→mov failed");
  return mov;
}

// ═══════════════════════════════════════════════════════════════════════════
// 6) الدمج النهائي: لمسة الفيلم + fade + كلمات/ASS + صوت + ميتاداتا نظيفة
// ═══════════════════════════════════════════════════════════════════════════

// -map_metadata -1 : ميتاداتا نظيفة محايدة (بدون أي ادّعاء)
const CLEAN_META = ["-map_metadata", "-1", "-fflags", "+bitexact"];

async function finalize(bg) {
  const fades = `fade=t=in:st=0:d=0.25,fade=t=out:st=${Math.max(0, TOTAL - 0.4).toFixed(3)}:d=0.4`;
  const look = `${filmLook()},${fades}`;
  const enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
    "-r", String(FPS), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
    "-t", TOTAL.toFixed(3), ...CLEAN_META];

  if (stage === "bg") {
    return ff(["-y", "-i", bg, "-i", audio, "-filter_complex", `[0:v]${look}[v]`,
      "-map", "[v]", "-map", "1:a:0", ...enc, outputPath], "final-bg");
  }

  if (isLong) {
    const assFile = join(TMP, "subs.ass");
    writeFileSync(assFile, buildAss(), "utf-8");
    const fontsDir = ["/usr/share/fonts/truetype", "/usr/share/fonts", "/usr/local/share/fonts"]
      .find((d) => existsSync(d)) || "/usr/share/fonts";
    const a = assFile.replace(/\\/g, "/").replace(/:/g, "\\:").replace(/ /g, "\\ ");
    const ok = ff(["-y", "-i", bg, "-i", audio,
      "-filter_complex", `[0:v]${look}[g];[g]ass='${a}':fontsdir='${fontsDir}'[v]`,
      "-map", "[v]", "-map", "1:a:0", ...enc, outputPath], "final-ass");
    if (ok) return true;
    console.log("  ⚠️ ASS failed → video without subtitles");
    return ff(["-y", "-i", bg, "-i", audio, "-filter_complex", `[0:v]${look}[v]`,
      "-map", "[v]", "-map", "1:a:0", ...enc, outputPath], "final-noass");
  }

  // Short: كلمات متحركة
  let mov = null;
  try { mov = await renderWordsMov(); }
  catch (e) { console.log(`  ⚠️ words overlay failed: ${e.message} → no words`); }
  if (mov) {
    const ok = ff(["-y", "-i", bg, "-i", mov, "-i", audio, "-filter_complex",
      `[0:v]${look}[g];[1:v]format=rgba[cap];[g][cap]overlay=0:0:format=auto,format=yuv420p[v]`,
      "-map", "[v]", "-map", "2:a:0", ...enc, outputPath], "final-words");
    if (ok) return true;
  }
  return ff(["-y", "-i", bg, "-i", audio, "-filter_complex", `[0:v]${look}[v]`,
    "-map", "[v]", "-map", "1:a:0", ...enc, outputPath], "final-plain");
}

// ═══════════════════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════════════════

(async () => {
  const bg = buildBackground();
  const ok = await finalize(bg);
  if (!ok || !existsSync(outputPath)) { console.error("❌ final render failed"); process.exit(1); }
  const dur = probeDuration(outputPath);
  console.log(`\n🎉 ${outputPath} (${dur.toFixed(2)}s vs audio ${TOTAL.toFixed(2)}s)`);
  if (Math.abs(dur - TOTAL) > 1.0) console.log("  ⚠️ duration drift > 1s");
})().catch((e) => { console.error("❌", e.message || e); process.exit(1); });
