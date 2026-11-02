"""
content.py — المحتوى: قراءة الجداول + التاجات + الإثراء بالذكاء الاصطناعي (v1.0)

يدمج: script_reader + tags_parser + ai_enricher
الذكاء الاصطناعي: Gemini أولاً (تدوير المفاتيح والموديلات)، وعند فشل الكل → Groq.

الواجهة العامة:
    read_scripts(path)                         -> list[record]
    process_content(content, lang, mode)       -> list[unit]
    enrich(record, lang, mode, units, force)   -> dict  (يحتوي "tagged" بالتاجات النهائية)
    strip_tags(text)
    call_ai(prompt, ...)                       -> str
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import time
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Optional

import config as C
import db

log = logging.getLogger(__name__)


class AIError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
# 1) التاجات
# ═══════════════════════════════════════════════════════════════

_TAG_RE = re.compile(r"\[([A-Za-z_ ]+)\]")


def strip_tags(text: str) -> str:
    """يزيل كل [tag] ويحافظ على حدود الكلمات."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


def auto_correct_tag(raw: Optional[str]) -> tuple[Optional[str], str]:
    """exact → lowercase → alias → fuzzy. يرجع (tag, source) أو (None, 'no_match')."""
    if not raw or not raw.strip():
        return None, "empty"
    s = raw.strip()
    if s in C.TAGS:
        return s, "exact"
    low = s.lower()
    if low in C.TAGS:
        return low, "case_fixed"
    if low in C.TAG_ALIASES:
        return C.TAG_ALIASES[low], "alias"
    m = get_close_matches(low, C.TAG_NAMES, n=1, cutoff=C.FUZZY_TAG_CUTOFF)
    if m:
        return m[0], "fuzzy"
    return None, "no_match"


def _split_by_tags(content: str) -> list[tuple[Optional[str], str]]:
    """يقسم النص إلى (tag_خام, نص). النص قبل أول tag يأخذ tag=None."""
    text = (content or "").strip()
    if not text:
        return []
    matches = list(_TAG_RE.finditer(text))
    if not matches:
        return [(None, text)]

    out: list[tuple[Optional[str], str]] = []
    pre = text[: matches[0].start()].strip()
    if pre:
        out.append((None, pre))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():end].strip()
        if seg:
            out.append((m.group(1).strip(), seg))
    return out


# ═══════════════════════════════════════════════════════════════
# 2) تقسيم المحتوى إلى وحدات
# ═══════════════════════════════════════════════════════════════

_SENT_RE = re.compile(r"(?<=[.!?؟…])\s+|\n+")
MIN_UNIT_CHARS = 2
MIN_UNIT_WORDS = 2


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(text) if p and p.strip()]
    merged: list[str] = []
    for p in parts:
        if len(p) < MIN_UNIT_CHARS:
            continue
        if merged and len(p.split()) < MIN_UNIT_WORDS:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged


def _make_unit(raw_tag: Optional[str], text: str, line: int) -> dict:
    corrected, src = auto_correct_tag(raw_tag)
    if corrected:
        final, source = corrected, src
    else:
        final, source = C.DEFAULT_TAG, "needs_ai"
    return {
        "raw_tag": raw_tag or "",
        "final_tag": final,
        "tag_source": source,
        "text": text,
        "line": line,
    }


def process_content(content: str, lang: str, mode: str) -> list[dict]:
    """
    Short: كل جملة وحدة (تَرِث tag الفقرة).
    Long : كل [tag]+فقرة وحدة واحدة (لا تُقسَّم). النص بلا tags يُقسَّم لفقرات.
    """
    C.validate_lang(lang)
    C.validate_mode(mode)
    units: list[dict] = []
    line = 1

    for raw_tag, seg in _split_by_tags(content):
        if mode == "long":
            pieces = [seg] if raw_tag else [
                p.strip() for p in re.split(r"\n\s*\n", seg) if p.strip()]
        else:
            pieces = _split_sentences(seg)
        for piece in pieces:
            piece = strip_tags(piece)
            if len(piece) < MIN_UNIT_CHARS:
                continue
            units.append(_make_unit(raw_tag, piece, line))
            line += 1

    log.info("  📝 %s/%s: %d units (%d need AI tag)", lang, mode, len(units),
             sum(1 for u in units if u["tag_source"] == "needs_ai"))
    return units


