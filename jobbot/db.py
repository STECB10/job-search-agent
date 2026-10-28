"""Base SQLite : offres vues, notes, lettres et suivi des candidatures."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Job

# Cycle de vie d'une offre :
#   filtre      éliminée par les filtres stricts (motif dans reason)
#   a_noter     en attente de notation par l'IA
#   ecarte      note sous le seuil de signalement
#   a_signaler  note moyenne, à signaler sans lettre      -> signale
#   a_envoyer   bonne note, lettre rédigée, à envoyer      -> propose
#   valide      lettre validée, kit envoyé, candidature à faire
#   postule     candidature envoyée (email) ou confirmée
#   ignore      écartée par l'utilisateur (motif dans reason)
#   erreur      échec de notation ou de rédaction
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE,
    dedupe_key TEXT,
    source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
    description TEXT, salary_text TEXT, contract TEXT, apply_email TEXT,
    posted_at TEXT, found_at TEXT, updated_at TEXT,
    status TEXT, reason TEXT,
    score INTEGER, analysis TEXT, letter TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Le bot Telegram appelle la base depuis plusieurs threads.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [_decode(r) for r in self.conn.execute(sql, params).fetchall()]

    def is_known(self, job: Job) -> bool:
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM jobs WHERE uid = ? OR dedupe_key = ?", (job.uid, job.dedupe_key)
            ).fetchone() is not None

    def add(self, job: Job, status: str, reason: str | None = None) -> None:
        self._exec(
            """INSERT OR IGNORE INTO jobs (uid, dedupe_key, source, title, company, location, url,
               description, salary_text, contract, apply_email, posted_at, found_at, updated_at,
               status, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.uid, job.dedupe_key, job.source, job.title, job.company, job.location, job.url,
             job.description, job.salary_text, job.contract, job.apply_email,
             job.posted_at.isoformat() if job.posted_at else None, _now(), _now(), status, reason),
        )

    def get(self, job_id: int) -> dict | None:
        rows = self._rows("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return rows[0] if rows else None

    def set_status(self, job_id: int, status: str, reason: str | None = None) -> None:
        self._exec("UPDATE jobs SET status = ?, reason = COALESCE(?, reason), updated_at = ? WHERE id = ?",
                   (status, reason, _now(), job_id))

    def save_analysis(self, job_id: int, analysis: dict, apply_email: str) -> None:
        self._exec("UPDATE jobs SET score = ?, analysis = ?, apply_email = ?, updated_at = ? WHERE id = ?",
                   (analysis["score"], json.dumps(analysis, ensure_ascii=False), apply_email, _now(), job_id))

    def save_letter(self, job_id: int, letter: dict) -> None:
        self._exec("UPDATE jobs SET letter = ?, updated_at = ? WHERE id = ?",
                   (json.dumps(letter, ensure_ascii=False), _now(), job_id))

    def update_description(self, job_id: int, description: str) -> None:
        self._exec("UPDATE jobs SET description = ?, updated_at = ? WHERE id = ?",
                   (description, _now(), job_id))

    def enrich(self, job: Job) -> None:
        """Une offre déjà vue ailleurs sans description (alerte LinkedIn) récupère
        la description complète venue d'une autre source."""
        if len(job.description) < 500:
            return
        self._exec("""UPDATE jobs SET description = ?, updated_at = ?
                      WHERE dedupe_key = ? AND uid != ? AND LENGTH(COALESCE(description, '')) < 500""",
                   (job.description, _now(), job.dedupe_key, job.uid))

    def by_status(self, status: str, limit: int = 100) -> list[dict]:
        return self._rows(
            "SELECT * FROM jobs WHERE status = ? ORDER BY posted_at DESC, id DESC LIMIT ?", (status, limit))

    def recent_rejections(self, limit: int = 15) -> list[dict]:
        return self._rows(
            "SELECT title, company, reason FROM jobs WHERE status = 'ignore' AND reason IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT ?", (limit,))

    def counts(self) -> dict[str, int]:
        with self.lock:
            return dict(self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    def get_state(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))


def _decode(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("analysis", "letter"):
        if data.get(key):
            data[key] = json.loads(data[key])
    return data
