"""
media.py — الوسائط (v1.0)

يدمج: video_sources + thumb_gen + thumbnail

الواجهة العامة:
    fetch_videos(clip_keywords, clip_tags, mode, out_dir)  -> list[Path]
    make_long_thumbnail(record, lang, out_base, keyword, video_paths) -> Optional[Path]
    make_fb_long(video_16x9, lang, out_path)               -> Path
    probe_video(path)                                      -> dict

ملاحظات:
  - Short = فيديوهات عمودية، Long = أفقية 16:9 فقط.
  - لا صور مصغرة للـ Short. الـ Long: رابط من الجدول أولاً، وإلا توليد تلقائي.
  - نسخة فيسبوك للـ Long: نفس الفيديو في منتصف صورة خلفية 9:16 (واحدة لكل لغة).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import requests

import config as C
import db
from audio import run_ffmpeg

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ثوابت
# ═══════════════════════════════════════════════════════════════

PEXELS_VIDEOS = "https://api.pexels.com/videos/search"
PEXELS_PHOTOS = "https://api.pexels.com/v1/search"
PIXABAY_VIDEOS = "https://pixabay.com/api/videos/"

API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 90
MIN_DURATION = 4
MIN_FPS = 10
MIN_FILE_BYTES = 300_000
PEXELS_PER_PAGE = 30
PIXABAY_PER_PAGE = 30
MAX_CANDIDATES = 10
RETRY_DELAYS = (1.0, 2.0, 4.0)

MIN_W_PORTRAIT, MIN_H_PORTRAIT = 720, 1280
MIN_W_LANDSCAPE = 1280

STRICT_SCORE = 0.5
LENIENT_SCORE = 0.01

YT_THUMB_MAX_BYTES = 2 * 1024 * 1024
MAX_IMG_BYTES = 15 * 1024 * 1024
THUMB_W, THUMB_H = 1280, 720

_VIDEO_CT = ("video/", "application/octet-stream")


# ═══════════════════════════════════════════════════════════════
# 1) صلة النتائج
# ═══════════════════════════════════════════════════════════════

_WORD_RE = re.compile(r"[a-z0-9]+")
_GENERIC = frozenset(
    "person people close closeup up face camera serious cinematic dramatic moody dark "
    "vertical portrait widescreen footage professional scene shot wide angle lighting "
    "slow motion atmospheric emotional expression direct talking speaking intense strong "
    "the and with for from into his her their a an of in on at man woman guy".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content_words(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tok in _WORD_RE.findall((text or "").lower()):
        if tok in _GENERIC or len(tok) <= 2:
            continue
        s = _stem(tok)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def relevance(keyword: str, meta: str) -> float:
    """نسبة كلمات المحتوى في keyword الموجودة في وصف النتيجة. كلها عامة → 1.0."""
    cw = _content_words(keyword)
    if not cw:
        return 1.0
    have = {_stem(t) for t in _WORD_RE.findall((meta or "").lower())}
    return sum(1 for w in cw if w in have) / len(cw)


def _slug(url: str) -> str:
    if not url:
        return ""
    return url.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ")


# ═══════════════════════════════════════════════════════════════
# 2) كاش بحث (LRU آمن للخيوط)
# ═══════════════════════════════════════════════════════════════

_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 300


def _cache_get(key: tuple) -> Optional[list]:
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    return None


def _cache_set(key: tuple, val: list) -> None:
    if not val:
        return
    with _CACHE_LOCK:
        _CACHE[key] = val
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


# ═══════════════════════════════════════════════════════════════
# 3) فحص الفيديو
# ═══════════════════════════════════════════════════════════════

def probe_video(path: str | Path) -> dict:
    """{valid, duration, fps, width, height}"""
    info = {"valid": False, "duration": 0.0, "fps": 0.0, "width": 0, "height": 0}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=C.FFPROBE_TIMEOUT)
        if r.returncode != 0:
            return info
        data = json.loads(r.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        info["width"] = int(st.get("width") or 0)
        info["height"] = int(st.get("height") or 0)
        try:
            num, den = str(st.get("avg_frame_rate", "0/1")).split("/")
            info["fps"] = int(num) / int(den) if int(den) else 0.0
        except (ValueError, ZeroDivisionError):
            pass
        info["duration"] = float((data.get("format") or {}).get("duration") or 0)
        info["valid"] = (info["duration"] >= MIN_DURATION
                         and (info["fps"] == 0 or info["fps"] >= MIN_FPS)
                         and info["width"] > 0)
    except Exception as e:
        log.debug("probe error: %s", e)
    return info


def _safe_name(s: str, n: int = 20) -> str:
    c = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", s.lower())).strip("_")
    return c[:n] or "video"


def _download(url: str, dest: Path, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                              allow_redirects=True) as r:
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "").lower()
                if not any(ct.startswith(p) for p in _VIDEO_CT):
                    return False
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk:
                            f.write(chunk)
            if not dest.exists() or dest.stat().st_size < MIN_FILE_BYTES:
                dest.unlink(missing_ok=True)
                return False
            info = probe_video(dest)
            if not info["valid"]:
                dest.unlink(missing_ok=True)
                return False
            return True
        except Exception as e:
            dest.unlink(missing_ok=True)
            if attempt < retries - 1:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            else:
                log.debug("download failed: %s", str(e)[:80])
    return False


# ═══════════════════════════════════════════════════════════════
# 4) مصادر البحث
# ═══════════════════════════════════════════════════════════════

def _api_get(url: str, ring_prefix: str, *, headers: Optional[dict] = None,
             params: Optional[dict] = None, key_param: str = "",
             retries: int = 3) -> Optional[dict]:
    ring = C.get_ring(ring_prefix)
    if not len(ring):
        return None
    for attempt in range(retries):
        key = ring.current()
        h = dict(headers or {})
        p = dict(params or {})
        if key_param:
            p[key_param] = key
        else:
            h["Authorization"] = key
        try:
            r = requests.get(url, headers=h, params=p, timeout=API_TIMEOUT)
            if r.status_code in (429, 401, 403) or (r.status_code == 400 and key_param):
                log.warning("  ⚠️  %s HTTP %d — rotating key", ring_prefix, r.status_code)
                ring.rotate()
                time.sleep(2 if r.status_code == 429 else 0.5)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            else:
                log.debug("%s search error: %s", ring_prefix, str(e)[:80])
    return None


def _orientation(mode: str) -> str:
    return "portrait" if mode == "short" else "landscape"


def _pexels_search(query: str, mode: str, page: int = 1) -> list[dict]:
    key = ("px", query.lower(), _orientation(mode), page)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = _api_get(PEXELS_VIDEOS, "PEXELS_API_KEY", params={
        "query": query, "per_page": PEXELS_PER_PAGE, "page": page,
        "orientation": _orientation(mode), "size": "large"})
    vids = (data or {}).get("videos", [])
    _cache_set(key, vids)
    return vids


def _pixabay_search(query: str, page: int = 1) -> list[dict]:
    key = ("pb", query.lower(), page)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = _api_get(PIXABAY_VIDEOS, "PIXABAY_API_KEY", key_param="key", params={
        "q": query[:100], "video_type": "film", "per_page": PIXABAY_PER_PAGE,
        "page": page, "safesearch": "true", "order": "popular"})
    hits = (data or {}).get("hits", [])
    _cache_set(key, hits)
    return hits


def _pexels_file_url(files: list[dict], mode: str) -> str:
    mp4 = [f for f in files if f.get("file_type") == "video/mp4"]
    if not mp4:
        return ""
    port = mode == "short"

    def ok(f: dict, hd: bool) -> bool:
        w, h = f.get("width", 0), f.get("height", 0)
        if port:
            return h > w and (not hd or (w >= MIN_W_PORTRAIT and h >= MIN_H_PORTRAIT))
        return w > h and (not hd or w >= MIN_W_LANDSCAPE)

    pool = [f for f in mp4 if ok(f, True)] or [f for f in mp4 if ok(f, False)] or mp4
    pool.sort(key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
    # أفضل جودة لكن ليست 4K (حجم ثقيل بلا فائدة)
    for f in pool:
        if f.get("width", 0) * f.get("height", 0) <= 2560 * 1440:
            return f.get("link", "")
    return pool[-1].get("link", "")


def _pixabay_url(hit: dict, mode: str) -> str:
    port = mode == "short"
    vids = hit.get("videos", {})
    for q in ("large", "medium", "small"):
        v = vids.get(q) or {}
        url, w, h = v.get("url", ""), v.get("width", 0), v.get("height", 0)
        if not url or ".mp4" not in url.lower():
            continue
        if w and h and ((h > w) == port):
            return url
    return ""   # اتجاه خاطئ — لا نقبله


# ═══════════════════════════════════════════════════════════════
# 5) جلب فيديو واحد
# ═══════════════════════════════════════════════════════════════

class _Session:
    def __init__(self) -> None:
        self.used: set[str] = set()
        self.lock = threading.Lock()

    def claim(self, key: str) -> bool:
        with self.lock:
            if key in self.used:
                return False
            self.used.add(key)
            return True

    def release(self, key: str) -> None:
        with self.lock:
            self.used.discard(key)


def _local_videos() -> list[Path]:
    if not C.LOCAL_VIDEO_DIR.is_dir():
        return []
    return sorted(list(C.LOCAL_VIDEO_DIR.glob("*.mp4")) + list(C.LOCAL_VIDEO_DIR.glob("*.mov")))


def _try_local(kw: str, min_score: float, sess: _Session) -> Optional[Path]:
    if not _content_words(kw):
        return None
    scored = sorted(
        ((relevance(kw, p.stem.replace("_", " ")), p) for p in _local_videos()),
        key=lambda x: -x[0])
    for sc, p in scored:
        if sc < max(min_score, 0.5):
            break
        if sess.claim(f"local_{p}") and probe_video(p)["valid"]:
            return p
        sess.release(f"local_{p}")
    return None


def _try_source(source: str, kw: str, mode: str, min_score: float, sess: _Session,
                idx: int, sub: int, out_dir: Path, pages: tuple[int, ...]) -> Optional[Path]:
    ring = C.get_ring("PEXELS_API_KEY" if source == "pexels" else "PIXABAY_API_KEY")
    if not len(ring):
        return None
    for page in pages:
        items = _pexels_search(kw, mode, page) if source == "pexels" else _pixabay_search(kw, page)
        scored: list[tuple[float, dict]] = []
        for it in items:
            if it.get("duration", 0) < MIN_DURATION:
                continue
            meta = (_slug(it.get("url", "")) if source == "pexels"
                    else f"{str(it.get('tags', '')).replace(',', ' ')} {_slug(it.get('pageURL', ''))}")
            sc = relevance(kw, meta)
            if sc >= min_score:
                scored.append((sc, it))
        scored.sort(key=lambda x: -x[0])          # مستقر: الترتيب الأصلي عند التعادل

        for sc, it in scored[:MAX_CANDIDATES]:
            vid = str(it["id"])
            skey = f"{source}_{vid}"
            if not sess.claim(skey):
                continue
            if db.is_video_used(vid, source):
                sess.release(skey)
                continue
            url = (_pexels_file_url(it.get("video_files", []), mode) if source == "pexels"
                   else _pixabay_url(it, mode))
            if not url:
                sess.release(skey)
                continue
            dest = out_dir / f"{idx:03d}_{sub}_{source[:2]}_{vid}_{_safe_name(kw)}.mp4"
            if _download(url, dest):
                db.mark_video_used(vid, kw, source)
                return dest
            sess.release(skey)
    return None


def _fetch_one(idx: int, keywords: list[str], tag: str, mode: str,
               sess: _Session, out_dir: Path) -> Optional[Path]:
    kws = [k.strip() for k in keywords if k and k.strip()]
    tag_fb = [k for k in _tag_fallbacks(tag) if k not in kws]

    stages: list[tuple[str, list[str], float, tuple[int, ...]]] = []
    if kws:
        stages += [("strict", kws, STRICT_SCORE, (1,)),
                   ("lenient", kws, LENIENT_SCORE, (1, 2))]
    stages += [("tag", tag_fb, LENIENT_SCORE, (1,)),
               ("any", tag_fb[:1], 0.0, (1,))]      # آخر ملاذ: أي نتيجة بالاتجاه الصحيح

    sub = 0
    for name, kw_list, min_score, pages in stages:
        for kw in kw_list:
            sub += 1
            log.info("  [%d] %s: %r", idx + 1, name, kw)
            p = _try_local(kw, min_score, sess)
            if p:
                return p
            for source in ("pexels", "pixabay"):
                p = _try_source(source, kw, mode, min_score, sess, idx, sub, out_dir, pages)
                if p:
                    return p
    return None


def _tag_fallbacks(tag: str) -> list[str]:
    base = C.TAGS.get(tag, C.TAGS[C.DEFAULT_TAG])[1]
    return [base, "person serious face talking camera", "emotional person close up expression"]


def _fill_gaps(results: list[Optional[Path]]) -> list[Path]:
    """الفجوة تُملأ بأقرب مقطع مجاور (السابق عند التساوي)."""
    avail = [i for i, r in enumerate(results) if r is not None]
    if not avail:
        local = _local_videos()
        if not local:
            raise RuntimeError("Could not fetch any video — check PEXELS/PIXABAY keys")
        return [local[i % len(local)] for i in range(len(results))]
    out: list[Path] = []
    for i, r in enumerate(results):
        if r is not None:
            out.append(r)
            continue
        j = min(avail, key=lambda a: (abs(a - i), 0 if a < i else 1))
        out.append(results[j])  # type: ignore[arg-type]
        log.info("  [%d] ♻️  gap filled from neighbor [%d]", i + 1, j + 1)
    return out


def fetch_videos(clip_keywords: list[list[str]], clip_tags: list[str], mode: str,
                 out_dir: str | Path, max_workers: int = 3) -> list[Path]:
    """مقطع لكل عنصر: يرجع قائمة بطول clip_keywords."""
    C.validate_mode(mode)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = len(clip_keywords)
    if n == 0:
        return []

    sess = _Session()
    results: list[Optional[Path]] = [None] * n
    workers = max(1, min(max_workers, n))
    log.info("\n  📹 Fetching %d clips [%s/%s] workers=%d", n, mode.upper(),
             _orientation(mode), workers)

    def job(i: int) -> tuple[int, Optional[Path]]:
        try:
            tag = clip_tags[i] if i < len(clip_tags) else C.DEFAULT_TAG
            return i, _fetch_one(i, clip_keywords[i], tag, mode, sess, out)
        finally:
            db.close_thread_conn()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(job, i): i for i in range(n)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                _, p = fut.result()
            except Exception as e:
                log.error("  ❌ clip %d crashed: %s", i + 1, e)
                p = None
            results[i] = p

    found = sum(1 for r in results if r)
    final = _fill_gaps(results)
    log.info("  ✅ Clips: %d/%d fetched, %d unique", found, n, len(set(final)))
    return final


# ═══════════════════════════════════════════════════════════════
# 6) الصورة المصغرة للـ Long
# ═══════════════════════════════════════════════════════════════

def _looks_like_image(head: bytes) -> bool:
    return (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
            or head.startswith(b"GIF8") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def _normalize_url(url: str) -> str:
    url = url.strip()
    m = (re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
         or re.search(r"drive\.google\.com/open\?id=([\w-]+)", url))
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def _fetch_bytes(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, stream=True)
    r.raise_for_status()
    buf = bytearray()
    for chunk in r.iter_content(65536):
        buf.extend(chunk)
        if len(buf) > MAX_IMG_BYTES:
            raise ValueError("image too large")
    return bytes(buf)


def _og_image(html: bytes) -> str:
    text = html[:300_000].decode("utf-8", errors="ignore")
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']'):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).replace("&amp;", "&").strip()
    return ""


def _to_thumb_jpg(src: Path, dst: Path) -> bool:
    """يحوّل أي صورة إلى 1280×720 JPG ≤ 2MB."""
    for q in (3, 6, 10):
        ok, _ = run_ffmpeg(
            ["ffmpeg", "-y", "-i", str(src), "-frames:v", "1", "-vf",
             f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,"
             f"crop={THUMB_W}:{THUMB_H}", "-q:v", str(q), str(dst)], timeout=60)
        if ok and dst.exists() and 1024 < dst.stat().st_size <= YT_THUMB_MAX_BYTES:
            return True
    dst.unlink(missing_ok=True)
    return False


def _download_custom_thumb(url: str, dst: Path) -> bool:
    url = _normalize_url(url)
    if not url.lower().startswith(("http://", "https://")):
        log.warning("  ⚠️  thumbnail URL invalid: %s", url[:80])
        return False
    raw = dst.with_suffix(".dl")
    try:
        data = _fetch_bytes(url)
        if not _looks_like_image(data[:16]):          # صفحة عرض (مثل ibb.co)
            direct = _og_image(data)
            if not direct.lower().startswith(("http://", "https://")):
                log.warning("  ⚠️  thumbnail URL is not an image")
                return False
            data = _fetch_bytes(direct)
        if not _looks_like_image(data[:16]):
            return False
        raw.write_bytes(data)
        return _to_thumb_jpg(raw, dst)
    except Exception as e:
        log.warning("  ⚠️  custom thumbnail failed: %s", str(e)[:100])
        return False
    finally:
        raw.unlink(missing_ok=True)


def _fetch_pexels_photo(keyword: str, dst: Path) -> bool:
    data = _api_get(PEXELS_PHOTOS, "PEXELS_API_KEY",
                    params={"query": keyword, "per_page": 5, "orientation": "landscape"})
    for ph in (data or {}).get("photos", []):
        src = ph.get("src", {})
        url = src.get("landscape") or src.get("large2x") or src.get("large")
        if not url:
            continue
        try:
            dst.write_bytes(_fetch_bytes(url))
            return True
        except Exception:
            continue
    return False


def _extract_frame(video: Path, dst: Path) -> bool:
    ok, _ = run_ffmpeg(["ffmpeg", "-y", "-ss", "2", "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,"
                               f"crop={THUMB_W}:{THUMB_H}", "-q:v", "2", str(dst)], timeout=30)
    return ok and dst.exists() and dst.stat().st_size > 1000


_TITLE_FONT_TABLE = [(10, 96, 88), (15, 82, 74), (22, 70, 62), (30, 60, 54), (38, 52, 46), (99, 44, 40)]


def _split_title(title: str) -> str:
    words = title.strip().split()
    if len(words) <= 2:
        return "\n".join(words)
    mid = len(words) // 2
    best, diff = mid, 10 ** 9
    for i in range(max(1, mid - 2), min(len(words), mid + 3)):
        d = abs(len(" ".join(words[:i])) - len(" ".join(words[i:])))
        if d < diff:
            best, diff = i, d
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def _thumb_html(title: str, lang: str, bg: Optional[Path]) -> str:
    rtl = lang in C.RTL_LANGS
    text = _split_title(title)
    longest = max(len(x) for x in text.split("\n"))
    size = next((a if rtl else e for mx, a, e in _TITLE_FONT_TABLE if longest <= mx), 40)
    font = ("'Noto Naskh Arabic','Amiri',serif" if rtl
            else "'Noto Sans','DejaVu Sans',sans-serif")
    bg_css = (f"background:url('file://{bg.resolve()}') center/cover;"
              if bg and bg.exists() else "background:linear-gradient(135deg,#0a0a0a,#2a0a14);")
    d = "rtl" if rtl else "ltr"
    side = "left" if rtl else "right"
    align = "right" if rtl else "left"
    body = html_lib.escape(text).replace("\n", "<br>")
    return f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="UTF-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{THUMB_W}px;height:{THUMB_H}px;overflow:hidden}}
.bg{{position:absolute;inset:0;{bg_css}}}
.g1{{position:absolute;inset:0;background:linear-gradient(to {side},rgba(0,0,0,.92) 0%,rgba(0,0,0,.7) 40%,transparent 75%)}}
.g2{{position:absolute;left:0;right:0;bottom:0;height:50%;background:linear-gradient(to top,rgba(0,0,0,.85),transparent)}}
.c{{position:absolute;bottom:48px;left:0;right:0;padding:0 56px;max-width:760px;direction:{d};text-align:{align}}}
{'.c{left:auto;right:0}' if rtl else ''}
.a{{width:70px;height:5px;border-radius:3px;background:#FF1744;margin-bottom:16px}}
.t{{font-family:{font};font-size:{size}px;font-weight:900;color:#fff;line-height:1.25;
text-shadow:0 3px 20px rgba(0,0,0,.95),2px 2px 0 rgba(0,0,0,.8);
-webkit-text-stroke:1px rgba(0,0,0,.6);paint-order:stroke fill;word-break:break-word}}
</style></head><body><div class="bg"></div><div class="g1"></div><div class="g2"></div>
<div class="c"><div class="a"></div><div class="t">{body}</div></div></body></html>"""