# ═══════════════════════════════════════════════════════════════
# 3) قراءة الجداول
# ═══════════════════════════════════════════════════════════════

_NORM_RE = re.compile(r"[\s\-/\\]+")
_INVALID_NUMBERS = frozenset({"", "number", "num", "no", "n", "id", "#", "رقم", "video_number"})


def _norm(s: Any) -> str:
    return _NORM_RE.sub("_", str(s).strip().lower())


def _detect_file_meta(path: Path) -> tuple[str, str]:
    """يستخرج (lang, mode) من اسم الملف {lang}_{mode}.xlsx — صارم."""
    m = re.fullmatch(r"(ar|fr|en|es)_(short|long)", path.stem.lower())
    if not m:
        raise ValueError(
            f"Bad script filename '{path.name}'. Expected {{lang}}_{{mode}}.xlsx "
            f"(e.g. ar_short.xlsx, es_long.xlsx)")
    return m.group(1), m.group(2)


def _detect_columns(headers: list[str], lang: str) -> dict[str, int]:
    normed = [_norm(h) for h in headers]
    col: dict[str, int] = {}
    for field, aliases in C.COLUMN_ALIASES.items():
        al = {_norm(a) for a in aliases}
        if field == "content":
            al |= {f"{lang}_content", f"content_{lang}"}
        for i, h in enumerate(normed):
            if h in al:
                col[field] = i
                break
    for field, pos in (("number", 0), ("title", 1), ("content", 2)):
        if field not in col and len(headers) > pos:
            col[field] = pos
            log.warning("  ⚠️  column '%s' not found — using col %d (%r)",
                        field, pos, headers[pos])
    return col


def _cell(row: Any, idx: Optional[int]) -> str:
    if idx is None:
        return ""
    try:
        v = row[idx]
    except (IndexError, TypeError):
        return ""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))          # 1.0 → "1"
    return str(v).strip()


def _rows_to_records(rows: list, col: dict[str, int], lang: str, mode: str) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    auto = 0
    for row in rows[1:]:
        if not row or not any(c is not None and str(c).strip() for c in row):
            continue
        auto += 1
        number = _cell(row, col.get("number"))
        if _norm(number) in _INVALID_NUMBERS:
            number = str(auto)
        title = _cell(row, col.get("title"))
        content = _cell(row, col.get("content"))
        if not title or not content:
            log.warning("  ⚠️  row %s skipped (no title/content)", number)
            continue
        base, k = number, 1
        while number in seen:
            number = f"{base}_{k}"
            k += 1
        if number != base:
            log.warning("  ⚠️  duplicate number %s → %s", base, number)
        seen.add(number)
        records.append({
            "number": number,
            "title": title,
            "content": content,
            "thumbnail_url": _cell(row, col.get("thumbnail_url")) if mode == "long" else "",
            "lang": lang,
            "content_mode": mode,
        })
    return records


def read_scripts(file_path: str | Path) -> list[dict]:
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Script file not found: {path}")
    lang, mode = _detect_file_meta(path)

    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
        except ImportError as e:
            raise ImportError("pip install openpyxl") from e
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = next((wb[n] for n in wb.sheetnames
                   if n.lower() in ("scripts", "data", "videos", "content", "sheet1")),
                  wb.active)
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    elif ext == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [list(r) for r in csv.reader(f)]
    else:
        raise ValueError(f"Unsupported format '{ext}' (use .xlsx or .csv)")

    if not rows or not any(str(h).strip() for h in rows[0] if h is not None):
        raise RuntimeError(f"No valid header row in {path.name}")

    col = _detect_columns([str(h) if h is not None else "" for h in rows[0]], lang)
    records = _rows_to_records(rows, col, lang, mode)
    log.info("  ✅ %d records from %s [%s/%s]", len(records), path.name,
             lang.upper(), mode.upper())
    return records


# ═══════════════════════════════════════════════════════════════
# 4) عميل الذكاء الاصطناعي: Gemini → Groq
# ═══════════════════════════════════════════════════════════════

_RATE_HINTS = ("429", "resource_exhausted", "quota", "rate limit", "rate_limit",
               "too many requests", "tokens per")
