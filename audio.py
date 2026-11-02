"""
audio.py — الصوت: TTS + المحاذاة + المزج (v1.0)

يدمج: tts + sync + audio_manager  (Whisper عبر faster-whisper بلا PyTorch)

الخط الكامل (produce_audio):
    1) Gemini TTS (مقاطع ≤ 300 كلمة) → voice.wav
    2) قص الصمت الأولي + تسريع (Short فقط)
    3) محاذاة: faster-whisper ثم مطابقة كلمات Whisper مع كلمات السكريبت
       → aligned: عنصر واحد بالضبط لكل unit (لا انزياح تاجات)
    4) مزج: EQ + compressor + موسيقى بـ sidechain ducking حقيقي
       + مؤثرات في أوقاتها (hook / whoosh / impact / riser) + limiter
       → final.m4a  (حاوية MP4: مدتها دقيقة في ffprobe)

المخرجات: (m4a_path, duration, aligned)
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
import wave
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import config as C

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
TTS_RATE_DEFAULT = 24000
MAX_WORDS_PER_TTS = 300
MIN_WORD_DUR = 0.08


# ═══════════════════════════════════════════════════════════════
# أدوات ffmpeg / ffprobe
# ═══════════════════════════════════════════════════════════════

def run_ffmpeg(args: list[str], timeout: int = C.FFMPEG_TIMEOUT) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log.debug("ffmpeg stderr: %s", (r.stderr or "")[-600:])
        return r.returncode == 0, r.stderr or ""
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "ffmpeg not found"
    except Exception as e:
        return False, str(e)


def get_audio_duration(path: str | Path) -> float:
    """مدة ملف صوتي بالثواني (0.0 عند الفشل)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=C.FFPROBE_TIMEOUT)
        return float(r.stdout.strip()) if r.stdout.strip() else 0.0
    except Exception as e:
        log.debug("ffprobe error: %s", e)
        return 0.0


def _safe_unlink(*paths: str | Path) -> None:
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 1) TTS (Gemini)
# ═══════════════════════════════════════════════════════════════

def _build_tts_prompt(units: list[dict], lang: str) -> str:
    st = C.STREET_STYLE[lang]
    used = sorted({u["final_tag"] for u in units})
    legend = "\n".join(f"[{t}]: {C.TAGS.get(t, C.TAGS[C.DEFAULT_TAG])[0]}" for t in used)
    script = "\n\n".join(f"[{u['final_tag']}] {u['text']}" for u in units)
    return f"""Read the transcript below aloud with an authentic street accent and real emotion.

# Voice profile
{st['voice']}
Speak EXACTLY as written, never "correct" it into formal language. Sound like a real
person talking to friends, not a news anchor.

# Delivery tags
Each paragraph starts with a [tag] describing HOW to say it. NEVER speak the tag itself.
Change your delivery with every tag:
{legend}

# Rules
1. Read EVERY word from start to finish. Never trail off or stop early.
2. Keep natural pauses between paragraphs.

# Transcript
{script}"""


