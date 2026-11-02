"""
db.py — قاعدة بيانات SQLite مبسطة (v1.0)

الجداول:
  used_videos : فيديوهات Pexels/Pixabay المستخدمة (لعدم التكرار)
  ai_cache    : نتائج الذكاء الاصطناعي (JSON واحد لكل فيديو)
  videos      : دورة حياة الإنتاج: pending → rendering → ready | failed
              (النشر الفعلي يتولاه Cloudflare Worker عبر صندوق R2، لا يمرّ بهذه القاعدة)

قاعدة لكل لغة (vsg_{lang}.db) تُحفظ في R2 بين التشغيلات — انظر use_database().

كل الأوقات UTC بصيغة واحدة: YYYY-MM-DDTHH:MM:SSZ  (انظر config.utc_iso)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from config import DB_PATH, LANGS, MODES, utc_iso, validate_lang, validate_mode

log = logging.getLogger(__name__)

_local      = threading.local()
_write_lock = threading.RLock()

STATUSES = ("pending", "rendering", "ready", "failed")
MAX_ERROR_LEN = 500


# ═══════════════════════════════════════════════════
# الاتصال
# ═══════════════════════════════════════════════════

def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _create_connection()
        _local.conn = c
    return c


def close_thread_conn() -> None:
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None


def use_database(path: "str | Path") -> None:
    """يبدّل ملف القاعدة (قاعدة لكل لغة). يغلق اتصال الخيط الحالي."""
    global DB_PATH
    close_thread_conn()
    DB_PATH = Path(path)


def checkpoint_and_close() -> None:
    """يدمج WAL في الملف الرئيسي ويغلق الاتصال — قبل رفع الملف إلى R2."""
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    close_thread_conn()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """كتابة آمنة: BEGIN IMMEDIATE / COMMIT / ROLLBACK (متداخلة عبر SAVEPOINT)."""
    with _write_lock:
        c = conn()
        if c.in_transaction:
            n = getattr(_local, "sp", 0) + 1
            _local.sp = n
            name = f"sp_{threading.get_ident()}_{n}"
            try:
                c.execute(f"SAVEPOINT {name}")
                yield c
                c.execute(f"RELEASE SAVEPOINT {name}")
            except Exception:
                try:
                    c.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    c.execute(f"RELEASE SAVEPOINT {name}")
                except Exception:
                    pass
                raise
            finally:
                _local.sp = n - 1
        else:
            try:
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.execute("COMMIT")
            except Exception:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                raise


# ═══════════════════════════════════════════════════
# المخطط
# ═══════════════════════════════════════════════════

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS used_videos (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        source    TEXT NOT NULL,
        keyword   TEXT,
        used_at   TEXT NOT NULL,
        UNIQUE(source_id, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_cache (
        cache_key    TEXT PRIMARY KEY,
        lang         TEXT NOT NULL,
        content_mode TEXT NOT NULL,
        text_hash    TEXT,
        data         TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        video_number TEXT NOT NULL,
        lang         TEXT NOT NULL,
        content_mode TEXT NOT NULL,
        slot_key     TEXT,
        title        TEXT,
        status       TEXT NOT NULL DEFAULT 'pending',
        publish_at   TEXT,
        yt_path      TEXT,
        fb_path      TEXT,
        yt_r2_key    TEXT,
        fb_r2_key    TEXT,
        thumb_r2_key TEXT,
        duration_s   REAL,
        error        TEXT,
        archived     INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        CHECK(status IN ('pending','rendering','ready','failed'))
    )
    """,
    # فيديو واحد نشط لكل (رقم، لغة، نوع) وموعد واحد لكل slot
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_videos_active
       ON videos(video_number, lang, content_mode) WHERE archived = 0""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_videos_slot
       ON videos(slot_key) WHERE slot_key IS NOT NULL AND archived = 0""",
    "CREATE INDEX IF NOT EXISTS ix_videos_due ON videos(status, publish_at)",
    "CREATE INDEX IF NOT EXISTS ix_videos_lang ON videos(lang, content_mode, status)",
]


def init_db() -> None:
    with _write_lock:
        c = conn()
        if c.in_transaction:
            c.execute("ROLLBACK")
        c.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA:
                c.execute(stmt)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    log.info("  ✅ DB ready")


# ═══════════════════════════════════════════════════
# Used videos
# ═══════════════════════════════════════════════════

def is_video_used(source_id: str, source: str) -> bool:
    row = conn().execute(
        "SELECT 1 FROM used_videos WHERE source_id=? AND source=?",
        (str(source_id), source),
    ).fetchone()
    return row is not None


def mark_video_used(source_id: str, keyword: str, source: str) -> None:
    with write_transaction() as c:
        c.execute(
            "INSERT OR IGNORE INTO used_videos(source_id, source, keyword, used_at) "
            "VALUES (?,?,?,?)",
            (str(source_id), source, str(keyword)[:200], utc_iso()),
        )


def reset_used_videos() -> int:
    with write_transaction() as c:
        return c.execute("DELETE FROM used_videos").rowcount


# ═══════════════════════════════════════════════════
# AI cache
# ═══════════════════════════════════════════════════

def make_cache_key(video_number: Any, lang: str, mode: str) -> str:
    validate_lang(lang)
    validate_mode(mode)
    return f"{str(video_number).strip()}_{lang}_{mode}"


def get_ai_cache(cache_key: str) -> Optional[dict]:
    row = conn().execute(
        "SELECT data, text_hash FROM ai_cache WHERE cache_key=?", (cache_key,)
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        data["_text_hash"] = row["text_hash"]
    return data


def save_ai_cache(cache_key: str, lang: str, mode: str,
                  data: dict, text_hash: str = "") -> None:
    validate_lang(lang)
    validate_mode(mode)
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    now = utc_iso()
    with write_transaction() as c:
        c.execute(
            """
            INSERT INTO ai_cache(cache_key, lang, content_mode, text_hash, data,
                                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                text_hash=excluded.text_hash, data=excluded.data,
                updated_at=excluded.updated_at
            """,
            (cache_key, lang, mode, text_hash,
             json.dumps(clean, ensure_ascii=False), now, now),
        )


def clear_ai_cache(cache_key: Optional[str] = None) -> int:
    with write_transaction() as c:
        if cache_key:
            return c.execute("DELETE FROM ai_cache WHERE cache_key=?", (cache_key,)).rowcount
        return c.execute("DELETE FROM ai_cache").rowcount


# ═══════════════════════════════════════════════════
# Videos (دورة الحياة)
# ═══════════════════════════════════════════════════

def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(r) if r else None


def get_video(video_number: Any, lang: str, mode: str) -> Optional[dict]:
    """الفيديو النشط (غير المؤرشف)."""
    return _row(conn().execute(
        "SELECT * FROM videos WHERE video_number=? AND lang=? AND content_mode=? "
        "AND archived=0", (str(video_number), lang, mode)).fetchone())


def get_video_by_slot(slot_key: str) -> Optional[dict]:
    return _row(conn().execute(
        "SELECT * FROM videos WHERE slot_key=? AND archived=0", (slot_key,)).fetchone())


def create_video(video_number: Any, lang: str, mode: str, title: str,
                 publish_at: str, slot_key: Optional[str] = None) -> int:
    """ينشئ سجلاً بحالة pending. يرمي IntegrityError إن وُجد نشط مسبقاً."""
    validate_lang(lang)
    validate_mode(mode)
    now = utc_iso()
    with write_transaction() as c:
        cur = c.execute(
            """INSERT INTO videos(video_number, lang, content_mode, slot_key, title,
                                  status, publish_at, created_at, updated_at)
               VALUES (?,?,?,?,?, 'pending', ?, ?, ?)""",
            (str(video_number), lang, mode, slot_key, title, publish_at, now, now),
        )
        return int(cur.lastrowid)


def set_status(video_id: int, status: str, error: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'")
    with write_transaction() as c:
        c.execute(
            "UPDATE videos SET status=?, error=?, updated_at=? WHERE id=?",
            (status, error[:MAX_ERROR_LEN] if error else None, utc_iso(), video_id),
        )


def mark_ready(video_id: int, duration_s: float,
               yt_path: str = "", fb_path: str = "",
               yt_r2_key: str = "", fb_r2_key: str = "",
               thumb_r2_key: str = "") -> None:
    """الفيديو جاهز ومرفوع على R2 وينتظر وقت النشر."""
    with write_transaction() as c:
        c.execute(
            """UPDATE videos SET status='ready', duration_s=?, yt_path=?, fb_path=?,
                   yt_r2_key=?, fb_r2_key=?, thumb_r2_key=?, error=NULL, updated_at=?
               WHERE id=?""",
            (float(duration_s), yt_path or None, fb_path or None,
             yt_r2_key or None, fb_r2_key or None, thumb_r2_key or None,
             utc_iso(), video_id),
        )




def mark_failed(video_id: int, error: str) -> None:
    set_status(video_id, "failed", error)




def reset_stuck_rendering(max_age_minutes: int = 180) -> int:
    """يعيد 'rendering' العالق (انقطاع job) إلى failed ليُعاد إنتاجه."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with write_transaction() as c:
        return c.execute(
            "UPDATE videos SET status='failed', error='stuck rendering', updated_at=? "
            "WHERE status='rendering' AND updated_at < ?", (utc_iso(), cutoff)).rowcount


def delete_video(video_id: int) -> None:
    """يحذف سجلاً (مثلاً failed) ليُعاد إنتاج نفس الـ slot."""
    with write_transaction() as c:
        c.execute("DELETE FROM videos WHERE id=?", (video_id,))


# ═══════════════════════════════════════════════════
# اختيار الفيديو التالي + الدورة
# ═══════════════════════════════════════════════════

def get_next_video_number(lang: str, mode: str,
                          available_numbers: list[str]) -> Optional[str]:
    """أول رقم لا يملك سجلاً نشطاً (pending/rendering/ready/sent).

    الفاشل لا يُحتسب فيُعاد اختياره. يرجع None إذا استُنفدت كل الأرقام.
    """
    validate_lang(lang)
    validate_mode(mode)
    rows = conn().execute(
        "SELECT video_number FROM videos WHERE lang=? AND content_mode=? "
        "AND archived=0 AND status != 'failed'", (lang, mode)).fetchall()
    taken = {str(r["video_number"]) for r in rows}
    for n in available_numbers:
        if str(n) not in taken:
            return str(n)
    return None


def start_new_cycle(lang: str, mode: str) -> int:
    """عند استنفاد الجدول: يؤرشف السجلات المُنتَجة (يحفظ التاريخ) ويبدأ دورة جديدة."""
    validate_lang(lang)
    validate_mode(mode)
    with write_transaction() as c:
        n = c.execute(
            "UPDATE videos SET archived=1, updated_at=? WHERE lang=? AND content_mode=? "
            "AND status='ready' AND archived=0", (utc_iso(), lang, mode)).rowcount
    log.info("  🔄 New cycle %s/%s — %d archived", lang, mode, n)
    return n


# ═══════════════════════════════════════════════════
# ملخص
# ═══════════════════════════════════════════════════

def print_db_summary() -> None:
    try:
        rows = conn().execute(
            "SELECT lang, content_mode, status, COUNT(*) n FROM videos "
            "WHERE archived=0 GROUP BY lang, content_mode, status "
            "ORDER BY lang, content_mode").fetchall()
        used = conn().execute("SELECT COUNT(*) FROM used_videos").fetchone()[0]
        log.info("  📊 DB: %d stock videos used", used)
        for r in rows:
            log.info("     %s/%-5s %-9s %d", r["lang"], r["content_mode"],
                     r["status"], r["n"])
    except Exception as e:
        log.debug("summary error: %s", e)