def _render_html(html_path: Path, png: Path, lang: str) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("  ⚠️  playwright (python) not installed")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--font-render-hinting=none"])
            try:
                ctx = browser.new_context(viewport={"width": THUMB_W, "height": THUMB_H},
                                          device_scale_factor=1,
                                          locale=C.LANG_LOCALES.get(lang, "en-US"))
                page = ctx.new_page()
                page.goto(f"file://{html_path.resolve()}", wait_until="load", timeout=30000)
                page.evaluate("Promise.race([document.fonts.ready,"
                              "new Promise(r=>setTimeout(r,5000))])")
                page.wait_for_timeout(1500 if lang in C.RTL_LANGS else 700)
                page.screenshot(path=str(png), type="png",
                                clip={"x": 0, "y": 0, "width": THUMB_W, "height": THUMB_H})
            finally:
                browser.close()
        return png.exists() and png.stat().st_size > 1000
    except Exception as e:
        log.warning("  ⚠️  thumbnail render failed: %s", str(e)[:120])
        return False


def make_long_thumbnail(record: dict, lang: str, out_base: str, keyword: str = "",
                        video_paths: Optional[list] = None) -> Optional[Path]:
    """
    1) رابط thumbnail_url من الجدول  2) توليد تلقائي (خلفية + عنوان).
    يرجع مسار JPG ≤ 2MB أو None.
    """
    final = Path(f"{out_base}_thumb.jpg")
    if final.exists() and final.stat().st_size > 1024:
        return final

    url = (record.get("thumbnail_url") or "").strip()
    if url:
        if _download_custom_thumb(url, final):
            log.info("  🖼️  custom thumbnail ✅")
            return final
        log.warning("  ⚠️  custom thumbnail failed — generating automatically")

    base = Path(out_base)
    bg = Path(f"{base}_thumb_bg.jpg")
    have_bg = _fetch_pexels_photo(keyword or record["title"], bg)
    if not have_bg:
        for vp in video_paths or []:
            if vp and Path(vp).exists() and _extract_frame(Path(vp), bg):
                have_bg = True
                break

    html_path = Path(f"{base}_thumb.html")
    png = Path(f"{base}_thumb.png")
    try:
        html_path.write_text(_thumb_html(record["title"], lang, bg if have_bg else None),
                             encoding="utf-8")
        rendered = _render_html(html_path, png, lang)
        if rendered and _to_thumb_jpg(png, final):
            log.info("  🖼️  generated thumbnail ✅")
            return final
        if have_bg and _to_thumb_jpg(bg, final):         # بدون عنوان أفضل من لا شيء
            log.warning("  ⚠️  thumbnail without title text (render unavailable)")
            return final
    finally:
        for p in (html_path, png, bg):
            p.unlink(missing_ok=True)
    return None