def _extract_pcm(chunk: Any) -> list[tuple[bytes, str]]:
    out: list[tuple[bytes, str]] = []
    for cand in getattr(chunk, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                out.append((data, getattr(inline, "mime_type", None) or "audio/L16;rate=24000"))
    return out


def _rate_from_mime(mime: str) -> int:
    m = re.search(r"rate=(\d+)", mime or "")
    return int(m.group(1)) if m else TTS_RATE_DEFAULT


def _write_wav(pcm: bytes, rate: int, path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _tts_chunk(units: list[dict], lang: str, op: str) -> tuple[bytes, int]:
    """يولّد PCM لمقطع واحد مع إعادة المحاولة وتدوير المفاتيح. يرجع (pcm, rate)."""
    from google import genai
    from google.genai import types

    ring = C.get_ring("GEMINI_API_KEY")
    if not len(ring):
        raise RuntimeError("No Gemini API keys for TTS")

    prompt = _build_tts_prompt(units, lang)
    words = sum(len(u["text"].split()) for u in units)
    expected = words / C.WPM_BY_LANG.get(lang, 160) * 60
    attempts = min(8, max(3, len(ring) * 2))
    cfg = types.GenerateContentConfig(
        temperature=1.0,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=C.TTS_VOICE[lang]))),
    )

    last: Optional[Exception] = None
    best: tuple[bytes, int] = (b"", TTS_RATE_DEFAULT)
    for attempt in range(attempts):
        key = ring.current()
        try:
            log.info("  🎙️  %s [key#%d attempt %d/%d, %d words]",
                     op, ring.index + 1, attempt + 1, attempts, words)
            client = genai.Client(api_key=key)
            pcm = bytearray()
            rate = TTS_RATE_DEFAULT
            for ch in client.models.generate_content_stream(
                    model=C.GEMINI_TTS_MODEL, contents=prompt, config=cfg):
                for data, mime in _extract_pcm(ch):
                    if data[:4] == b"RIFF":
                        data = data[44:]
                    rate = _rate_from_mime(mime)
                    pcm.extend(data)
            if not pcm:
                raise RuntimeError("no audio returned")
            dur = len(pcm) / 2 / rate
            if len(best[0]) < len(pcm):
                best = (bytes(pcm), rate)
            if expected > 6 and dur < expected * 0.5:
                raise RuntimeError(f"truncated audio ({dur:.1f}s vs ~{expected:.1f}s)")
            return bytes(pcm), rate
        except Exception as e:
            last = e
            msg = str(e).lower()
            log.warning("  ⚠️  TTS error: %s", str(e)[:110])
            ring.rotate()
            time.sleep(3 if any(h in msg for h in ("429", "quota", "exhausted")) else 1.5)

    if best[0]:
        log.warning("  ⚠️  TTS: using best (possibly short) result")
        return best
    raise RuntimeError(f"TTS failed: {last}")


def synthesize(units: list[dict], lang: str, out_wav: Path) -> float:
    """يولّد الصوت كاملاً (مقاطع ≤ 300 كلمة تُلصق) ويحفظه wav. يرجع المدة."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    cur_w = 0
    for u in units:
        w = len(u["text"].split())
        if cur and cur_w + w > MAX_WORDS_PER_TTS:
            groups.append(cur)
            cur, cur_w = [], 0
        cur.append(u)
        cur_w += w
    if cur:
        groups.append(cur)

    pcm_all = bytearray()
    rate_all = TTS_RATE_DEFAULT
    for i, g in enumerate(groups):
        pcm, rate = _tts_chunk(g, lang, f"TTS {i + 1}/{len(groups)}")
        if i == 0:
            rate_all = rate
        if i > 0:
            pcm_all.extend(b"\x00\x00" * int(rate_all * 0.2))   # 0.2s صمت بين المقاطع
        pcm_all.extend(pcm)

    _write_wav(bytes(pcm_all), rate_all, out_wav)
    dur = len(pcm_all) / 2 / rate_all
    log.info("  ✅ TTS: %.1fs (%d chunk%s)", dur, len(groups), "s" if len(groups) > 1 else "")
    return dur


def _clean_voice(src: Path, dst: Path, speed: float) -> Path:
    """قص الصمت الأولي + تسريع اختياري. يرجع dst أو src عند الفشل."""
    filters = ["silenceremove=start_periods=1:start_duration=0.1:start_threshold=-45dB"]
    if abs(speed - 1.0) > 0.01:
        filters.append(f"atempo={speed:.3f}")
    ok, _ = run_ffmpeg(["ffmpeg", "-y", "-i", str(src), "-af", ",".join(filters),
                        "-c:a", "pcm_s16le", str(dst)], timeout=120)
    if ok and get_audio_duration(dst) >= C.MIN_VALID_AUDIO_S * 0.5:
        return dst
    log.warning("  ⚠️  voice cleanup failed — using raw voice")
    return src


# ═══════════════════════════════════════════════════════════════
# 2) المحاذاة: كلمات Whisper ↔ كلمات السكريبت
# ═══════════════════════════════════════════════════════════════

_MODEL: Any = None
_MODEL_LOCK = threading.Lock()

_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ـ": ""})


def _norm_token(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower()).translate(_AR_MAP)
    t = "".join(ch for ch in t if not unicodedata.combining(ch) and ch.isalnum())
    return t


def _load_whisper() -> Any:
    """faster-whisper مباشرة (CTranslate2) — بلا PyTorch."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from faster_whisper import WhisperModel
            cache = Path.home() / ".cache" / "faster-whisper"
            cache.mkdir(parents=True, exist_ok=True)
            log.info("  📥 Loading Whisper '%s'...", C.WHISPER_MODEL)
            t0 = time.time()
            _MODEL = WhisperModel(C.WHISPER_MODEL, device=C.WHISPER_DEVICE,
                                  compute_type=C.WHISPER_COMPUTE,
                                  download_root=str(cache))
            log.info("  ✅ Whisper loaded in %.1fs", time.time() - t0)
        except Exception as e:
            log.error("  ❌ Whisper unavailable: %s", e)
            _MODEL = None
    return _MODEL


