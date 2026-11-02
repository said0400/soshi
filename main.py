#!/usr/bin/env python3
"""
main.py — المنسّق الرئيسي (v2.0)

يعمل على GitHub Actions للإنتاج فقط. النشر يتولاه Cloudflare Worker (مجلد worker/).

سير العمل:
  1) produce: لكل موعد نشر اقترب → ينتج الفيديو → يرفعه إلى R2
              ويكتب بجانبه  queue/{وقت}_{لغة}_{نوع}_{رقم}.json
  2) Cloudflare Worker (كل دقيقة): يقرأ queue/ ويرسل Webhook إلى Make عند الموعد
              ثم ينقل الملف إلى sent/ ويحذف كل شيء بعد 48 ساعة.

حالة الإنتاج (أرقام مستخدمة، كاش AI، مخزون مستخدم) في قاعدة لكل لغة:
  R2: state/vsg_{lang}.db  ← تُنزَّل في بداية التشغيل وتُرفع بعد كل فيديو.
  (concurrency في الـ workflow يمنع تشغيلين متزامنين لنفس اللغة)

الأوامر:
  produce   ينتج ما اقترب موعده            --lang ar|fr|en|es|all  --mode short|long|all
  due       يطبع اللغات التي لها فيديو مستحق الإنتاج (JSON)
  run       إنتاج يدوي لفيديو واحد         --lang --mode [--video-number] [--no-upload]
  check     فحص البيئة والمفاتيح
  status    ملخص القواعد
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import config as C
import db
from audio import get_audio_duration, produce_audio
from content import AIError, build_publish_payload, enrich, process_content, read_scripts
from media import fetch_videos, make_fb_long, make_long_thumbnail

log = logging.getLogger("main")

EMOJI_LEFT, EMOJI_RIGHT = "🔥", "💥"
MIN_CLIP = 0.8
MAX_SHORT_CLIP = 15.0

VIDEOS_PREFIX = "videos/"
QUEUE_PREFIX = "queue/"
SENT_PREFIX = "sent/"
STATE_PREFIX = "state/"
PLATFORMS = ("youtube", "facebook")      # أضف "instagram" هنا + مسار في Make لتفعيله


# ═══════════════════════════════════════════════════════════════
# 1) Cloudflare R2
# ═══════════════════════════════════════════════════════════════

class R2:
    """عميل R2 (S3 API). boto3 يُستورد كسولاً."""

    def __init__(self) -> None:
        self.cfg = C.r2_settings()
        self._client: Any = None

    def configured(self) -> bool:
        return all(self.cfg[k] for k in ("account_id", "access_key", "secret_key", "bucket"))

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client(
                "s3",
                endpoint_url=f"https://{self.cfg['account_id']}.r2.cloudflarestorage.com",
                aws_access_key_id=self.cfg["access_key"],
                aws_secret_access_key=self.cfg["secret_key"],
                region_name="auto",
                config=Config(retries={"max_attempts": 5, "mode": "standard"},
                              signature_version="s3v4"))
        return self._client

    def upload(self, path: Path, key: str, content_type: str) -> str:
        """يرفع ملفاً ويرجع رابطاً يحمّله Make."""
        log.info("  ☁️  R2 ↑ %s (%.1f MB)", key, path.stat().st_size / 1_048_576)
        self.client.upload_file(str(path), self.cfg["bucket"], key,
                                ExtraArgs={"ContentType": content_type})
        return self.url(key)

    def url(self, key: str) -> str:
        if self.cfg["public_url"]:
            return f"{self.cfg['public_url']}/{key}"
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.cfg["bucket"], "Key": key},
            ExpiresIn=C.R2_PRESIGN_SECONDS)

    def put_json(self, key: str, obj: dict) -> None:
        self.client.put_object(Bucket=self.cfg["bucket"], Key=key,
                               Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                               ContentType="application/json")

    def download(self, key: str, dest: Path) -> bool:
        """يحمّل ملفاً. False إن لم يوجد."""
        try:
            self.client.download_file(self.cfg["bucket"], key, str(dest))
            return True
        except Exception as e:
            msg = str(e)
            if "404" in msg or "Not Found" in msg or "NoSuchKey" in msg:
                return False
            raise

    def any_with_prefix(self, prefix: str) -> bool:
        r = self.client.list_objects_v2(Bucket=self.cfg["bucket"], Prefix=prefix, MaxKeys=1)
        return bool(r.get("KeyCount", 0))


_r2: Optional[R2] = None


def r2() -> R2:
    global _r2
    if _r2 is None:
        _r2 = R2()
    return _r2


# ═══════════════════════════════════════════════════════════════
# 2) حالة الإنتاج: قاعدة لكل لغة في R2
# ═══════════════════════════════════════════════════════════════

class _State:
    def __init__(self, lang: str, local: Path, remote_key: str, remote: bool, write: bool):
        self.lang, self.local, self.remote_key = lang, local, remote_key
        self.remote, self.write = remote, write

    def save(self) -> None:
        """يدمج WAL ويرفع القاعدة إلى R2 (بعد كل فيديو لتفادي ضياع الحالة)."""
        if not (self.remote and self.write):
            return
        db.checkpoint_and_close()
        r2().upload(self.local, self.remote_key, "application/octet-stream")
        db.use_database(self.local)
        db.init_db()


@contextmanager
def lang_state(lang: str, remote: bool = True, write: bool = True) -> Iterator[_State]:
    """ينزّل vsg_{lang}.db من R2، يفتحها، وعند الخروج يرفعها (إن write)."""
    use_remote = remote and r2().configured()
    local = C.BASE_DIR / f"vsg_{lang}.db"
    key = f"{STATE_PREFIX}vsg_{lang}.db"
    if use_remote:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{local}{suffix}").unlink(missing_ok=True)
        if r2().download(key, local):
            log.info("  📥 state %s restored from R2", lang.upper())
        else:
            log.info("  🆕 state %s: new database", lang.upper())
    db.use_database(local)
    db.init_db()
    st = _State(lang, local, key, use_remote, write)
    try:
        yield st
    finally:
        db.checkpoint_and_close()
        if use_remote and write:
            try:
                r2().upload(local, key, "application/octet-stream")
            except Exception as e:
                log.error("  ❌ cannot upload state %s: %s", lang, e)
                raise


# ═══════════════════════════════════════════════════════════════
# 3) الجدولة
# ═══════════════════════════════════════════════════════════════

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def slots_between(lang: str, mode: str, start: datetime, end: datetime) -> list[datetime]:
    """مواعيد النشر (UTC) بين start و end.

    Short: [(h, m)] يومياً.   Long: [(weekday, h, m)] أسبوعياً (0=الاثنين).
    في يوم الطويل يُحذف آخر Short (DROP_LAST_SHORT_ON_LONG_DAY).
    """
    spec = C.PUBLISH_SLOTS.get(lang, {}).get(mode, [])
    if not spec:
        return []
    tz = C.tz_of(lang)
    first = (start.astimezone(tz) - timedelta(days=1)).date()
    last = (end.astimezone(tz) + timedelta(days=1)).date()
    long_days = {s[0] for s in C.PUBLISH_SLOTS.get(lang, {}).get("long", [])}
    out: list[datetime] = []
    d = first
    while d <= last:
        items = list(spec)
        if mode == "short" and C.DROP_LAST_SHORT_ON_LONG_DAY and d.weekday() in long_days:
            items = sorted(items)[:-1]
        for item in items:
            if mode == "short":
                h, m = item
            else:
                wd, h, m = item
                if d.weekday() != wd:
                    continue
            u = datetime(d.year, d.month, d.day, h, m, tzinfo=tz).astimezone(timezone.utc)
            if start <= u <= end:
                out.append(u)
        d += timedelta(days=1)
    return sorted(out)


def slot_key(lang: str, mode: str, when: datetime) -> str:
    return f"{lang}_{mode}_{when.strftime('%Y%m%dT%H%M')}"


def slots_to_produce(lang: str, mode: str, now: datetime) -> list[datetime]:
    """مواعيد دخلت نافذة الإنتاج ولم يُنتج لها شيء بعد."""
    lead = C.PRE_PRODUCE_LEAD_MIN[mode] + C.PRODUCE_SAFETY_MIN
    todo: list[datetime] = []
    for when in slots_between(lang, mode, now - timedelta(minutes=C.MAX_LATE_MIN),
                              now + timedelta(minutes=lead)):
        rec = db.get_video_by_slot(slot_key(lang, mode, when))
        if rec is None or rec["status"] == "failed":
            todo.append(when)
    return todo


def already_queued(lang: str, mode: str, when: datetime) -> bool:
    """حماية من التكرار: هل يوجد في R2 عنصر لنفس الموعد (queue/ أو sent/)؟"""
    if not r2().configured():
        return False
    stem = f"{compact(when)}_{lang}_{mode}_"
    return r2().any_with_prefix(QUEUE_PREFIX + stem) or r2().any_with_prefix(SENT_PREFIX + stem)


# ═══════════════════════════════════════════════════════════════
# 4) خطة المقاطع
# ═══════════════════════════════════════════════════════════════

def short_clip_plan(aligned: list[dict], ai: dict, total: float
                    ) -> tuple[list[list[str]], list[str], list[float]]:
    """مقطع لكل جملة. أول مقطع يبدأ من 0، وآخر مقطع يمتد حتى نهاية الصوت."""
    kws: list[list[str]] = []
    tags: list[str] = []
    durs: list[float] = []
    vk = ai.get("visual_keywords") or []
    for i, a in enumerate(aligned):
        start = 0.0 if i == 0 else a["start"]
        end = total if i == len(aligned) - 1 else a["end"]
        durs.append(min(MAX_SHORT_CLIP, max(MIN_CLIP, end - start)))
        tags.append(a["tag"])
        kws.append(list(vk[i]) if i < len(vk) and vk[i] else [])
    hook = (ai.get("hook_keyword") or "").strip()
    if kws and hook:
        kws[0] = [hook] + [k for k in kws[0] if k.lower() != hook.lower()]
    return kws, tags, durs


def long_clip_plan(aligned: list[dict], ai: dict, total: float
                   ) -> tuple[list[list[str]], list[str], list[float]]:
    """مقاطع بطول ثابت (~6s، وتكبر لو تجاوزت 80 مقطعاً) تتبع الفقرة النشطة."""
    chunk = C.BACKGROUND_CHUNK_DUR_LONG
    if total / chunk > C.MAX_LONG_CHUNKS:
        chunk = round(total / C.MAX_LONG_CHUNKS, 1)
    n = max(1, int(round(total / chunk)))
    vk = ai.get("visual_keywords") or []
    kws: list[list[str]] = []
    tags: list[str] = []
    durs: list[float] = []
    seen_in_unit: dict[int, int] = {}
    for c in range(n):
        t0, t1 = c * total / n, (c + 1) * total / n
        mid = (t0 + t1) / 2
        ui = next((i for i, a in enumerate(aligned) if a["start"] <= mid < a["end"]),
                  len(aligned) - 1)
        row = list(vk[ui]) if ui < len(vk) and vk[ui] else []
        k = seen_in_unit.get(ui, 0)
        seen_in_unit[ui] = k + 1
        if row:
            row = row[k % len(row):] + row[:k % len(row)]     # كلمات مختلفة لكل مقطع
        kws.append(row)
        tags.append(aligned[ui]["tag"] if aligned else C.DEFAULT_TAG)
        durs.append(t1 - t0)
    return kws, tags, durs


# ═══════════════════════════════════════════════════════════════
# 5) الريندر (Node)
# ═══════════════════════════════════════════════════════════════

def run_render(manifest: dict, manifest_path: Path, output: Path, mode: str) -> None:
    if not C.RENDER_SCRIPT.exists():
        raise FileNotFoundError(f"render script missing: {C.RENDER_SCRIPT}")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    timeout = C.RENDER_TIMEOUT[mode]
    log.info("  🎬 render [%s] timeout %d min", mode, timeout // 60)
    try:
        r = subprocess.run(["node", str(C.RENDER_SCRIPT), str(manifest_path), str(output)],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(C.RENDER_SCRIPT.parent))
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"render timeout ({timeout}s)") from e
    log.info("[render]\n%s", "\n".join((r.stdout or "").strip().splitlines()[-12:]))
    if r.returncode != 0 or not output.exists():
        raise RuntimeError(f"render failed: {(r.stderr or r.stdout or '')[-500:]}")


# ═══════════════════════════════════════════════════════════════
# 6) بناء فيديو + رفعه
# ═══════════════════════════════════════════════════════════════

def stable_seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def build_video(record: dict, lang: str, mode: str, out_dir: Path,
                force_ai: bool = False) -> dict:
    """ينتج الفيديو محلياً. يرجع: final, fb (أو None), thumb (أو None), duration, publish."""
    num = str(record["number"])
    out_base = str(out_dir / f"video_{num}_{lang}_{mode}")
    t0 = time.time()

    units = process_content(record["content"], lang, mode)
    if not units:
        raise RuntimeError("no content units")
    ai = enrich(record, lang, mode, units, force=force_ai)

    seed = stable_seed(record["title"] + lang)
    audio_path, duration, aligned = produce_audio(ai["tagged"], lang, mode, out_base, seed=seed)

    plan = short_clip_plan if mode == "short" else long_clip_plan
    kws, tags, durs = plan(aligned, ai, duration)

    vid_dir = out_dir / f"clips_{num}_{lang}_{mode}"
    clips_paths = fetch_videos(kws, tags, mode, vid_dir)

    dims = C.DIMENSIONS[mode]
    final = Path(f"{out_base}_final.mp4")
    manifest = {
        "content_mode": mode, "lang": lang, "width": dims["width"], "height": dims["height"],
        "fps": C.FPS, "audio": str(Path(audio_path).resolve()), "duration_s": duration,
        "clips": [{"video": str(Path(p).resolve()), "duration": d, "tag": t}
                  for p, d, t in zip(clips_paths, durs, tags)],
        "aligned": aligned,
        "title": record["title"], "display_title": record["title"],
        "emoji_left": EMOJI_LEFT, "emoji_right": EMOJI_RIGHT,
        "power_words": ai.get("power_words", []),
        "custom_hook": ai.get("custom_hook", ""),
        "has_hook": mode == "short",
        "bg_style": ai.get("bg_style", "video"),
        "primary_emotion": ai.get("analysis", {}).get("primary_emotion", ""),
        "stage": "full",
    }
    run_render(manifest, Path(f"{out_base}_manifest.json"), final, mode)

    rd = get_audio_duration(final)
    if abs(rd - duration) > 1.5:
        raise RuntimeError(f"render duration drift: video {rd:.1f}s vs audio {duration:.1f}s")

    fb: Optional[Path] = None
    thumb: Optional[Path] = None
    if mode == "long":
        thumb = make_long_thumbnail(record, lang, out_base, ai.get("hook_keyword", ""),
                                    clips_paths)
        fb = make_fb_long(final, lang, f"{out_base}_fb.mp4")

    shutil.rmtree(vid_dir, ignore_errors=True)
    log.info("  ⏱️  #%s built in %.1f min", num, (time.time() - t0) / 60)
    return {"final": final, "fb": fb, "thumb": thumb, "duration": duration,
            "publish": build_publish_payload(record, ai, lang, mode)}


def _asset_key(lang: str, mode: str, num: str, kind: str, ext: str) -> str:
    stamp = now_utc().strftime("%Y%m%dT%H%M%S")
    return f"{VIDEOS_PREFIX}{lang}/{mode}/{num}_{stamp}_{kind}.{ext}"


def upload_video(built: dict, lang: str, mode: str, num: str) -> dict:
    """Short: ملف واحد للمنصتين. Long: يوتيوب أفقي + فيسبوك عمودي + صورة مصغرة."""
    store = r2()
    yt_key = _asset_key(lang, mode, num, "yt", "mp4")
    yt_url = store.upload(built["final"], yt_key, "video/mp4")
    if mode == "short":
        fb_key, fb_url = yt_key, yt_url
    else:
        fb_key = _asset_key(lang, mode, num, "fb", "mp4")
        fb_url = store.upload(built["fb"], fb_key, "video/mp4")
    thumb_key = thumb_url = ""
    if built.get("thumb"):
        thumb_key = _asset_key(lang, mode, num, "thumb", "jpg")
        thumb_url = store.upload(built["thumb"], thumb_key, "image/jpeg")
    return {"yt_key": yt_key, "fb_key": fb_key, "thumb_key": thumb_key,
            "yt_url": yt_url, "fb_url": fb_url, "thumb_url": thumb_url}


# ═══════════════════════════════════════════════════════════════
# 7) صندوق النشر (queue/*.json) — يقرؤه Cloudflare Worker
# ═══════════════════════════════════════════════════════════════

def build_queue_item(sid: str, lang: str, mode: str, num: str, title: str,
                     publish_at: datetime, payload: dict, up: dict) -> dict:
    """عنصر واحد لكل فيديو. الـ Worker يرسل body كما هو ويضيف auth من أسراره."""
    common = {
        "lang": lang, "content_mode": mode, "video_number": num, "title": payload["title"],
        "publish_at": iso(publish_at), "is_short": mode == "short",
        "hashtags": payload["hashtags"],
    }
    bodies = {
        "youtube": {**common, "platform": "youtube", "video_url": up["yt_url"],
                    "description": payload["youtube_description"],
                    "tags": payload["youtube_tags"], "thumbnail_url": up.get("thumb_url", "")},
        "facebook": {**common, "platform": "facebook", "video_url": up["fb_url"],
                     "description": payload["facebook_caption"]},
    }
    platforms = {}
    for p in PLATFORMS:
        body = dict(bodies[p])
        body["idempotency_key"] = f"{sid}_{p}"          # لتفادي التكرار داخل Make
        platforms[p] = {"body": body, "sent_at": None, "attempts": 0, "last_error": None}
    return {
        "version": 1, "id": sid, "lang": lang, "content_mode": mode,
        "video_number": num, "title": title,
        "publish_at": iso(publish_at), "created_at": iso(now_utc()),
        "platforms": platforms,
        "assets": [k for k in (up["yt_key"], up["fb_key"], up["thumb_key"]) if k],
    }


def queue_key(lang: str, mode: str, num: str, publish_at: datetime) -> str:
    return f"{QUEUE_PREFIX}{compact(publish_at)}_{lang}_{mode}_{num}.json"


# ═══════════════════════════════════════════════════════════════
# 8) إنتاج موعد واحد
# ═══════════════════════════════════════════════════════════════

def pick_record(lang: str, mode: str, number: Optional[str] = None) -> Optional[dict]:
    records = read_scripts(C.script_path(lang, mode))
    if not records:
        return None
    by_num = {str(r["number"]): r for r in records}
    if number:
        return by_num.get(str(number))
    numbers = [str(r["number"]) for r in records]
    nxt = db.get_next_video_number(lang, mode, numbers)
    if nxt is None:
        log.info("  🔄 all %s/%s scripts used — new cycle", lang, mode)
        db.start_new_cycle(lang, mode)
        nxt = db.get_next_video_number(lang, mode, numbers)
    return by_num.get(nxt) if nxt else None


def produce_slot(lang: str, mode: str, when: datetime, out_dir: Path, state: _State,
                 force_ai: bool = False, record: Optional[dict] = None,
                 key_override: Optional[str] = None) -> bool:
    """ينتج ويرفع ويضع عنصراً في queue/ لموعد محدد."""
    sid = key_override or slot_key(lang, mode, when)
    prev = db.get_video_by_slot(sid)
    if prev and prev["status"] == "failed":
        db.delete_video(prev["id"])
    if not key_override and already_queued(lang, mode, when):
        log.warning("  ⏭️  %s already in R2 queue/sent — skipping duplicate", sid)
        return False

    record = record or pick_record(lang, mode)
    if record is None:
        log.error("  ❌ no script available for %s/%s", lang, mode)
        return False
    num = str(record["number"])
    log.info("\n%s\n  🎬 #%s [%s/%s] → publish %s", "═" * 62, num, lang.upper(),
             mode.upper(), iso(when))
    try:
        vid = db.create_video(num, lang, mode, record["title"], iso(when), sid)
    except Exception as e:
        log.warning("  ⚠️  cannot reserve slot/number (%s) — skipping", e)
        return False

    db.set_status(vid, "rendering")
    work = out_dir / f"w_{num}_{lang}_{mode}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        built = build_video(record, lang, mode, work, force_ai)
        up = upload_video(built, lang, mode, num)
        item = build_queue_item(sid, lang, mode, num, record["title"], when,
                                built["publish"], up)
        r2().put_json(queue_key(lang, mode, num, when), item)       # ← يظهر للـ Worker الآن
        db.mark_ready(vid, built["duration"], yt_path=up["yt_url"], fb_path=up["fb_url"],
                      yt_r2_key=up["yt_key"], fb_r2_key=up["fb_key"],
                      thumb_r2_key=up["thumb_key"])
        state.save()                                                # احفظ الحالة فوراً
        log.info("  ✅ #%s queued — Worker will send at %s", num, iso(when))
        return True
    except Exception as e:
        log.error("  ❌ #%s failed: %s", num, e)
        traceback.print_exc()
        db.mark_failed(vid, str(e))
        try:
            state.save()
        except Exception:
            pass
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 9) الأوامر
# ═══════════════════════════════════════════════════════════════

def _langs(arg: str) -> list[str]:
    return list(C.LANGS) if arg == "all" else [arg]


def _modes(arg: str) -> list[str]:
    return list(C.MODES) if arg == "all" else [arg]


def cmd_produce(args: argparse.Namespace) -> int:
    n = parse_iso(args.now) if args.now else now_utc()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total_ok = total_jobs = 0

    for lang in _langs(args.lang):
        with lang_state(lang, write=not args.dry_run) as st:
            db.reset_stuck_rendering()
            jobs = sorted((when, mode) for mode in _modes(args.mode)
                          for when in slots_to_produce(lang, mode, n))
            if not jobs:
                log.info("  ℹ️  %s: nothing to produce (%s)", lang.upper(), iso(n))
                continue
            log.info("  📋 %s: %d slot(s) to produce", lang.upper(), len(jobs))
            for when, mode in jobs[: args.max_videos]:
                total_jobs += 1
                if args.dry_run:
                    log.info("  🔍 [dry-run] would produce %s/%s for %s", lang, mode, iso(when))
                    continue
                total_ok += int(produce_slot(lang, mode, when, out_dir, st, args.force_ai))
    if args.dry_run or total_jobs == 0:
        return 0
    return 0 if total_ok else 1


def cmd_due(args: argparse.Namespace) -> int:
    """يطبع ["ar","es"] للغات التي لها موعد دخل نافذة الإنتاج."""
    n = parse_iso(args.now) if args.now else now_utc()
    due: list[str] = []
    for lang in _langs(args.lang):
        with lang_state(lang, write=False):
            if any(slots_to_produce(lang, mode, n) for mode in C.MODES):
                due.append(lang)
    out = json.dumps(due)
    print(out)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"langs={out}\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """إنتاج يدوي لفيديو واحد. --no-upload يبقي الملفات محلياً بلا R2."""
    lang, mode = args.lang, args.mode
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with lang_state(lang, remote=not args.no_upload) as st:
        record = pick_record(lang, mode, args.video_number)
        if record is None:
            log.error("❌ no record found")
            return 1
        if args.no_upload:
            built = build_video(record, lang, mode, out_dir, args.force_ai)
            for k in ("final", "fb", "thumb"):
                if built[k]:
                    log.info("  📁 %s", built[k])
            return 0
        when = now_utc() + timedelta(minutes=args.publish_in)
        sid = f"manual_{lang}_{mode}_{when.strftime('%Y%m%dT%H%M')}"
        prev = db.get_video(record["number"], lang, mode)
        if prev:
            db.delete_video(prev["id"])
        ok = produce_slot(lang, mode, when, out_dir, st, args.force_ai,
                          record=record, key_override=sid)
        return 0 if ok else 1


def cmd_check(_: argparse.Namespace) -> int:
    ok = True

    def line(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  {'✅' if good else '❌'} {name}{(' — ' + detail) if detail else ''}")

    print("\n  🔎 Environment check\n")
    for tool in ("ffmpeg", "ffprobe", "node"):
        line(tool, shutil.which(tool) is not None)
    line("render.mjs", C.RENDER_SCRIPT.exists())
    for prefix, required in (("GEMINI_API_KEY", True), ("GROQ_API_KEY", False),
                             ("PEXELS_API_KEY", True), ("PIXABAY_API_KEY", False)):
        n = len(C.get_ring(prefix))
        line(prefix, n > 0 or not required, f"{n} key(s)")
    line("R2", r2().configured(),
         "public URL" if C.r2_settings()["public_url"] else "presigned URLs")
    for lang in C.LANGS:
        for mode in C.MODES:
            line(f"scripts/{lang}_{mode}.xlsx", C.script_path(lang, mode).exists())
        line(f"assets/bg_{lang}.png", C.bg_image_path(lang).exists(), "FB long background")
        spec = C.PUBLISH_SLOTS.get(lang, {})
        line(f"slots {lang.upper()}", bool(spec.get("short")) and bool(spec.get("long")),
             f"{len(spec.get('short', []))} short/day, {len(spec.get('long', []))} long/week")
    for sub in ("hook", "whoosh", "impact", "riser"):
        d = C.SFX_DIR / sub
        line(f"sfx/{sub}", d.is_dir() and any(d.iterdir()))
    has_music = C.MUSIC_DIR.is_dir() and any(
        p.suffix.lower() in (".mp3", ".wav", ".m4a") for p in C.MUSIC_DIR.rglob("*"))
    line("music", has_music, "assets/music")
    print("\n  (روابط Make تُضبط كأسرار في Cloudflare Worker وليس هنا)\n")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    for lang in _langs(args.lang):
        with lang_state(lang, write=False):
            print(f"\n── {lang.upper()} ──")
            db.print_db_summary()
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="🎬 Video pipeline",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("produce", help="إنتاج ما اقترب موعده")
    sp.add_argument("--lang", default="all", choices=["all", *C.LANGS])
    sp.add_argument("--mode", default="all", choices=["all", *C.MODES])
    sp.add_argument("--max-videos", type=int, default=3, help="لكل لغة في التشغيل الواحد")
    sp.add_argument("--now", help="تجاوز الوقت الحالي (UTC ISO) للاختبار")
    sp.add_argument("--force-ai", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--output-dir", default=str(C.OUTPUT_DIR))
    sp.set_defaults(fn=cmd_produce)

    sp = sub.add_parser("due", help="اللغات المستحقة (JSON)")
    sp.add_argument("--lang", default="all", choices=["all", *C.LANGS])
    sp.add_argument("--now")
    sp.set_defaults(fn=cmd_due)

    sp = sub.add_parser("run", help="إنتاج يدوي لفيديو واحد")
    sp.add_argument("--lang", required=True, choices=C.LANGS)
    sp.add_argument("--mode", required=True, choices=C.MODES)
    sp.add_argument("--video-number")
    sp.add_argument("--no-upload", action="store_true")
    sp.add_argument("--publish-in", type=int, default=5, help="بعد كم دقيقة يُرسل (دقائق)")
    sp.add_argument("--force-ai", action="store_true")
    sp.add_argument("--output-dir", default=str(C.OUTPUT_DIR))
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("check", help="فحص البيئة")
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("status", help="ملخص القواعد")
    sp.add_argument("--lang", default="all", choices=["all", *C.LANGS])
    sp.set_defaults(fn=cmd_status)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    C.setup_logging()
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        log.warning("⛔ interrupted")
        return 130
    except AIError as e:
        log.error("❌ AI: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
