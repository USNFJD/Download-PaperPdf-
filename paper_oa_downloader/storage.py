from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    doi TEXT,
    title TEXT NOT NULL,
    authors TEXT,
    abstract TEXT,
    publication_date TEXT,
    journal TEXT,
    publisher TEXT,
    sci_quartile TEXT,
    pdf_url TEXT,
    pdf_path TEXT,
    source_url TEXT,
    relevance_score REAL,
    query TEXT,
    downloaded_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_papers_downloaded_at ON papers(downloaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
"""


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_paper(self, paper: dict[str, Any]) -> None:
        columns = [
            "id",
            "doi",
            "title",
            "authors",
            "abstract",
            "publication_date",
            "journal",
            "publisher",
            "sci_quartile",
            "pdf_url",
            "pdf_path",
            "source_url",
            "relevance_score",
            "query",
        ]
        payload = {key: paper.get(key) for key in columns}
        if isinstance(payload["authors"], list):
            payload["authors"] = json.dumps(payload["authors"], ensure_ascii=False)
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
        sql = f"""
            INSERT INTO papers ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {assignments}, downloaded_at=CURRENT_TIMESTAMP
        """
        with self.connect() as conn:
            conn.execute(sql, [payload[column] for column in columns])

    def list_papers(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM papers ORDER BY downloaded_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["authors"] = json.loads(item.get("authors") or "[]")
            except json.JSONDecodeError:
                item["authors"] = []
            result.append(item)
        return result