# ═══════════════════════════════════════════════════════════════
# 7) نسخة فيسبوك للـ Long: الفيديو في منتصف صورة 9:16
# ═══════════════════════════════════════════════════════════════

def make_fb_long(video_16x9: str | Path, lang: str, out_path: str | Path) -> Path:
    """
    يضع فيديو 16:9 في منتصف لوحة 1080×1920. بدون إعادة ريندر Remotion (تمريرة ffmpeg واحدة).
    الخلفية: assets/bg_{lang}.png، وإن غابت → نسخة مموّهة من الفيديو نفسه.
    """
    C.validate_lang(lang)
    src, out = Path(video_16x9), Path(out_path)
    if not src.exists():
        raise FileNotFoundError(f"video not found: {src}")
    W, H, FW = C.DIMENSIONS["long_fb"]["width"], C.DIMENSIONS["long_fb"]["height"], C.LONG_FB_VIDEO_WIDTH
    bg = C.bg_image_path(lang)

    has_audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True).stdout.strip() != ""
    audio_args = (["-map", "0:a:0", "-c:a", "copy"] if has_audio else ["-an"])
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-r", str(C.FPS), "-movflags", "+faststart"]

    if bg.exists():
        log.info("  📘 FB long: video over %s", bg.name)
        cmd = ["ffmpeg", "-y", "-i", str(src), "-loop", "1", "-framerate", str(C.FPS), "-i", str(bg),
               "-filter_complex",
               f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[bg];"
               f"[0:v]scale={FW}:-2,setsar=1[fg];"
               f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v]",
               "-map", "[v]", *audio_args, *enc, "-shortest", str(out)]
    else:
        log.warning("  ⚠️  %s missing — using blurred video background", bg.name)
        cmd = ["ffmpeg", "-y", "-i", str(src), "-filter_complex",
               f"[0:v]split=2[a][b];"
               f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
               f"boxblur=30:5,eq=brightness=-0.15[bg];"
               f"[b]scale={FW}:-2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v]",
               "-map", "[v]", *audio_args, *enc, str(out)]

    ok, err = run_ffmpeg(cmd, timeout=3600)
    if not ok or not out.exists() or out.stat().st_size < 10_000:
        raise RuntimeError(f"FB long composite failed: {err[-300:]}")
    log.info("  ✅ FB long: %s (%.1f MB)", out.name, out.stat().st_size / 1_048_576)
    return out
