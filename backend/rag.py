"""Simple local RAG store using SQLite FTS5.

This keeps deployment lightweight (no external vector DB service) while still
providing retrieval grounding from project docs/knowledge files. Chunking is
heading-aware: markdown heading lines start new sections, long sections are
further split with overlap. The ``source`` column doubles as the role key for
per-role knowledge (``maruti`` / ``sales_1`` / ``sales_2``).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,}")


def _fts_terms(text: str, max_terms: int = 24) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for tok in _TOKEN_RE.findall(text or ""):
        t = tok.lower()
        if t in seen:
            continue
        seen.add(t)
        terms.append(t)
        if len(terms) >= max_terms:
            break
    return terms


def _split_heading_sections(text: str) -> list[str]:
    """Split text into sections, each beginning at a markdown heading line.

    Lines starting with ``#`` open a new section; the heading stays attached to
    the content that follows it. Consecutive heading lines (e.g. ``# 2.``
    directly above ``## 2A.``) merge into one heading stack so parent context is
    kept with the subsection. Text before the first heading forms its own
    leading section.
    """
    sections: list[list[str]] = []
    cur: list[str] = []
    for line in (text or "").split("\n"):
        if line.startswith("#"):
            if cur and not all(l.startswith("#") for l in cur):
                sections.append(cur)
                cur = [line]
            else:
                cur.append(line)
        else:
            cur.append(line)
    if cur:
        sections.append(cur)
    out: list[str] = []
    for sec in sections:
        s = "\n".join(sec).strip()
        if s:
            out.append(s)
    return out


def _split_long_section(section: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    """Split one section into fixed-size chunks, repeating its heading prefix.

    Consecutive leading heading lines (e.g. ``## 8A.`` under ``# 8.``) are
    treated as the section title and prepended to every sub-chunk so retrieved
    chunks keep their heading context.
    """
    lines = section.split("\n")
    heading_lines: list[str] = []
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.startswith("#"):
            heading_lines.append(ln)
            body_start = i + 1
        else:
            break
    heading = "\n".join(heading_lines).strip()
    body = "\n".join(lines[body_start:]).strip()
    if not body:
        return [section.strip()]
    if len(section.strip()) <= chunk_chars:
        return [section.strip()]

    prefix = (heading + "\n") if heading else ""
    budget = max(160, chunk_chars - len(prefix) - 1)
    chunks: list[str] = []
    start = 0
    n = len(body)
    while start < n:
        end = min(start + budget, n)
        part = body[start:end].strip()
        if part:
            chunks.append((prefix + part).strip())
        if end >= n:
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def _chunk_text(text: str, chunk_chars: int = 900, overlap_chars: int = 180) -> list[str]:
    """Heading-aware chunking.

    First split on markdown headings (each heading + its following content is a
    section). Sections still larger than ``chunk_chars`` are further split into
    fixed-size chunks with ``overlap_chars`` overlap, keeping the heading
    prefix on every sub-chunk.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    if len(raw) <= chunk_chars:
        return [raw]

    chunks: list[str] = []
    for section in _split_heading_sections(raw):
        if len(section) <= chunk_chars:
            chunks.append(section)
        else:
            chunks.extend(_split_long_section(section, chunk_chars, overlap_chars))
    return chunks


class RagStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(text, content='chunks', content_rowid='id', tokenize='unicode61');
                """
            )
            # Rebuild fts table if needed.
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            conn.commit()

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks")
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            conn.commit()

    def count_role(self, role: str) -> int:
        """Number of indexed chunks for one role (``source`` column)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE source = ?", (role,)
            ).fetchone()
        return int(row["c"]) if row else 0

    def clear_role(self, role: str) -> None:
        """Delete every chunk belonging to one role/source."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE source = ?", (role,))
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            conn.commit()

    def add_document(self, source: str, text: str, *, chunk_chars: int = 900) -> int:
        chunks = _chunk_text(text, chunk_chars=chunk_chars)
        if not chunks:
            return 0
        with self._connect() as conn:
            rows = [(source, i, c) for i, c in enumerate(chunks)]
            conn.executemany(
                "INSERT INTO chunks(source, chunk_index, text) VALUES (?, ?, ?)",
                rows,
            )
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            conn.commit()
        return len(chunks)

    def build_role_index(self, role: str, text: str, chunk_chars: int = 900) -> int:
        """Replace a role's chunk index with fresh chunks of ``text``."""
        self.clear_role(role)
        if not (text or "").strip():
            return 0
        chunks = _chunk_text(text, chunk_chars=chunk_chars)
        if not chunks:
            return 0
        with self._connect() as conn:
            rows = [(role, i, c) for i, c in enumerate(chunks)]
            conn.executemany(
                "INSERT INTO chunks(source, chunk_index, text) VALUES (?, ?, ?)",
                rows,
            )
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            conn.commit()
        return len(chunks)

    def build_from_files(self, files: Iterable[Path], *, chunk_chars: int = 900) -> int:
        total = 0
        self.clear()
        for path in files:
            try:
                txt = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            src = str(path)
            total += self.add_document(src, txt, chunk_chars=chunk_chars)
        return total

    def query(
        self,
        text: str,
        *,
        top_k: int = 4,
        max_chars: int = 2200,
        source: str | None = None,
    ) -> list[dict[str, str]]:
        """Keyword/FTS retrieval; ``source`` optionally restricts to one role."""
        terms = _fts_terms(text)
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms)
        sql = """
            SELECT c.source, c.text, bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
        """
        params: list = [fts_query]
        if source:
            sql += " AND c.source = ?"
            params.append(source)
        sql += " ORDER BY score LIMIT ?"
        params.append(int(top_k))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        out: list[dict[str, str]] = []
        used = 0
        for r in rows:
            snippet = str(r["text"]).strip()
            if not snippet:
                continue
            remain = max_chars - used
            if remain <= 0:
                break
            if len(snippet) > remain:
                snippet = snippet[: max(0, remain - 1)].rstrip() + "…"
            out.append({"source": str(r["source"]), "text": snippet})
            used += len(snippet)
        return out

    def query_role(
        self, role: str, text: str, top_k: int = 4, max_chars: int = 2200
    ) -> list[dict[str, str]]:
        """Top-k retrieval restricted to one role's chunks."""
        return self.query(text, top_k=top_k, max_chars=max_chars, source=role)