def _asr_words(audio: Path, lang: str, prompt_hint: str) -> list[tuple[str, float, float]]:
    """[(norm, start, end)] من faster-whisper. قائمة فارغة عند الفشل."""
    model = _load_whisper()
    if model is None:
        return []
    wav = audio.with_suffix(".asr16k.wav")
    ok, _ = run_ffmpeg(["ffmpeg", "-y", "-i", str(audio), "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(wav)], timeout=120)
    src = str(wav if ok else audio)
    try:
        t0 = time.time()
        segments, _info = model.transcribe(
            src, language=lang, word_timestamps=True, beam_size=1,
            vad_filter=False, condition_on_previous_text=False,
            initial_prompt=prompt_hint[:200] or None)
        words: list[tuple[str, float, float]] = []
        for seg in segments:                       # مولّد: الترجمة تجري هنا
            for w in seg.words or []:
                if w.start is None or w.end is None:
                    continue
                for tok in str(w.word).split():
                    n = _norm_token(tok)
                    if n:
                        words.append((n, float(w.start), max(float(w.end), float(w.start) + 0.01)))
        log.info("  ✅ Whisper: %d words in %.1fs", len(words), time.time() - t0)
        return words
    except Exception as e:
        log.error("  ❌ Whisper failed: %s", e)
        return []
    finally:
        _safe_unlink(wav)


def _interpolate(times: list[Optional[tuple[float, float]]], total: float) -> None:
    """يملأ الكلمات غير المطابقة بالتوزيع الخطي بين أقرب جارين مطابقين."""
    n = len(times)
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        t0 = times[i - 1][1] if i > 0 and times[i - 1] else 0.0
        t1 = times[j][0] if j < n and times[j] else total
        t1 = max(t1, t0 + (j - i) * MIN_WORD_DUR)
        step = (t1 - t0) / (j - i)
        for k in range(i, j):
            times[k] = (t0 + (k - i) * step, t0 + (k - i + 1) * step)
        i = j


def align_units(audio: Path, units: list[dict], lang: str,
                cache_path: Optional[Path] = None) -> list[dict]:
    """
    يرجع aligned بطول units بالضبط:
      [{"sentence","start","end","tag","words":[{"word","start","end"}]}]
    النص من السكريبت (إملاء صحيح)، والتوقيت من Whisper.
    """
    total = get_audio_duration(audio)
    sig = hashlib.sha256(
        ("|".join(u["text"] for u in units) + f"|{total:.2f}|{C.WHISPER_MODEL}").encode()
    ).hexdigest()[:20]

    if cache_path and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("sig") == sig:
                log.info("  ♻️  alignment cache hit")
                return data["aligned"]
        except Exception:
            pass

    # كلمات السكريبت
    script: list[tuple[str, str, int]] = []      # (original, norm, unit_idx)
    for ui, u in enumerate(units):
        for tok in u["text"].split():
            n = _norm_token(tok)
            script.append((tok, n, ui))

    asr = _asr_words(audio, lang, units[0]["text"] if units else "")
    times: list[Optional[tuple[float, float]]] = [None] * len(script)
    matched = 0

    if asr and script:
        a_norm = [w[0] for w in asr]
        s_norm = [s[1] for s in script]
        sm = SequenceMatcher(None, s_norm, a_norm, autojunk=False)
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                if s_norm[blk.a + k]:
                    _, st, en = asr[blk.b + k]
                    times[blk.a + k] = (st, en)
                    matched += 1
        ratio = matched / max(1, len(script))
        log.info("  🔗 matched %d/%d script words (%.0f%%)", matched, len(script), ratio * 100)
        if ratio < 0.3:
            log.warning("  ⚠️  low match — falling back to proportional timing")
            times = [None] * len(script)

    if not any(t is not None for t in times):
        # fallback: توزيع بحسب عدد الأحرف
        weights = [max(1, len(s[0])) for s in script]
        acc, tw = 0.0, float(sum(weights)) or 1.0
        for i, w in enumerate(weights):
            times[i] = (acc / tw * total, (acc + w) / tw * total)
            acc += w
    else:
        _interpolate(times, total)

    # ترتيب وتنظيف: تصاعدي، حد أدنى للمدة، بلا تداخل
    prev_end = 0.0
    fixed: list[tuple[float, float]] = []
    for t in times:
        s, e = t  # type: ignore[misc]
        s = max(s, prev_end)
        e = max(e, s + MIN_WORD_DUR)
        fixed.append((s, e))
        prev_end = e

    aligned: list[dict] = []
    for ui, u in enumerate(units):
        idxs = [i for i, s in enumerate(script) if s[2] == ui]
        if not idxs:
            continue
        words = [{"word": script[i][0], "start": round(fixed[i][0], 3),
                  "end": round(fixed[i][1], 3)} for i in idxs]
        aligned.append({
            "sentence": u["text"],
            "tag": u["final_tag"],
            "start": words[0]["start"],
            "end": words[-1]["end"],
            "words": words,
        })

    # حدود الوحدات متصلة: نهاية كل وحدة = بداية التالية (يمنع فجوات الكليبات)
    for i in range(len(aligned) - 1):
        aligned[i]["end"] = aligned[i + 1]["start"]
    if aligned:
        aligned[-1]["end"] = max(aligned[-1]["end"], min(total, aligned[-1]["end"] + 0.3))

    if cache_path:
        try:
            cache_path.write_text(json.dumps({"sig": sig, "aligned": aligned},
                                             ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return aligned


# ═══════════════════════════════════════════════════════════════
# 3) المؤثرات الصوتية (SFX)
# ═══════════════════════════════════════════════════════════════

_AUDIO_EXT = (".wav", ".mp3", ".m4a", ".ogg", ".flac")

# حجم المؤثر بحسب التاغ (يُضرب في أساس الفئة)
TAG_SFX_VOL: dict[str, float] = {
    "climax": 1.0, "shock": 0.95, "hook": 0.9, "revelation": 0.85,
    "dramatic": 0.8, "tension": 0.7,
}
SFX_BASE = {"hook": 0.6, "whoosh": 0.32, "impact": 0.62, "riser": 0.45}
SFX_MAX = {"short": 30, "long": 60}
MIN_WHOOSH_GAP = {"short": 0.9, "long": 3.0}
MIN_ANY_GAP = 0.35

EQ_BY_LANG: dict[str, str] = {
    "ar": "equalizer=f=90:t=o:w=2:g=3,equalizer=f=220:t=o:w=2:g=2,"
          "equalizer=f=3000:t=o:w=2:g=-1,equalizer=f=8000:t=o:w=2:g=-2",
    "fr": "equalizer=f=90:t=o:w=2:g=1,equalizer=f=1000:t=o:w=2:g=2,"
          "equalizer=f=2500:t=o:w=2:g=3,equalizer=f=8000:t=o:w=2:g=1",
    "en": "equalizer=f=90:t=o:w=2:g=1,equalizer=f=500:t=o:w=2:g=-1,"
          "equalizer=f=4000:t=o:w=2:g=3,equalizer=f=10000:t=o:w=2:g=2",
    "es": "equalizer=f=90:t=o:w=2:g=2,equalizer=f=800:t=o:w=2:g=1,"
          "equalizer=f=3500:t=o:w=2:g=3,equalizer=f=9000:t=o:w=2:g=1",
}
COMPRESSOR = "acompressor=threshold=0.125:ratio=3.5:attack=5:release=80:makeup=2"
MUSIC_VOLUME = 0.22
DUCK = "sidechaincompress=threshold=0.02:ratio=10:attack=15:release=400"


def _files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.suffix.lower() in _AUDIO_EXT and p.is_file())


def _pick_music(seed: int) -> Optional[Path]:
    pool = [p for p in C.MUSIC_DIR.rglob("*")
            if p.suffix.lower() in _AUDIO_EXT and p.is_file()] if C.MUSIC_DIR.is_dir() else []
    if not pool:
        return None
    pool.sort()
    return random.Random(seed).choice(pool)


def plan_sfx(timeline: list[dict], mode: str, total: float, seed: int) -> list[dict]:
    """
    يبني قائمة مؤثرات متزامنة مع الحدود:
      - hook   : عند 0 (Short)
      - whoosh : قبل الحد بـ 0.12s (يتوافق ذروته مع القطع)
      - impact : عند بداية وحدة بتاغ قوي (shock/climax/...)
      - riser  : ينتهي عند بداية وحدة climax/revelation/tension
    """
    rng = random.Random(seed)
    pools = {k: _files(C.SFX_DIR / k) for k in ("hook", "whoosh", "impact", "riser")}
    last_pick: dict[str, Path] = {}

    def pick(kind: str) -> Optional[Path]:
        pool = pools[kind]
        if not pool:
            return None
        choices = [p for p in pool if p != last_pick.get(kind)] or pool
        p = rng.choice(choices)
        last_pick[kind] = p
        return p

    plan: list[dict] = []

    def add(kind: str, f: Optional[Path], t: float, vol: float, cut: float = 0.0) -> None:
        if f is None or t >= total - 0.2:
            return
        plan.append({"kind": kind, "file": f, "time": max(0.0, t),
                     "volume": round(vol, 3), "cut": cut})

    if mode == "short" and timeline:
        add("hook", pick("hook"), 0.0, SFX_BASE["hook"])

    last_whoosh = -99.0
    last_any = 0.0
    for i, u in enumerate(timeline):
        if i == 0:
            continue
        t = float(u["start"])
        tag = u.get("tag", C.DEFAULT_TAG)
        tv = TAG_SFX_VOL.get(tag, 0.6)

        if tag in C.RISER_TAGS:
            f = pick("riser")
            if f is not None:
                dur = get_audio_duration(f)
                if dur > 0 and t > 0.8:
                    start = t - dur
                    cut = max(0.0, -start)            # اقتطع بداية الـ riser إن لم يتسع
                    add("riser", f, max(0.0, start), SFX_BASE["riser"] * tv, cut)
        if tag in C.IMPACT_TAGS:
            if t - last_any >= MIN_ANY_GAP:
                add("impact", pick("impact"), t, SFX_BASE["impact"] * tv)
                last_any = t
        elif t - last_whoosh >= MIN_WHOOSH_GAP.get(mode, 1.0) and t - last_any >= MIN_ANY_GAP:
            add("whoosh", pick("whoosh"), t - 0.12, SFX_BASE["whoosh"])
            last_whoosh = t
            last_any = t

    cap = SFX_MAX.get(mode, 30)
    if len(plan) > cap:
        plan = sorted(plan, key=lambda p: p["time"])[:cap]
    return sorted(plan, key=lambda p: p["time"])


# ═══════════════════════════════════════════════════════════════
# 4) المزج
# ═══════════════════════════════════════════════════════════════

_FMT = f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"


def _prerender_music(music: Path, dur: float, out: Path) -> bool:
    """يكرّر الموسيقى حتى المدة المطلوبة في تمريرة منفصلة (تفادي خلل stream_loop+amix)."""
    ok, _ = run_ffmpeg(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(music),
                        "-t", f"{dur + 1:.3f}", "-ar", str(SAMPLE_RATE), "-ac", "2",
                        "-c:a", "pcm_s16le", str(out)], timeout=C.FFMPEG_TIMEOUT)
    return ok and out.exists() and get_audio_duration(out) >= dur - 0.5


def build_mix_graph(dur: float, lang: str, has_music: bool, sfx: list[dict]) -> str:
    """filter_complex: input0=voice, input1=music (إن وُجدت)، بعدها SFX بالترتيب."""
    parts = [
        f"[0:a]{_FMT},{EQ_BY_LANG.get(lang, EQ_BY_LANG['en'])},{COMPRESSOR}[vo]",
    ]
    mix_labels = ["[vo]"]
    if has_music:
        parts.append("[vo]asplit=2[vmix][vsc]")
        mix_labels = ["[vmix]"]
        fade_out_st = max(0.0, dur - 2.0)
        parts.append(
            f"[1:a]{_FMT},volume={MUSIC_VOLUME},afade=t=in:st=0:d=1.0,"
            f"afade=t=out:st={fade_out_st:.3f}:d=2.0,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[mu]")
        parts.append(f"[mu][vsc]{DUCK}[md]")
        mix_labels.append("[md]")

    base = 2 if has_music else 1
    for k, s in enumerate(sfx):
        idx = base + k
        trim = (f"atrim=start={s['cut']:.3f},asetpts=PTS-STARTPTS,"
                if s.get("cut", 0) > 0 else "")
        ms = int(round(s["time"] * 1000))
        parts.append(f"[{idx}:a]{_FMT},{trim}volume={s['volume']},adelay={ms}:all=1[s{k}]")
        mix_labels.append(f"[s{k}]")

    n = len(mix_labels)
    if n == 1:
        parts.append(f"{mix_labels[0]}alimiter=limit=0.95:attack=5:release=50[out]")
    else:
        parts.append(f"{''.join(mix_labels)}amix=inputs={n}:duration=first:"
                     f"normalize=0:dropout_transition=0[mx]")
        parts.append("[mx]alimiter=limit=0.95:attack=5:release=50[out]")
    return ";\n".join(parts)


def mix_audio(voice: Path, out_m4a: Path, lang: str, mode: str,
              timeline: list[dict], seed: int) -> Path:
    """يمزج الصوت. عند أي فشل يعود لصوت المعلّق فقط (محوّل إلى m4a)."""
    dur = get_audio_duration(voice)
    music = _pick_music(seed)
    sfx = plan_sfx(timeline, mode, dur, seed)
    tmp_music = out_m4a.with_suffix(".music.wav")
    script_file: Optional[str] = None

    try:
        has_music = False
        if music is not None:
            has_music = _prerender_music(music, dur, tmp_music)
            if not has_music:
                log.warning("  ⚠️  music prerender failed — no music")
        else:
            log.warning("  ⚠️  no music files in %s", C.MUSIC_DIR)

        graph = build_mix_graph(dur, lang, has_music, sfx)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(graph)
            script_file = f.name

        cmd = ["ffmpeg", "-y", "-i", str(voice)]
        if has_music:
            cmd += ["-i", str(tmp_music)]
        for s in sfx:
            cmd += ["-i", str(s["file"])]
        cmd += ["-/filter_complex", script_file, "-map", "[out]", "-t", f"{dur:.3f}",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_m4a)]

        ok, err = run_ffmpeg(cmd)
        if not ok:
            # ffmpeg < 7 يستعمل -filter_complex_script
            cmd[cmd.index("-/filter_complex")] = "-filter_complex_script"
            ok, err = run_ffmpeg(cmd)

        got = get_audio_duration(out_m4a) if ok else 0.0
        if ok and got >= dur - 0.5:
            log.info("  ✅ Mixed: %.1fs | music=%s | %d SFX", got,
                     music.name if has_music and music else "none", len(sfx))
            return out_m4a
        log.warning("  ⚠️  mix failed (%s) — voice only", err[-200:])
    finally:
        _safe_unlink(tmp_music)
        if script_file:
            _safe_unlink(script_file)

    ok, _ = run_ffmpeg(["ffmpeg", "-y", "-i", str(voice), "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", str(out_m4a)])
    if not ok:
        raise RuntimeError("audio mix and fallback both failed")
    return out_m4a


# ═══════════════════════════════════════════════════════════════
# 5) الواجهة الرئيسية
# ═══════════════════════════════════════════════════════════════

def produce_audio(units: list[dict], lang: str, mode: str, out_base: str,
                  seed: int = 0, force: bool = False) -> tuple[Path, float, list[dict]]:
    """
    out_base: مثل output/video_12_ar_short  (يُنتج {out_base}_audio.m4a وملفات مساعدة)
    يرجع (m4a, duration, aligned)
    """
    C.validate_lang(lang)
    C.validate_mode(mode)
    base = Path(out_base)
    base.parent.mkdir(parents=True, exist_ok=True)

    voice_raw = Path(f"{base}_voice_raw.wav")
    voice = Path(f"{base}_voice.wav")
    final = Path(f"{base}_audio.m4a")
    align_cache = Path(f"{base}_align.json")

    if not force and final.exists() and voice.exists() and align_cache.exists():
        dur = get_audio_duration(final)
        if dur >= C.MIN_VALID_AUDIO_S:
            aligned = align_units(voice, units, lang, align_cache)
            if len(aligned) == len(units):
                log.info("  ♻️  reusing audio %s (%.1fs)", final.name, dur)
                return final, dur, aligned

    if force:
        _safe_unlink(voice_raw, voice, final, align_cache)

    log.info("\n  %s\n  🎙️  AUDIO %s/%s", "─" * 55, lang.upper(), mode.upper())
    if not voice.exists():
        if not voice_raw.exists():
            synthesize(units, lang, voice_raw)
        speed = C.SPEED_MULTIPLIER.get(lang, 1.0) if mode == "short" else 1.0
        cleaned = _clean_voice(voice_raw, voice, speed)
        if cleaned != voice:
            voice_raw.replace(voice)
    voice_dur = get_audio_duration(voice)
    if voice_dur < C.MIN_VALID_AUDIO_S:
        raise RuntimeError(f"voice too short: {voice_dur:.1f}s")

    aligned = align_units(voice, units, lang, align_cache)
    timeline = [{"start": a["start"], "end": a["end"], "tag": a["tag"]} for a in aligned]
    mix_audio(voice, final, lang, mode, timeline, seed)

    _safe_unlink(voice_raw)
    dur = get_audio_duration(final)
    log.info("  ✅ Audio ready: %.2fs", dur)
    return final, dur, aligned