_AUTH_HINTS = ("401", "403", "permission_denied", "api key", "api_key_invalid",
               "unauthenticated")
_MODEL_HINTS = ("404", "not found", "model_not_found", "does not exist", "not supported")

_gemini_clients: dict[str, Any] = {}
_groq_clients: dict[str, Any] = {}


def _classify(err: Exception) -> str:
    s = str(err).lower()
    if any(h in s for h in _MODEL_HINTS):
        return "model"
    if any(h in s for h in _RATE_HINTS):
        return "rate"
    if any(h in s for h in _AUTH_HINTS):
        return "auth"
    return "other"


def _call_gemini(prompt: str, max_tokens: int, temperature: float,
                 json_mode: bool, op: str) -> Optional[str]:
    ring = C.get_ring("GEMINI_API_KEY")
    if not len(ring):
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log.warning("  ⚠️  google-genai not installed")
        return None

    for model in C.GEMINI_TEXT_MODELS:
        for _ in range(len(ring) * 2):
            key = ring.current()
            try:
                client = _gemini_clients.setdefault(key, genai.Client(api_key=key))
                kwargs: dict[str, Any] = dict(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                )
                if json_mode:
                    kwargs["response_mime_type"] = "application/json"
                try:
                    kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
                except Exception:
                    pass
                log.info("  🤖 %s [gemini %s key#%d]", op, model, ring.index + 1)
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(**kwargs))
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    return text
                raise AIError("empty response")
            except Exception as e:
                kind = _classify(e)
                log.warning("  ⚠️  gemini %s [%s]: %s", model, kind, str(e)[:100])
                if kind == "model":
                    break                       # الموديل التالي
                ring.rotate()
                time.sleep(2 if kind == "rate" else 1)
    return None


def _call_groq(prompt: str, max_tokens: int, temperature: float, op: str) -> Optional[str]:
    ring = C.get_ring("GROQ_API_KEY")
    if not len(ring):
        return None
    try:
        from groq import Groq
    except ImportError:
        log.warning("  ⚠️  groq not installed")
        return None

    for model in C.GROQ_MODELS:
        for _ in range(len(ring) * 2):
            key = ring.current()
            try:
                client = _groq_clients.setdefault(
                    key, Groq(api_key=key, timeout=C.AI_TIMEOUT))
                log.info("  🛟 %s [groq %s key#%d]", op, model, ring.index + 1)
                resp = client.chat.completions.create(
                    model=model, temperature=temperature, max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}])
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    return text
                raise AIError("empty response")
            except Exception as e:
                kind = _classify(e)
                log.warning("  ⚠️  groq %s [%s]: %s", model, kind, str(e)[:100])
                if kind == "model":
                    break
                ring.rotate()
                time.sleep(2 if kind == "rate" else 1)
    return None


def call_ai(prompt: str, *, max_tokens: int = 1500, temperature: float = 0.7,
            json_mode: bool = False, op: str = "AI") -> str:
    """Gemini أولاً، وإن فشلت كل المفاتيح والموديلات → Groq."""
    text = _call_gemini(prompt, max_tokens, temperature, json_mode, op)
    if text:
        return text
    log.warning("  🔀 %s: Gemini failed on all keys — switching to Groq", op)
    text = _call_groq(prompt, max_tokens, temperature, op)
    if text:
        return text
    raise AIError(f"{op}: all Gemini and Groq keys failed")


# ═══════════════════════════════════════════════════════════════
# 5) JSON parsing (خفيف — Gemini يدعم JSON mode)
# ═══════════════════════════════════════════════════════════════

def _strip_wrappers(raw: str) -> str:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL)
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t.strip())
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _close_open_brackets(t: str) -> str:
    stack: list[str] = []
    in_str = esc = False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        t += '"'
    return t + "".join(reversed(stack))


def parse_json(raw: str, expected: type) -> Any:
    t = _strip_wrappers(raw)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        raise AIError("no JSON found")
    t = t[min(starts):]
    dec = json.JSONDecoder()

    candidates = [t, re.sub(r",\s*([}\]])", r"\1", t),
                  _close_open_brackets(re.sub(r",\s*([}\]])", r"\1", t))]
    for cand in candidates:
        try:
            obj, _ = dec.raw_decode(cand)
            if isinstance(obj, expected):
                return obj
            if expected is list and isinstance(obj, dict):
                for v in obj.values():
                    if isinstance(v, list):
                        return v
        except json.JSONDecodeError:
            continue
    raise AIError(f"invalid JSON: {t[:150]}")


