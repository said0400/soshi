#!/usr/bin/env python3
"""
main.py — المنسّق الرئيسي (v3.0)

يعمل على GitHub Actions للإنتاج. لا خوادم ولا بطاقة دفع:

  1) produce : لكل موعد نشر اقترب → ينتج الفيديو → يرفعه كأصل في GitHub Release "media"
               ويكتب صفاً في Google Sheet (عبر Apps Script) فيه كل بيانات النشر.
  2) Google Apps Script (كل دقيقة): عند حلول الموعد يرسل Webhook واحداً إلى سيناريو
               Make الواحد، فيحمّل Make الفيديو من الرابط وينشر على يوتيوب وفيسبوك.
  3) cleanup : يحذف أصول الفيديو الأقدم من 48 ساعة.

حالة الإنتاج (أرقام مستخدمة، كاش AI، مخزون مستخدم) في قاعدة لكل لغة:
  Release "state": vsg_{lang}.db ← تُنزَّل في بداية التشغيل وتُرفع بعد كل فيديو.
  (concurrency في الـ workflow يمنع تشغيلين متزامنين لنفس اللغة)

الأوامر:
  produce   ينتج ما اقترب موعده            --lang ar|fr|en|es|all  --mode short|long|all
  due       يطبع اللغات التي لها فيديو مستحق الإنتاج (JSON)
  cleanup   حذف أصول الفيديو الأقدم من 48 ساعة
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

import requests

import config as C
import db
from audio import get_audio_duration, produce_audio
from content import AIError, build_publish_payload, enrich, process_content, read_scripts
from media import fetch_videos, make_fb_long, make_long_thumbnail

log = logging.getLogger("main")

EMOJI_LEFT, EMOJI_RIGHT = "🔥", "💥"
MIN_CLIP = 0.8
MAX_SHORT_CLIP = 15.0



# ═══════════════════════════════════════════════════════════════
# 1) GitHub Releases (تخزين الفيديوهات + حالة اللغات)
# ═══════════════════════════════════════════════════════════════

class GitHubStore:
    """يخزّن الملفات كأصول (assets) في Releases. المستودع عام فالروابط عامة."""

    API = "https://api.github.com"
    UPLOAD = "https://uploads.github.com"

    def __init__(self) -> None:
        g = C.github_settings()
        self.token, self.repo = g["token"], g["repo"]
        self._releases: dict[str, dict] = {}

    def configured(self) -> bool:
        return bool(self.token and self.repo)

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", **extra}

    def release(self, tag: str) -> dict:
        """يجلب الـ Release بوسم معيّن أو ينشئه."""
        if tag in self._releases:
            return self._releases[tag]
        url = f"{self.API}/repos/{self.repo}/releases"
        r = requests.get(f"{url}/tags/{tag}", headers=self._headers(), timeout=30)
        if r.status_code == 404:
            r = requests.post(url, headers=self._headers(), timeout=30, json={
                "tag_name": tag, "name": tag, "prerelease": True,
                "body": "Automated storage — do not edit or delete."})
            if r.status_code == 422:                        # سباق: أنشأه job آخر للتو
                r = requests.get(f"{url}/tags/{tag}", headers=self._headers(), timeout=30)
        r.raise_for_status()
        self._releases[tag] = r.json()
        return self._releases[tag]

    def assets(self, tag: str) -> list[dict]:
        rid = self.release(tag)["id"]
        out: list[dict] = []
        page = 1
        while True:
            r = requests.get(f"{self.API}/repos/{self.repo}/releases/{rid}/assets",
                             headers=self._headers(), params={"per_page": 100, "page": page},
                             timeout=30)
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def delete_asset(self, asset_id: int) -> None:
        r = requests.delete(f"{self.API}/repos/{self.repo}/releases/assets/{asset_id}",
                            headers=self._headers(), timeout=30)
        if r.status_code not in (204, 404):
            r.raise_for_status()

    def upload_asset(self, path: Path, name: str, content_type: str, tag: str,
                     replace: bool = False) -> str:
        """يرفع ملفاً (حتى 2GB) ويرجع رابط التحميل العام."""
        rel = self.release(tag)
        if replace:
            for a in self.assets(tag):
                if a["name"] == name:
                    self.delete_asset(a["id"])
        log.info("  ☁️  GitHub ↑ %s (%.1f MB)", name, path.stat().st_size / 1_048_576)
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                with open(path, "rb") as f:
                    r = requests.post(
                        f"{self.UPLOAD}/repos/{self.repo}/releases/{rel['id']}/assets",
                        params={"name": name}, data=f, timeout=(30, 900),
                        headers=self._headers(**{"Content-Type": content_type}))
                if r.status_code == 422:               # الاسم موجود (محاولة سابقة انقطعت)
                    for a in self.assets(tag):
                        if a["name"] == name:
                            self.delete_asset(a["id"])
                    continue
                r.raise_for_status()
                return r.json()["browser_download_url"]
            except Exception as e:
                last = e
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"upload failed for {name}: {last}")

    def download_asset(self, tag: str, name: str, dest: Path) -> bool:
        """يحمّل أصلاً بالاسم. False إن لم يوجد."""
        for a in self.assets(tag):
            if a["name"] != name:
                continue
            with requests.get(f"{self.API}/repos/{self.repo}/releases/assets/{a['id']}",
                              headers=self._headers(Accept="application/octet-stream"),
                              stream=True, timeout=(30, 300)) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            return True
        return False

    def delete_older_than(self, tag: str, hours: int) -> int:
        cutoff = now_utc() - timedelta(hours=hours)
        n = 0
        for a in self.assets(tag):
            created = datetime.strptime(a["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            if created < cutoff:
                self.delete_asset(a["id"])
                n += 1
        return n


_store: Optional[GitHubStore] = None


def store() -> GitHubStore:
    global _store
    if _store is None:
        _store = GitHubStore()
    return _store


# ═══════════════════════════════════════════════════════════════
# 2) صندوق النشر: Google Sheet عبر Apps Script
# ═══════════════════════════════════════════════════════════════

class SheetQueue:
    """يكتب صفاً لكل فيديو في الـ Sheet. Apps Script يرسله إلى Make عند الموعد."""

    def __init__(self) -> None:
        a = C.apps_script_settings()
        self.url, self.secret = a["url"], a["secret"]

    def configured(self) -> bool:
        return bool(self.url and self.secret)

    def _call(self, payload: dict) -> dict:
        body = json.dumps({"secret": self.secret, **payload}, ensure_ascii=False)
        last = ""
        for attempt in range(3):
            try:
                r = requests.post(self.url, data=body.encode("utf-8"), timeout=60,
                                  headers={"Content-Type": "text/plain; charset=utf-8"})
                data = r.json()
                if data.get("ok"):
                    return data
                last = str(data.get("error", "unknown"))
            except Exception as e:
                last = str(e)[:150]
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Apps Script call failed: {last}")

    def enqueue(self, item: dict) -> None:
        self._call({"action": "enqueue", "item": item})

    def exists(self, sid: str) -> bool:
        return bool(self._call({"action": "exists", "id": sid}).get("exists"))


_queue: Optional[SheetQueue] = None


def queue() -> SheetQueue:
    global _queue
    if _queue is None:
        _queue = SheetQueue()
    return _queue


# ═══════════════════════════════════════════════════════════════
# 2b) حالة الإنتاج: قاعدة لكل لغة في Release "state"
# ═══════════════════════════════════════════════════════════════

class _State:
    def __init__(self, lang: str, local: Path, name: str, remote: bool, write: bool):
        self.lang, self.local, self.name = lang, local, name
        self.remote, self.write = remote, write

    def save(self) -> None:
        """يدمج WAL ويرفع القاعدة (بعد كل فيديو لتفادي ضياع الحالة)."""
        if not (self.remote and self.write):
            return
        db.checkpoint_and_close()
        store().upload_asset(self.local, self.name, "application/octet-stream",
                             C.STATE_TAG, replace=True)
        db.use_database(self.local)
        db.init_db()


@contextmanager
def lang_state(lang: str, remote: bool = True, write: bool = True) -> Iterator[_State]:
    """ينزّل vsg_{lang}.db من GitHub، يفتحها، وعند الخروج يرفعها (إن write)."""
    use_remote = remote and store().configured()
    local = C.BASE_DIR / f"vsg_{lang}.db"
    name = f"vsg_{lang}.db"
    if use_remote:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{local}{suffix}").unlink(missing_ok=True)
        if store().download_asset(C.STATE_TAG, name, local):
            log.info("  📥 state %s restored", lang.upper())
        else:
            log.info("  🆕 state %s: new database", lang.upper())
    db.use_database(local)
    db.init_db()
    st = _State(lang, local, name, use_remote, write)
    try:
        yield st
    finally:
        db.checkpoint_and_close()
        if use_remote and write:
            try:
                store().upload_asset(local, name, "application/octet-stream",
                                     C.STATE_TAG, replace=True)
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
    """حماية من التكرار: هل يوجد صف لنفس الموعد في الـ Sheet؟"""
    return queue().configured() and queue().exists(slot_key(lang, mode, when))


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


def _asset_name(lang: str, mode: str, num: str, kind: str, ext: str) -> str:
    stamp = now_utc().strftime("%Y%m%dT%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(num))
    return f"{lang}_{mode}_{safe}_{stamp}_{kind}.{ext}"


def upload_video(built: dict, lang: str, mode: str, num: str) -> dict:
    """Short: ملف واحد للمنصتين. Long: يوتيوب أفقي + فيسبوك عمودي + صورة مصغرة."""
    st = store()
    yt_name = _asset_name(lang, mode, num, "yt", "mp4")
    yt_url = st.upload_asset(built["final"], yt_name, "video/mp4", C.MEDIA_TAG)
    if mode == "short":
        fb_url = yt_url
    else:
        fb_url = st.upload_asset(built["fb"], _asset_name(lang, mode, num, "fb", "mp4"),
                                 "video/mp4", C.MEDIA_TAG)
    thumb_url = ""
    if built.get("thumb"):
        thumb_url = st.upload_asset(built["thumb"], _asset_name(lang, mode, num, "thumb", "jpg"),
                                    "image/jpeg", C.MEDIA_TAG)
    return {"yt_url": yt_url, "fb_url": fb_url, "thumb_url": thumb_url}


# ═══════════════════════════════════════════════════════════════
# 7) طلب Make: طلب واحد لكل فيديو يحمل المنصتين (سيناريو واحد في Make)
# ═══════════════════════════════════════════════════════════════

def build_queue_item(sid: str, lang: str, mode: str, num: str, title: str,
                     publish_at: datetime, payload: dict, up: dict) -> dict:
    """
    الصف الذي يُكتب في الـ Sheet. حقل payload هو ما يُرسَل حرفياً إلى Webhook في Make
    (يضيف Apps Script الحقل auth من أسراره).
    """
    tags = payload["youtube_tags"]
    make_payload = {
        "idempotency_key": sid,
        "lang": lang,
        "content_mode": mode,
        "is_short": mode == "short",
        "video_number": num,
        "publish_at": iso(publish_at),
        "title": payload["title"],
        "video_url_youtube": up["yt_url"],
        "video_url_facebook": up["fb_url"],
        "description_youtube": payload["youtube_description"],
        "caption_facebook": payload["facebook_caption"],
        "tags": tags,
        "tags_csv": ",".join(tags),
        "hashtags_text": " ".join(payload["hashtags"][:15]),
        "thumbnail_url": up.get("thumb_url", ""),
    }
    return {"id": sid, "lang": lang, "content_mode": mode, "video_number": num,
            "title": title, "publish_at": iso(publish_at), "created_at": iso(now_utc()),
            "payload": make_payload}


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
        log.warning("  ⏭️  %s already in the Sheet queue — skipping duplicate", sid)
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
        queue().enqueue(item)                                       # ← يظهر لـ Apps Script الآن
        db.mark_ready(vid, built["duration"], yt_url=up["yt_url"], fb_url=up["fb_url"],
                      thumb_url=up["thumb_url"])
        state.save()                                                # احفظ الحالة فوراً
        log.info("  ✅ #%s queued — Apps Script will send to Make at %s", num, iso(when))
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
    """إنتاج يدوي لفيديو واحد. --no-upload يبقي الملفات محلياً بلا رفع."""
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
    line("GitHub token/repo", store().configured(), "GITHUB_TOKEN + GITHUB_REPOSITORY")
    line("Apps Script", queue().configured(), "APPS_SCRIPT_URL + APPS_SCRIPT_SECRET")
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
    print("\n  (رابط Make يُضبط في Script Properties داخل Google Apps Script وليس هنا)\n")
    return 0 if ok else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    """يحذف من Release "media" كل أصل مضى عليه 48 ساعة."""
    if not store().configured():
        log.warning("  ⚠️  GitHub not configured — nothing to clean")
        return 0
    if args.dry_run:
        old = [a["name"] for a in store().assets(C.MEDIA_TAG)]
        log.info("  🔍 [dry-run] %d asset(s) currently in release", len(old))
        return 0
    n = store().delete_older_than(C.MEDIA_TAG, C.MEDIA_RETENTION_HOURS)
    log.info("  🗑️  deleted %d asset(s) older than %dh", n, C.MEDIA_RETENTION_HOURS)
    return 0


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

    sp = sub.add_parser("cleanup", help="حذف أصول الفيديو الأقدم من 48 ساعة")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_cleanup)

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
