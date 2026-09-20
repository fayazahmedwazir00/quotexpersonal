"""
database.py — SQLite session + history storage
"""
import sqlite3
import json
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


def _conn():
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            email      TEXT,
            mode       TEXT DEFAULT 'PRACTICE',
            created_at INTEGER,
            last_used  INTEGER
        );

        CREATE TABLE IF NOT EXISTS history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT,
            timeframe    INTEGER,
            decision     TEXT,
            confidence   REAL,
            votes        TEXT,
            market_data  TEXT,
            created_at   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_history_created
            ON history (created_at DESC);
    """)
    con.commit()
    con.close()


def save_session(session_id: str, email: str, mode: str = "PRACTICE"):
    con = _conn()
    now = int(time.time())
    con.execute("""
        INSERT OR REPLACE INTO sessions (session_id, email, mode, created_at, last_used)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, email, mode, now, now))
    con.commit()
    con.close()


def update_session_mode(session_id: str, mode: str):
    con = _conn()
    con.execute("UPDATE sessions SET mode=?, last_used=? WHERE session_id=?",
                (mode, int(time.time()), session_id))
    con.commit()
    con.close()


def touch_session(session_id: str):
    con = _conn()
    con.execute("UPDATE sessions SET last_used=? WHERE session_id=?",
                (int(time.time()), session_id))
    con.commit()
    con.close()


def save_history(symbol, timeframe, decision, confidence, votes, market_data):
    con = _conn()
    con.execute("""
        INSERT INTO history (symbol, timeframe, decision, confidence, votes, market_data, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (symbol, timeframe, decision, confidence,
          json.dumps(votes), json.dumps(market_data), int(time.time())))
    con.commit()
    con.close()


def get_history(limit: int = 50):
    con = _conn()
    rows = con.execute("""
        SELECT * FROM history ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_session(session_id: str):
    con = _conn()
    row = con.execute("SELECT * FROM sessions WHERE session_id=?",
                      (session_id,)).fetchone()
    con.close()
    return dict(row) if row else None