def _ask_json(prompt: str, expected: type, *, op: str, max_tokens: int = 1500,
              temperature: float = 0.5, retries: int = 1) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        raw = call_ai(prompt, max_tokens=max_tokens,
                      temperature=temperature if attempt == 0 else min(temperature + 0.2, 1.0),
                      json_mode=True, op=op)
        try:
            return parse_json(raw, expected)
        except AIError as e:
            last = e
            log.warning("  ⚠️  %s: %s — retry %d", op, e, attempt + 1)
    raise AIError(f"{op}: {last}")


# ═══════════════════════════════════════════════════════════════
# 6) مراحل الإثراء
# ═══════════════════════════════════════════════════════════════

CONTENT_TYPES = ("psychology relationships business lifestyle motivation education "
                 "health spirituality finance social_skills").split()
EMOTIONS = "curiosity fear desire anger hope sadness joy awe surprise".split()
TONES = "energetic calm emotional inspirational mysterious urgent".split()

DEFAULT_ACCENTS = ["#FF003C", "#FFD700", "#00FFFF", "#39FF14"]
_HEX6 = re.compile(r"#?([0-9A-Fa-f]{6})\b")
_HEX3 = re.compile(r"#([0-9A-Fa-f]{3})\b")

_PLACEHOLDER_RE = re.compile(r"^\s*(kw|keyword|item|example)\s*\d*\s*$", re.I)
_ABSTRACT = frozenset(
    "mystery mysterious journey soul shadows silence whisper darkness longing ethereal "
    "abstract spiritual void abyss illusion dream fantasy essence energy vibes magic "
    "surreal haunting melancholy solitude echo horizon twilight dusk mist fog haze glow "
    "radiance aura pulse rhythm flow".split())


def _trunc(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("."))
    return (cut[:sp] if sp > n * 0.75 else cut).strip() + "..."


# ── 6.1 التحليل + الكلمات القوية + hook + ألوان ─────────────────

def _normalize_color(c: Any) -> Optional[str]:
    if isinstance(c, dict):
        for k in ("hex", "color", "code", "value"):
            if k in c:
                return _normalize_color(c[k])
        return None
    s = str(c).strip()
    m = _HEX6.fullmatch(s.lstrip("#")) and _HEX6.search(s)
    if m:
        return "#" + m.group(1).upper()
    m3 = _HEX3.search(s)
    if m3:
        r, g, b = m3.group(1)
        return f"#{r}{r}{g}{g}{b}{b}".upper()
    return None


def _analyze(title: str, content: str, lang: str, mode: str) -> dict:
    name = C.LANG_NAMES[lang]
    want_hook = mode == "short"
    prompt = f"""Analyze this {name} video script and return ONE JSON object.

TITLE: {_trunc(title, 80)}
SCRIPT:
{_trunc(content, 900)}

Return exactly these keys:
- content_type: one of {CONTENT_TYPES}
- primary_emotion: one of {EMOTIONS}
- tone: one of {TONES}
- intensity: integer 1-10
- power_words: 10 single emotionally strong words that appear in the script (in {name})
- hook_keyword: 3-5 English words for stock footage of the opening (person + emotion/action)
- custom_hook: {"a hook sentence in " + name + ", max 10 words, instant curiosity, no quotes" if want_hook else '""'}
- accent_colors: 4 vibrant HEX colors like "#FF003C"
"""
    data = _ask_json(prompt, dict, op="Analysis", max_tokens=900, temperature=0.4)

    ctype = str(data.get("content_type", "")).lower()
    emo = str(data.get("primary_emotion", "")).lower()
    tone = str(data.get("tone", "")).lower()
    try:
        inten = max(1, min(10, int(re.findall(r"\d+", str(data.get("intensity", 7)))[0])))
    except (IndexError, ValueError):
        inten = 7

    pw: list[str] = []
    for w in data.get("power_words") or []:
        w = str(w).strip()
        if w and " " not in w and len(w) >= 2 and w.lower() not in {p.lower() for p in pw}:
            pw.append(w)

    colors: list[str] = []
    for c in data.get("accent_colors") or []:
        n = _normalize_color(c)
        if n and n not in colors:
            colors.append(n)
    for d in DEFAULT_ACCENTS:
        if len(colors) >= 4:
            break
        if d not in colors:
            colors.append(d)

    hook_kw = str(data.get("hook_keyword") or "").strip().strip('"')
    if not hook_kw or len(hook_kw) > 80 or any(w in _ABSTRACT for w in hook_kw.lower().split()):
        hook_kw = "person emotional dramatic close up face"

    return {
        "analysis": {
            "content_type": ctype if ctype in CONTENT_TYPES else "psychology",
            "primary_emotion": emo if emo in EMOTIONS else "curiosity",
            "tone": tone if tone in TONES else "energetic",
            "intensity": inten,
        },
        "power_words": pw[:10],
        "hook_keyword": hook_kw,
        "custom_hook": str(data.get("custom_hook") or "").strip().strip('"') if want_hook else "",
        "accent_colors": colors[:4],
    }