def format_references(items: list[dict[str, str]]) -> str:
    if not items:
        return ""
    lines: list[str] = []
    for i, item in enumerate(items, start=1):
        src = Path(item.get("source", "")).name or item.get("source", "unknown")
        txt = item.get("text", "").strip()
        if not txt:
            continue
        lines.append(f"[{i}] source={src}\n{txt}")
    return "\n\n".join(lines).strip()


# ── Module-level conveniences (lazy config import to avoid circulars) ──

_rag_store: RagStore | None = None


def get_rag_store() -> RagStore:
    """Lazily instantiate the shared RagStore from config.settings.rag_db_path."""
    global _rag_store
    if _rag_store is None:
        from config import settings

        _rag_store = RagStore(settings.rag_db_path)
    return _rag_store


def index_role_rag(role: str, text: str, chunk_chars: int = 900) -> int:
    """Rebuild the chunk index for one role. Returns number of chunks stored."""
    return get_rag_store().build_role_index(role, text, chunk_chars=chunk_chars)


def rag_context_for_role(role: str, query_text: str) -> str:
    """Top-k RAG context for one role as formatted reference text.

    Returns ``""`` when RAG is disabled, the query is empty, or nothing matches.
    Never raises — callers embed this directly into the system instruction.
    """
    try:
        from config import settings

        if not getattr(settings, "rag_enabled", True):
            return ""
        top_k = int(getattr(settings, "rag_top_k", 4))
        max_chars = int(getattr(settings, "rag_max_context_chars", 3600))
    except Exception:
        top_k, max_chars = 4, 3600
    if not (query_text or "").strip():
        return ""
    try:
        items = get_rag_store().query_role(
            role, query_text, top_k=top_k, max_chars=max_chars
        )
    except Exception:
        return ""
    return format_references(items)


def query_role_chunks(role: str, query_text: str) -> list[dict[str, str]]:
    """Raw top-k chunk list for one role (UI preview / debugging).

    Never raises; returns ``[]`` when RAG is disabled or nothing matches.
    """
    try:
        from config import settings

        if not getattr(settings, "rag_enabled", True):
            return []
        top_k = int(getattr(settings, "rag_top_k", 4))
        max_chars = int(getattr(settings, "rag_max_context_chars", 3600))
    except Exception:
        top_k, max_chars = 4, 3600
    try:
        return get_rag_store().query_role(
            role, query_text or "", top_k=top_k, max_chars=max_chars
        )
    except Exception:
        return []


def role_chunk_count(role: str) -> int:
    """Number of chunks currently indexed for one role. Never raises."""
    try:
        return get_rag_store().count_role(role)
    except Exception:
        return 0
