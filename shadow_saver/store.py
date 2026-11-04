import json
import secrets
import sqlite3
from pathlib import Path


class QueryStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS queries (
                id TEXT PRIMARY KEY, msg_id TEXT UNIQUE, input_type TEXT NOT NULL,
                input_value TEXT NOT NULL, source_platform TEXT,
                status TEXT NOT NULL, search_query TEXT,
                offers_json TEXT NOT NULL DEFAULT '[]'
            )""")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, msg_id: str, input_type: str, input_value: str, platform: str | None) -> str:
        query_id = secrets.token_urlsafe(24)
        status = "awaiting_vision" if input_type == "image" else "awaiting_provider"
        search_query = input_value if input_type == "text" else None
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO queries
                (id, msg_id, input_type, input_value, source_platform, status, search_query)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (query_id, msg_id, input_type, input_value, platform, status, search_query))
            row = db.execute("SELECT id FROM queries WHERE msg_id = ?", (msg_id,)).fetchone()
        return str(row["id"])

    def get(self, query_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM queries WHERE id = ?", (query_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "input_type": row["input_type"],
            "source_platform": row["source_platform"],
            "status": row["status"],
            "search_query": row["search_query"],
            "offers": json.loads(row["offers_json"]),
        }

    def set_search_query(self, query_id: str, search_query: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE queries SET search_query = ?, status = 'awaiting_provider' WHERE id = ?",
                (search_query, query_id),
            )

    def set_failed(self, query_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE queries SET status = 'failed' WHERE id = ?", (query_id,))

    def set_unavailable(self, query_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE queries SET status = 'unavailable' WHERE id = ?", (query_id,))

    def set_offers(self, query_id: str, offers: list[dict]) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE queries SET offers_json = ?, status = 'ready' WHERE id = ?",
                (json.dumps(offers, ensure_ascii=False), query_id),
            )