# ── 6.2 اقتراح التاجات ──────────────────────────────────────────

BATCH_TAGS = 25


def _suggest_tags(units: list[dict], need: list[int], analysis: dict, lang: str) -> None:
    """يملأ final_tag للوحدات ذات tag_source == 'needs_ai'."""
    if not need:
        return
    tags_list = ", ".join(C.TAG_NAMES)
    for s in range(0, len(need), BATCH_TAGS):
        chunk = need[s:s + BATCH_TAGS]
        lines = "\n".join(f"{i + 1}. {_trunc(units[idx]['text'], 120)}"
                          for i, idx in enumerate(chunk))
        prompt = f"""Choose ONE emotional delivery tag for each narration sentence.
Context: {analysis['content_type']} content, {analysis['tone']} tone. Language: {C.LANG_NAMES[lang]}.
Allowed tags: {tags_list}
The FIRST sentence of the whole video should usually be "hook".
Sentences ({len(chunk)}):
{lines}

Return a JSON array of exactly {len(chunk)} tag strings."""
        try:
            arr = _ask_json(prompt, list, op=f"Tags[{s + 1}-{s + len(chunk)}]",
                            max_tokens=400, temperature=0.4)
        except AIError as e:
            log.warning("  ⚠️  tag suggestion failed: %s — default tag", e)
            arr = []
        for i, idx in enumerate(chunk):
            tag, _ = auto_correct_tag(str(arr[i])) if i < len(arr) else (None, "")
            units[idx]["final_tag"] = tag or C.DEFAULT_TAG
            units[idx]["tag_source"] = "ai_suggested" if tag else "fallback"


# ── 6.3 كلمات البحث البصرية (Gemini) ───────────────────────────

BATCH_KEYWORDS = 20
_KW_FORMAT_EXAMPLE = json.dumps([
    ["person closing laptop tired night", "hands rubbing eyes desk lamp", "man staring window thinking"],
    ["woman laughing friends cafe", "two friends hugging outdoor", "person smiling reading phone"],
])


def _tag_fallback_keywords(tag: str) -> list[str]:
    base = C.TAGS.get(tag, C.TAGS[C.DEFAULT_TAG])[1]
    return [base, f"{base} cinematic", "person serious face talking camera"]


def _clean_kw_row(row: Any) -> list[str]:
    if isinstance(row, str):
        row = [row]
    if not isinstance(row, list):
        return []
    out: list[str] = []
    for k in row[:5]:
        k = str(k).strip()
        if not k or _PLACEHOLDER_RE.match(k):
            continue
        words = k.lower().split()
        if any(w in _ABSTRACT for w in words):
            words = [w for w in words if w not in _ABSTRACT]
            if len(words) < 2:
                continue
            k = " ".join(words)
        if k.lower() not in {o.lower() for o in out}:
            out.append(k)
    return out


