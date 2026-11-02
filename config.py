"""
config.py — الإعدادات المركزية للمشروع (v1.0)

يضم: المسارات، اللغات والمناطق الزمنية، التاجات، أسلوب الشارع لكل لغة،
إعدادات Gemini/Groq/R2/Make، قراءة المفاتيح مع التدوير، وأبعاد الفيديو.

لا يحتوي أي منطق إنتاج. كل الملفات الأخرى تستورد منه فقط.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════
# المسارات
# ═══════════════════════════════════════════════════════════════

BASE_DIR    = Path(__file__).parent.resolve()
SCRIPTS_DIR = BASE_DIR / "scripts"
ASSETS_DIR  = BASE_DIR / "assets"
MUSIC_DIR   = ASSETS_DIR / "music"
SFX_DIR     = ASSETS_DIR / "sfx"
LOCAL_VIDEO_DIR = ASSETS_DIR / "videos"
OUTPUT_DIR  = BASE_DIR / "output"
DB_PATH     = BASE_DIR / "vsg.db"
RENDER_SCRIPT = BASE_DIR / "remotion" / "render.mjs"


def script_path(lang: str, mode: str) -> Path:
    """مسار جدول المحتوى: scripts/{lang}_{mode}.xlsx"""
    validate_lang(lang)
    validate_mode(mode)
    return SCRIPTS_DIR / f"{lang}_{mode}.xlsx"


def bg_image_path(lang: str) -> Path:
    """صورة الخلفية 9:16 لنسخة فيسبوك من الفيديو الطويل (واحدة لكل لغة)."""
    validate_lang(lang)
    return ASSETS_DIR / f"bg_{lang}.png"


# ═══════════════════════════════════════════════════════════════
# اللغات والأنماط
# ═══════════════════════════════════════════════════════════════

LANGS: tuple[str, ...] = ("ar", "fr", "en", "es")
MODES: tuple[str, ...] = ("short", "long")
PLATFORMS: tuple[str, ...] = ("youtube", "facebook")

LANG_NAMES: dict[str, str] = {
    "ar": "Arabic", "fr": "French", "en": "English", "es": "Spanish",
}

LANG_TIMEZONES: dict[str, str] = {
    "ar": "Asia/Riyadh",
    "fr": "Europe/Paris",
    "en": "America/New_York",
    "es": "Europe/Madrid",
}

LANG_LOCALES: dict[str, str] = {
    "ar": "ar-SA", "fr": "fr-FR", "en": "en-US", "es": "es-ES",
}

RTL_LANGS = frozenset({"ar"})


def validate_lang(lang: str) -> None:
    if lang not in LANGS:
        raise ValueError(f"Invalid lang '{lang}'. Use one of {LANGS}")


def validate_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Invalid content_mode '{mode}'. Use one of {MODES}")


# ═══════════════════════════════════════════════════════════════
# الجدولة (الأوقات تُحدَّد لاحقاً)
# ═══════════════════════════════════════════════════════════════

SHORTS_PER_DAY  = 3     # لكل لغة (قناة + صفحة)
LONGS_PER_WEEK  = 2     # لكل لغة
# الإنتاج قبل الموعد بكم دقيقة (Short: 90 لتحمّل تأخر cron. Long: 180 لطول الريندر)
PRE_PRODUCE_LEAD_MIN: dict[str, int] = {"short": 90, "long": 180}
PRODUCE_SAFETY_MIN = 15        # هامش إضافي لتأخر cron
MAX_LATE_MIN = 180             # نافذة إنتاج الفائت (Worker له MAX_LATE_MIN الخاص)
PUBLISH_TOLERANCE_MIN = 20  # نافذة السماح بعد الموعد (تأخر cron)

# أوقات النشر بالتوقيت المحلي لكل لغة (تتبع التوقيت الصيفي تلقائياً)
#   short: [(ساعة, دقيقة), ...] يومياً          long: [(يوم, ساعة, دقيقة), ...] أسبوعياً
#   أيام الأسبوع: 0=الاثنين 1=الثلاثاء 2=الأربعاء 3=الخميس 4=الجمعة 5=السبت 6=الأحد
# قاعدة السقف: في يوم الفيديو الطويل يُحذف آخر Short في ذلك اليوم
#   (3 منشورات كحد أقصى يومياً لكل قناة) — انظر DROP_LAST_SHORT_ON_LONG_DAY
DROP_LAST_SHORT_ON_LONG_DAY = True

PUBLISH_SLOTS: dict[str, dict[str, list[tuple]]] = {
    "en": {"short": [(12, 0), (16, 0), (20, 0)],      # America/New_York
           "long":  [(1, 20, 0), (5, 20, 0)]},        # الثلاثاء، السبت
    "fr": {"short": [(12, 0), (16, 0), (20, 0)],      # Europe/Paris
           "long":  [(2, 20, 0), (6, 20, 0)]},        # الأربعاء، الأحد
    "es": {"short": [(14, 0), (18, 0), (22, 0)],      # Europe/Madrid
           "long":  [(4, 20, 0), (6, 20, 0)]},        # الجمعة، الأحد
    "ar": {"short": [(13, 0), (17, 0), (21, 0)],      # Asia/Riyadh
           "long":  [(2, 20, 0), (5, 20, 0)]},        # الأربعاء، السبت
}


# ═══════════════════════════════════════════════════════════════
# R2 (الفيديوهات + صندوق النشر + حالة القواعد)  — Make يستدعيه Cloudflare Worker
# ═══════════════════════════════════════════════════════════════

R2_RETENTION_HOURS = 48
R2_PRESIGN_SECONDS = 60 * 60 * 24 * 3   # صلاحية الرابط إن لم يكن الـ bucket عاماً



def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def get_env_lang(key: str, lang: str) -> str:
    """يقرأ KEY_LANG ثم KEY (fallback)."""
    return get_env(f"{key}_{lang.upper()}") or get_env(key)


def r2_settings() -> dict[str, str]:
    return {
        "account_id": get_env("R2_ACCOUNT_ID"),
        "access_key": get_env("R2_ACCESS_KEY_ID"),
        "secret_key": get_env("R2_SECRET_ACCESS_KEY"),
        "bucket":     get_env("R2_BUCKET"),
        "public_url": get_env("R2_PUBLIC_BASE_URL").rstrip("/"),
    }


# ═══════════════════════════════════════════════════════════════
# الذكاء الاصطناعي
# ═══════════════════════════════════════════════════════════════

GEMINI_TEXT_MODELS: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"

# احتياطي عند فشل كل مفاتيح Gemini
GROQ_MODELS: list[str] = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "allam-2-7b",
]

AI_TIMEOUT        = 60
AI_MAX_RETRIES    = 3
AI_OP_SLEEP       = float(get_env("AI_OPERATION_SLEEP", "1.0") or 1.0)
AI_BATCH_TAGS     = 10
AI_BATCH_KEYWORDS = 20


class KeyRing:
    """مجموعة مفاتيح مع تدوير آمن للخيوط.

    تدعم: PREFIX, PREFIX_1..N, PREFIX1..N
    """

    def __init__(self, prefix: str, max_scan: int = 50):
        self.prefix = prefix
        self._lock = threading.RLock()
        self._idx = 0
        self.keys = self._load(max_scan)

    def _load(self, max_scan: int) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()

        def add(v: str) -> None:
            if v and v not in seen:
                seen.add(v)
                keys.append(v)

        # سرّ واحد يحوي عدة مفاتيح مفصولة بفاصلة أو سطر جديد
        for part in re.split(r"[,\n;]+", get_env(self.prefix)):
            add(part.strip())
        for i in range(1, max_scan + 1):
            add(get_env(f"{self.prefix}_{i}"))
            add(get_env(f"{self.prefix}{i}"))
        return keys

    def __len__(self) -> int:
        return len(self.keys)

    def current(self) -> str:
        with self._lock:
            return self.keys[self._idx % len(self.keys)] if self.keys else ""

    def rotate(self) -> str:
        with self._lock:
            if len(self.keys) > 1:
                self._idx = (self._idx + 1) % len(self.keys)
            return self.current()

    @property
    def index(self) -> int:
        with self._lock:
            return self._idx


_RINGS: dict[str, KeyRing] = {}
_RINGS_LOCK = threading.Lock()


def get_ring(prefix: str) -> KeyRing:
    """KeyRing مشترك لكل prefix: GEMINI_API_KEY, GROQ_API_KEY, PEXELS_API_KEY, PIXABAY_API_KEY"""
    with _RINGS_LOCK:
        if prefix not in _RINGS:
            _RINGS[prefix] = KeyRing(prefix)
        return _RINGS[prefix]


# ═══════════════════════════════════════════════════════════════
# الفيديو والصوت
# ═══════════════════════════════════════════════════════════════

FPS = 30

DIMENSIONS: dict[str, dict[str, int]] = {
    "short":   {"width": 1080, "height": 1920},   # 9:16
    "long":    {"width": 1920, "height": 1080},   # 16:9 (يوتيوب)
    "long_fb": {"width": 1080, "height": 1920},   # لوحة فيسبوك 9:16
}
LONG_FB_VIDEO_WIDTH = 1080   # عرض الفيديو 16:9 داخل لوحة فيسبوك

DURATION_LIMITS: dict[str, dict[str, int]] = {
    "short": {"min": 30,  "max": 90},
    "long":  {"min": 120, "max": 900},
}

WPM_BASE = 150.0
# تسريع الصوت للـ Short فقط (لتحسين الإيقاع)
SPEED_MULTIPLIER: dict[str, float] = {
    "ar": 1.15, "fr": 1.05, "en": 1.15, "es": 1.10,
}
WPM_BY_LANG: dict[str, int] = {"ar": 140, "fr": 180, "en": 160, "es": 175}

MIN_VALID_AUDIO_S = 5.0
CLIP_DURATION_SHORT = 3.0
BACKGROUND_CHUNK_DUR_LONG = 6.0
MAX_LONG_CHUNKS = 80

# بصري: إبطاء جمالي (لتحسين المشاهدة) بحسب التاغ
SLOW_FACTOR_DEFAULT = 1.15
SLOW_FACTOR_BY_TAG: dict[str, float] = {
    "emotional": 1.30, "calm": 1.30, "wisdom": 1.25, "pause": 1.35,
    "whisper": 1.30, "dramatic": 1.25, "storytelling": 1.15,
    "shock": 1.0, "urgency": 1.0, "hook": 1.0, "climax": 1.0,
}

# ميتاداتا نظيفة محايدة (بدون أي ادّعاء)
CLEAN_METADATA_ARGS: list[str] = ["-map_metadata", "-1"]

# TTS: صوت Gemini ولهجة لكل لغة
TTS_VOICE: dict[str, str] = {
    "ar": "Charon", "fr": "Puck", "en": "Fenrir", "es": "Orus",
}

FFMPEG_TIMEOUT = 300
FFPROBE_TIMEOUT = 15
RENDER_TIMEOUT: dict[str, int] = {"short": 1800, "long": 7200}

# Whisper (faster-whisper مباشرة، بلا PyTorch)
WHISPER_MODEL   = "medium"
WHISPER_DEVICE  = "cpu"
WHISPER_COMPUTE = "int8"


# ═══════════════════════════════════════════════════════════════
# التاجات (22)
# ═══════════════════════════════════════════════════════════════

DEFAULT_TAG = "information"

# tag -> (تعليمة الصوت، وصف بصري لكلمات البحث)
TAGS: dict[str, tuple[str, str]] = {
    "hook":         ("Maximum energy from the first word, abrupt, impossible to scroll away",
                     "person shocked surprised face close up"),
    "intrigue":     ("Slow, lowered volume, like sharing a secret, tiny pauses",
                     "person whispering curious close up face"),
    "desire":       ("Warm, soft, savoring each word",
                     "person looking longing emotional"),
    "information":  ("Clear, natural conversational pace, light emphasis on key terms",
                     "person explaining talking to camera"),
    "inspiration":  ("Uplifting, building momentum, end with strong emphasis",
                     "person determined walking forward success"),
    "confident":    ("Bold, grounded, declarations not suggestions",
                     "confident person speaking strong posture"),
    "shock":        ("Sudden sharp delivery, higher pitch, pause after the key word",
                     "person shocked wide eyes reaction"),
    "wisdom":       ("Slow, deliberate, low and reflective",
                     "older person thinking calm contemplating"),
    "urgency":      ("Faster, higher pitch, no time to waste, imperative ending",
                     "person hurrying stressed time pressure"),
    "calm":         ("Soft, gentle, slow and reassuring",
                     "person breathing calm peaceful"),
    "emotional":    ("Tender, slightly slower, vulnerable and authentic",
                     "person emotional tears face close up"),
    "direct":       ("No fluff, straight to the point, like grabbing someone's shoulders",
                     "person pointing at camera serious"),
    "cta":          ("Energetic invitation to act now, confident momentum",
                     "person smiling inviting gesture to camera"),
    "pause":        ("Very slow, almost silent, let the moment breathe",
                     "person standing alone silent thinking"),
    "whisper":      ("Very quiet, close to the mic, forces the listener to lean in",
                     "person whispering close up secretive"),
    "curiosity":    ("Provocative, slight upward inflection, make them want the answer",
                     "person curious questioning looking"),
    "storytelling": ("Engaging narrative flow, natural rhythm, immersive",
                     "person telling story engaged speaking"),
    "dramatic":     ("Maximum emotional weight, cinematic, every word hits",
                     "intense dramatic person scene"),
    "revelation":   ("Build-up, short pause, then drop the truth like a bombshell",
                     "person realization shocked truth"),
    "tension":      ("Gradual escalation, sharper consonants, rising tension",
                     "person nervous anxious tense"),
    "climax":       ("Peak intensity, loud, sharp, undeniable",
                     "intense emotional breakthrough person"),
    "powerful":     ("Grounded weighted delivery, every syllable concrete",
                     "strong determined person unstoppable"),
}
TAG_NAMES: list[str] = list(TAGS)

TAG_ALIASES: dict[str, str] = {
    "excited": "inspiration", "happy": "inspiration", "sad": "emotional",
    "angry": "shock", "fear": "tension", "reflective": "wisdom",
    "mysterious": "intrigue", "suspense": "tension", "soft": "calm",
    "strong": "powerful", "epic": "climax", "story": "storytelling",
    "secret": "whisper", "reveal": "revelation", "truth": "revelation",
    "silence": "pause", "question": "curiosity",
    "opening": "hook", "intro": "hook", "start": "hook", "beginning": "hook",
    "direct message": "direct", "direct_message": "direct", "straight": "direct",
    "blunt": "direct", "call to action": "cta", "call_to_action": "cta",
    "action": "cta", "save": "cta", "follow": "cta", "subscribe": "cta",
    "share": "cta", "info": "information", "inspire": "inspiration",
    "conf": "confident", "emotion": "emotional", "drama": "dramatic",
    "tense": "tension",
}
FUZZY_TAG_CUTOFF = 0.65

# تصنيف تاغات للأقسام والمؤثرات
HOOK_TAGS   = frozenset({"hook", "intrigue", "shock", "urgency", "curiosity"})
CTA_TAGS    = frozenset({"cta", "confident", "inspiration", "powerful"})
IMPACT_TAGS = frozenset({"shock", "climax", "revelation", "dramatic", "hook"})
RISER_TAGS  = frozenset({"climax", "revelation", "tension"})


# ═══════════════════════════════════════════════════════════════
# أسلوب الشارع لكل لغة (النصوص + الصوت)
# ═══════════════════════════════════════════════════════════════

STREET_STYLE: dict[str, dict[str, str]] = {
    "ar": {
        "name": "لهجة الشارع السعودي (خليجي)",
        "text": ("اكتب بأسلوب شباب الخليج السعودي: والله، يبيلك، خوش، زين، شدة، "
                 "تكفى، شف، اسمع. مباشر وحماسي كأنك تكلم صاحبك. إيموجي. بلا فصحى رسمية."),
        "voice": ("Young Saudi street narrator, late 20s, confident. Authentic Najdi/Hijazi "
                  "Khaliji dialect, ق pronounced as 'g'. Never Fusha, Egyptian or Levantine."),
        "hashtags": "hashtags بالعربية والإنجليزية",
        "cta": "شاهد الفيديو كاملاً! 🔥",
    },
    "fr": {
        "name": "argot français de rue",
        "text": ("Écris en argot français urbain: wesh, c'est ouf, trop stylé, carrément, "
                 "grave, c'est chaud, t'as vu. Direct et énergique, comme à un pote. "
                 "Emojis. Pas de français formel."),
        "voice": ("Young Parisian street narrator, mid-20s. Modern urban Parisian French, "
                  "street contractions (t'es, j'sais pas). Never formal, never Quebec."),
        "hashtags": "hashtags en français et en anglais",
        "cta": "Regarde la vidéo complète ! 🔥",
    },
    "en": {
        "name": "American street slang",
        "text": ("Write in authentic Gen Z American street slang: no cap, fr fr, lowkey, "
                 "bussin, it's giving, facts, bet, deadass, hits different. Keep it real "
                 "and hype. Lots of emojis. No formal English."),
        "voice": ("Young American urban narrator, mid-20s. NYC/LA Gen Z cadence, natural "
                  "contractions (gonna, tryna). Never Southern, British or corporate."),
        "hashtags": "hashtags in English only",
        "cta": "Watch the full video! 🔥",
    },
    "es": {
        "name": "lenguaje de calle español (España)",
        "text": ("Escribe en jerga callejera de España: tío, mola, flipar, de puta madre "
                 "(suave), qué fuerte, en plan, vamos, hostia, currar, guay, ni de coña. "
                 "Directo y con energía, como hablando con un colega. Emojis. "
                 "Nada de español formal ni latinoamericano."),
        "voice": ("Young Spanish street narrator from Madrid, mid-20s, confident. Peninsular "
                  "Spanish with vosotros, distinción c/z, urban Madrid slang. Never Latin "
                  "American accent, never formal."),
        "hashtags": "hashtags en español e inglés",
        "cta": "¡Mira el vídeo completo! 🔥",
    },
}

HOOK_DEFAULT: dict[str, str] = {
    "ar": "🔴 لا تتجاوز هذا", "fr": "🔴 Ne rate pas ça",
    "en": "🔴 Don't skip this", "es": "🔴 No te lo saltes",
}

# ═══════════════════════════════════════════════════════════════
# أعمدة الجداول
# ═══════════════════════════════════════════════════════════════

COLUMN_ALIASES: dict[str, list[str]] = {
    "number":        ["number", "num", "no", "id", "video_number", "رقم", "#", "n"],
    "title":         ["title", "name", "video_title", "subject", "عنوان", "titre", "nom", "titulo", "título"],
    "content":       ["content", "text", "script", "contenu", "texte", "contenido", "texto",
                      "محتوى", "النص", "نص"],
    "thumbnail_url": ["thumbnail_url", "thumbnail", "thumb_url", "thumb",
                      "الصورة_المصغرة", "رابط_الصورة_المصغرة"],
}


# ═══════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════

def setup_logging(level: int = logging.INFO) -> None:
    """يُستدعى مرة واحدة من نقطة الدخول فقط."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def utc_iso() -> str:
    """صيغة الوقت الوحيدة المعتمدة في كل المشروع (UTC)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tz_of(lang: str):
    from zoneinfo import ZoneInfo
    return ZoneInfo(LANG_TIMEZONES[lang])
