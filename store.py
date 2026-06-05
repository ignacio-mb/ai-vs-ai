"""SQLite persistence for debate history.

One row per conversation. The full transcript is stored as a JSON blob (it's
only ever read/written whole), with a few flat columns for cheap listing. Each
function opens its own short-lived connection, which keeps things thread-safe
between the request threads and the background debate threads.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

DB_PATH = os.environ.get(
    "DEBATE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "debates.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    starter       TEXT,
    claude_model  TEXT,
    chatgpt_model TEXT,
    status        TEXT NOT NULL,   -- running | done | stopped | error
    reason        TEXT,            -- consensus | max_rounds | stopped
    messages      TEXT NOT NULL    -- JSON array of transcript entries
);

CREATE TABLE IF NOT EXISTS analytics (
    conversation_id TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    stats           TEXT NOT NULL  -- JSON blob from analytics.analyze()
);
"""


def _now() -> str:
    # Full precision (microseconds) so rows created in the same second still
    # sort deterministically by recency.
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Make sure the parent directory exists (e.g. a mounted disk at /var/data).
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def create_conversation(conv_id, topic, starter, claude_model, chatgpt_model):
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO conversations "
            "(id, topic, created_at, updated_at, starter, claude_model, "
            " chatgpt_model, status, reason, messages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', NULL, '[]')",
            (conv_id, topic, now, now, starter, claude_model, chatgpt_model),
        )


def save_transcript(conv_id, messages: List[Dict], status: str, reason: Optional[str]):
    """Overwrite the transcript and status for a conversation."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET messages = ?, status = ?, reason = ?, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(messages), status, reason, _now(), conv_id),
        )


def list_conversations(limit: int = 100) -> List[Dict]:
    """Lightweight listing for the history sidebar (no full transcript)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, topic, created_at, updated_at, status, reason, "
            "starter, claude_model, chatgpt_model, "
            "json_array_length(messages) AS message_count "
            "FROM conversations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["messages"] = json.loads(data["messages"] or "[]")
    return data


def delete_conversation(conv_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.execute("DELETE FROM analytics WHERE conversation_id = ?", (conv_id,))
    return cur.rowcount > 0


# ---------- analytics ----------

def save_analytics(conv_id: str, stats: Dict):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analytics (conversation_id, created_at, stats) "
            "VALUES (?, ?, ?)",
            (conv_id, _now(), json.dumps(stats)),
        )


def get_analytics(conv_id: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT stats FROM analytics WHERE conversation_id = ?", (conv_id,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["stats"])


def list_analytics(limit: int = 200) -> List[Dict]:
    """All analytics rows, newest first, as ``[{id, created_at, stats}]``."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT a.conversation_id AS id, a.created_at AS created_at, "
            "a.stats AS stats FROM analytics a "
            "JOIN conversations c ON c.id = a.conversation_id "
            "ORDER BY c.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "stats": json.loads(r["stats"]),
        })
    return out