def _visual_keywords(units: list[dict], title: str, analysis: dict) -> list[list[str]]:
    n = len(units)
    result: list[list[str]] = [[] for _ in range(n)]

    for s in range(0, n, BATCH_KEYWORDS):
        chunk = list(range(s, min(s + BATCH_KEYWORDS, n)))
        block = "".join(
            f"Sentence {i + 1} [{units[idx]['final_tag']}]: "
            f"\"{_trunc(units[idx]['text'], 150)}\"\n"
            f"  mood hint: {C.TAGS.get(units[idx]['final_tag'], C.TAGS[C.DEFAULT_TAG])[1]}\n"
            for i, idx in enumerate(chunk))
        prompt = f"""You are a video editor searching Pexels/Pixabay stock footage.
Video: "{_trunc(title, 60)}" | {analysis['content_type']} | emotion: {analysis['primary_emotion']}

RULES:
1. Each query is a REAL stock-footage search: 3-5 English words.
2. Always include a person/people/face/hands and a clear action or emotion.
3. Concrete and visual. FORBIDDEN abstract words: mystery, shadows, silence, soul, journey,
   ethereal, longing, abyss, whisper, darkness, dream, essence.
4. 3 DIFFERENT queries per sentence, written for THAT sentence's meaning.

{block}
Return a JSON array of EXACTLY {len(chunk)} arrays (one per sentence, 3 strings each).
Format example for a different video (never copy it, never write placeholders):
{_KW_FORMAT_EXAMPLE}"""
        rows: list = []
        for attempt in range(2):
            try:
                rows = _ask_json(prompt, list, op=f"Keywords[{s + 1}-{chunk[-1] + 1}]",
                                 max_tokens=max(500, len(chunk) * 80),
                                 temperature=0.3 + 0.2 * attempt, retries=0)
            except AIError as e:
                log.warning("  ⚠️  keywords batch failed: %s", e)
                rows = []
            cleaned = [_clean_kw_row(rows[i]) if i < len(rows) else [] for i in range(len(chunk))]
            if sum(1 for r in cleaned if r) / len(chunk) >= 0.8:
                break
        for i, idx in enumerate(chunk):
            result[idx] = cleaned[i]

    missing = 0
    for idx, row in enumerate(result):
        if not row:
            missing += 1
            result[idx] = _tag_fallback_keywords(units[idx]["final_tag"])
        else:
            fb = _tag_fallback_keywords(units[idx]["final_tag"])
            while len(row) < 3:
                row.append(fb[len(row) % 3])
            result[idx] = row[:3]
    if missing:
        log.warning("  ⚠️  %d/%d sentences use tag-based fallback keywords", missing, n)
    return result


# ── 6.4 نصوص النشر: وصف + كابشن + هاشتاغات + وسوم يوتيوب ──────

def _clean_hashtags(items: Any, limit: int = 25) -> list[str]:
    if isinstance(items, str):
        items = re.findall(r"#[^\s#,،;|]+", items) or re.split(r"[\s,،;|]+", items)
    out: list[str] = []
    for t in items or []:
        t = str(t).strip().replace(" ", "_")
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        t = re.sub(r"[,.،;:!?]+$", "", t)
        if len(t) > 1 and t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out[:limit]


def _publish_texts(title: str, content: str, analysis: dict, lang: str, mode: str) -> dict:
    st = C.STREET_STYLE[lang]
    name = C.LANG_NAMES[lang]
    prompt = f"""Write social media publishing texts in {name} for Facebook and YouTube.

Title: "{_trunc(title, 80)}"
Type: {analysis['content_type']} | Emotion: {analysis['primary_emotion']}
Script:
{_trunc(content, 800)}

STYLE ({st['name']}): {st['text']}

Return ONE JSON object with:
- description: long description, 8-12 lines: shocking hook line, detailed explanation,
  a real example or mini-story, strong call-to-action, many emojis. NO hashtags inside.
- caption: short caption (1 hook line + 2-3 value lines + call-to-action + emojis). NO hashtags.
- hashtags: 15 hashtags ({st['hashtags']}), each starting with #, underscores instead of spaces.
- youtube_tags: 10 plain search keywords/phrases (no #), in {name} and English.
"""
    try:
        data = _ask_json(prompt, dict, op="Publish texts", max_tokens=1800, temperature=0.8)
    except AIError as e:
        log.warning("  ⚠️  publish texts failed: %s — fallback", e)
        data = {}

    desc = str(data.get("description") or "").strip()
    cap = str(data.get("caption") or "").strip()
    if len(desc) < 100:
        desc = f"{title}\n\n{st['cta']}"
    if not cap:
        cap = f"{title}\n\n{st['cta']}"

    tags = _clean_hashtags(data.get("hashtags"))
    if len(tags) < 3:
        tags = _clean_hashtags(["viral", "fyp", analysis["content_type"]])
    if mode == "short" and not any(t.lower() == "#shorts" for t in tags):
        tags.insert(0, "#Shorts")

    yt_tags = [str(t).strip().lstrip("#") for t in (data.get("youtube_tags") or [])
               if str(t).strip()][:15]

    return {
        "description": desc,
        "caption": cap,
        "hashtags": tags,
        "youtube_tags": yt_tags or [t.lstrip("#") for t in tags[:10]],
    }


def build_publish_payload(record: dict, enriched: dict, lang: str, mode: str) -> dict:
    """نصوص جاهزة لـ Make: عنوان + وصف يوتيوب + كابشن فيسبوك."""
    pub = enriched["publish"]
    tags_line = " ".join(pub["hashtags"][:20])
    title = record["title"][:100]
    return {
        "title": title,
        "youtube_description": f"{pub['description']}\n.\n.\n.\n{tags_line}"[:5000],
        "facebook_caption": f"{pub['caption']}\n.\n.\n.\n{' '.join(pub['hashtags'][:12])}",
        "youtube_tags": pub["youtube_tags"],
        "hashtags": pub["hashtags"],
    }


# ═══════════════════════════════════════════════════════════════
# 7) الواجهة الرئيسية: enrich
# ═══════════════════════════════════════════════════════════════

_BG_STYLE_BY_EMOTION = {"fear": "cinematic", "sadness": "cinematic", "awe": "blur"}


def _text_hash(units: list[dict], mode: str) -> str:
    blob = mode + "\n" + "\n".join(u["text"] for u in units)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def enrich(record: dict, lang: str, mode: str, units: list[dict],
           force: bool = False) -> dict:
    """
    يرجع dict: analysis, power_words, hook_keyword, custom_hook, accent_colors,
    visual_keywords (واحد لكل unit), publish, bg_style, tagged (units بتاجاتها النهائية).
    الكاش يُبطَل تلقائياً إذا تغيّر نص السكريبت.
    """
    C.validate_lang(lang)
    C.validate_mode(mode)
    if not units:
        raise AIError("no units to enrich")

    number = record["number"]
    title = record["title"]
    key = db.make_cache_key(number, lang, mode)
    thash = _text_hash(units, mode)

    if not force:
        cached = db.get_ai_cache(key)
        if (cached and cached.get("_text_hash") == thash
                and cached.get("tagged") and len(cached.get("visual_keywords", [])) == len(units)
                and cached.get("publish")):
            log.info("  ♻️  AI cache hit #%s", number)
            return cached
        if cached:
            log.info("  🔄 AI cache stale/incomplete #%s — regenerating", number)

    content = " ".join(u["text"] for u in units)
    log.info("\n  🧠 AI enrichment #%s (%s/%s): %s", number, lang.upper(), mode, _trunc(title, 50))

    base = _analyze(title, content, lang, mode)
    time.sleep(C.AI_OP_SLEEP)

    need = [i for i, u in enumerate(units) if u["tag_source"] == "needs_ai"]
    _suggest_tags(units, need, base["analysis"], lang)
    time.sleep(C.AI_OP_SLEEP)

    visual = _visual_keywords(units, title, base["analysis"])
    time.sleep(C.AI_OP_SLEEP)

    publish = _publish_texts(title, content, base["analysis"], lang, mode)

    enriched = {
        **base,
        "visual_keywords": visual,
        "publish": publish,
        "bg_style": _BG_STYLE_BY_EMOTION.get(base["analysis"]["primary_emotion"], "video"),
        "tagged": units,
        "lang": lang,
        "content_mode": mode,
    }
    if mode == "long":
        enriched["hook_keyword"] = visual[0][0] if visual and visual[0] else base["hook_keyword"]
        enriched["custom_hook"] = ""

    db.save_ai_cache(key, lang, mode, enriched, thash)
    log.info("  ✅ AI enrichment done #%s", number)
    return enriched
