"""
SQLite-backed fact store with entity resolution and trust scoring.
Single-user Hermes memory store plugin.
"""

import re
import sqlite3
import threading
from pathlib import Path

try:
    from . import holographic as hrr
except ImportError:
    import holographic as hrr  # type: ignore[no-redef]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    title           TEXT DEFAULT '',
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_relations (
    relation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entity  INTEGER REFERENCES entities(entity_id),
    relation       TEXT NOT NULL,
    target_entity  INTEGER REFERENCES entities(entity_id),
    source_label   TEXT DEFAULT '',
    target_label   TEXT DEFAULT '',
    fact_id        INTEGER DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_entity, relation, target_entity, fact_id)
);
"""

# Trust adjustment constants
_HELPFUL_DELTA   =  0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN       =  0.0
_TRUST_MAX       =  1.0

# Entity extraction patterns
_RE_CAPITALIZED  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA          = re.compile(
    r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)',
    re.IGNORECASE,
)


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring."""

    # --- Process-wide shared connection registry -------------------------
    # SQLite permits only one writer at a time. Each MemoryStore instance used
    # to open its own connection guarded by its own RLock, so the several
    # providers that coexist in one process (the main agent plus every
    # delegate_task subagent) raced as independent WAL writers. Combined with
    # writes that were not rolled back on error, one connection could leave an
    # open write transaction that pinned the write lock and made every other
    # connection's write fail with "database is locked" for the full busy
    # timeout. All instances for the same database now share ONE connection and
    # ONE re-entrant lock, so access is fully serialized and cross-connection
    # contention is impossible. The shared connection is refcounted, so closing
    # one instance never tears the connection out from under a live sibling.
    _shared: dict = {}
    _shared_guard = threading.Lock()

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        default_trust: float = 0.5,
        hrr_dim: int = 1024,
    ) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self.hrr_dim = hrr_dim
        self._hrr_available = hrr._HAS_NUMPY

        # Acquire (or open) the process-wide shared connection for this DB.
        # resolve() (not just expanduser) so symlinked/relative paths to the
        # same file share ONE connection instead of silently reintroducing
        # the multi-writer contention this registry exists to prevent.
        try:
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                conn = sqlite3.connect(
                    self._key,
                    check_same_thread=False,
                    timeout=10.0,
                    # Autocommit: every statement is its own transaction, so a
                    # write that raises mid-method can never leave a dangling
                    # transaction (and its write lock) open. The explicit
                    # commit() calls below become harmless no-ops.
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                entry = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False}
                MemoryStore._shared[self._key] = entry
            entry["refs"] += 1
            self._entry = entry
            self._conn = entry["conn"]
            self._lock = entry["lock"]

        # Initialise the schema once per shared connection.
        with self._lock:
            if not self._entry["ready"]:
                self._init_db()
                self._entry["ready"] = True

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables, indexes, and triggers if they do not exist. Enable WAL mode."""
        # Use the shared WAL-fallback helper so memory_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same issue as
        # state.db / kanban.db — see hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (holographic)")
        self._conn.executescript(_SCHEMA)
        # Migrate: add hrr_vector column if missing (safe for existing databases)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "hrr_vector" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")
        # Migrate: add title column if missing (Seraph: Trilium title/content split)
        if "title" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN title TEXT DEFAULT ''")
        # Migrate: add description column to entities if missing
        ecols = {row[1] for row in self._conn.execute("PRAGMA table_info(entities)").fetchall()}
        if "description" not in ecols:
            self._conn.execute("ALTER TABLE entities ADD COLUMN description TEXT DEFAULT ''")
        # Migrate: add reviewed column to entities (self_heal incremental review)
        # reviewed=1 → 已审查确认，self_heal 跳过；实体关联变化 → _touch 清 0
        if "reviewed" not in ecols:
            self._conn.execute("ALTER TABLE entities ADD COLUMN reviewed INTEGER DEFAULT 0")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_fact(
        self,
        content: str,
        category: str = "general",
        tags: str = "",
        title: str = "",
    ) -> int:
        """Insert a fact and return its fact_id.

        Deduplicates by content (UNIQUE constraint). On duplicate, returns
        the existing fact_id without modifying the row. Extracts entities from
        the content and links them to the fact.

        title: optional short title for the fact (used as Trilium note title).
        When empty, auto-generates a summary from content (first 40 chars).
        """
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")

            if not title:
                title = self._auto_title(content)

            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO facts (content, title, category, tags, trust_score)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (content, title, category, tags, self.default_trust),
                )
                self._conn.commit()
                fact_id: int = cur.lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                # Duplicate content — return existing id
                row = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ?", (content,)
                ).fetchone()
                return int(row["fact_id"])

            # Entity extraction and linking
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)

            # Compute HRR vector after entity linking
            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category)

            return fact_id

    def search_facts(
        self,
        query: str,
        category: str | None = None,
        min_trust: float = 0.3,
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search over facts using FTS5.

        Returns a list of fact dicts ordered by FTS5 rank, then trust_score
        descending. Also increments retrieval_count for matched facts.
        """
        with self._lock:
            query = query.strip()
            if not query:
                return []

            # FTS5 AND-joins tokens by default, which zeroes out recall on
            # natural-language queries. Reuse the retriever's sanitizer
            # (stopword drop + OR-join content tokens). Imported lazily to
            # avoid a store->retrieval import cycle.
            from plugins.memory.holographic.retrieval import FactRetriever

            match_query = FactRetriever._sanitize_fts_query(query)
            params: list = [match_query, min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND f.category = ?"
                params.append(category)
            params.append(limit)

            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ?
                  AND f.trust_score >= ?
                  {category_clause}
                ORDER BY fts.rank, f.trust_score DESC
                LIMIT ?
            """

            rows = self._conn.execute(sql, params).fetchall()
            results = [self._row_to_dict(r) for r in rows]

            if results:
                ids = [r["fact_id"] for r in results]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
                    ids,
                )
                self._conn.commit()

            return results

    def update_fact(
        self,
        fact_id: int,
        content: str | None = None,
        trust_delta: float | None = None,
        tags: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Partially update a fact. Trust is clamped to [0, 1].

        Returns True if the row existed, False otherwise.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            assignments: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
            params: list = []

            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)

            params.append(fact_id)
            self._conn.execute(
                f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?",
                params,
            )
            self._conn.commit()

            # If content changed, re-extract entities
            if content is not None:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
                touched: list[int] = []
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, entity_id)
                    touched.append(entity_id)
                if touched:
                    self._touch(touched)  # 内容变 → 关联实体重审
                self._conn.commit()

            # Recompute HRR vector if content changed
            if content is not None:
                self._compute_hrr_vector(fact_id, content)
            # Rebuild bank for relevant category
            cat = category or self._conn.execute(
                "SELECT category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()["category"]
            self._rebuild_bank(cat)

            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links. Returns True if the row existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            # 删除前收集关联实体 → 清 reviewed（引用变了需重审）
            linked = [
                r[0] for r in self._conn.execute(
                    "SELECT entity_id FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
            ]
            self._conn.execute(
                "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
            )
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            if linked:
                self._touch(linked)
            self._conn.commit()
            self._rebuild_bank(row["category"])
            return True

    def remove_entity(self, entity_id: int) -> bool:
        """Delete an entity and all its links (fact_entities + entity_relations).

        Returns True if the row existed. Cascade-safe: linking rows are removed
        first so no orphan references remain.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT entity_id FROM entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "DELETE FROM fact_entities WHERE entity_id = ?", (entity_id,)
            )
            self._conn.execute(
                "DELETE FROM entity_relations WHERE source_entity = ? OR target_entity = ?",
                (entity_id, entity_id),
            )
            self._conn.execute(
                "DELETE FROM entities WHERE entity_id = ?", (entity_id,)
            )
            self._conn.commit()
            return True

    def update_entity_type(self, entity_id: int, entity_type: str) -> bool:
        """Update an entity's type (e.g. 'unknown' -> 'resource').

        Returns True if the row existed. Used to correct LLM extraction
        mistakes (e.g. an IP entity typed 'unknown' should be 'resource').
        """
        valid_types = {
            "person", "server", "device", "domain", "service", "project",
            "repo", "tool", "resource", "company", "system", "user",
            "package", "account", "file", "script", "format",
        }
        et = (entity_type or "").strip().lower()
        if et not in valid_types:
            raise ValueError(f"invalid entity_type '{entity_type}'; valid: {sorted(valid_types)}")
        with self._lock:
            row = self._conn.execute(
                "SELECT entity_id FROM entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE entities SET entity_type = ? WHERE entity_id = ?",
                (et, entity_id),
            )
            self._touch([entity_id])  # 类型变 → 清 reviewed 重审
            self._conn.commit()
            return True

    def purge_orphan_entities(self, entity_type: str | None = None) -> int:
        """Delete entities with NO fact_entities and NO entity_relations links.

        Optionally restrict to one entity_type (e.g. 'unknown'). Returns the
        number of entities deleted. Used for cleaning LLM-extraction noise.
        IP-address entities are NEVER purged (user preference: IPs are valid
        resources, even when currently unreferenced) — consistent with
        self_heal's orphan logic.
        """
        with self._lock:
            params: list = []
            type_clause = ""
            if entity_type:
                type_clause = " AND e.entity_type = ?"
                params.append(entity_type)
            rows = self._conn.execute(
                f"""
                SELECT e.entity_id FROM entities e
                WHERE NOT EXISTS (
                    SELECT 1 FROM fact_entities fe WHERE fe.entity_id = e.entity_id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM entity_relations er
                    WHERE er.source_entity = e.entity_id OR er.target_entity = e.entity_id
                )
                -- IP addresses are valid entities: never auto-purge
                AND NOT (e.name GLOB '*.*.*.*'
                         AND e.name NOT GLOB '*[a-zA-Z]*')
                {type_clause}
                """,
                params,
            ).fetchall()
            ids = [r["entity_id"] for r in rows]
            for eid in ids:
                self._conn.execute(
                    "DELETE FROM entities WHERE entity_id = ?", (eid,)
                )
            self._conn.commit()
            return len(ids)

    def list_facts(
        self,
        category: str | None = None,
        min_trust: float = 0.0,
        limit: int = 50,
    ) -> list[dict]:
        """Browse facts ordered by trust_score descending.

        Optionally filter by category and minimum trust score.
        """
        with self._lock:
            params: list = [min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND category = ?"
                params.append(category)
            params.append(limit)

            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at
                FROM facts
                WHERE trust_score >= ?
                  {category_clause}
                ORDER BY trust_score DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Record user feedback and adjust trust asymmetrically.

        Diminishing-returns schedule (trust never saturates at 1.0):

        helpful=True  -> trust += 0.05 * (1 - old_trust), helpful_count += 1
        helpful=False -> trust -= 0.10 * old_trust

        Low-trust facts climb quickly; high-trust facts need many
        confirmations to move further; a high-trust fact that is
        contradicted drops hard. Trust is clamped to [0, 1] and
        asymptotically approaches 1.0 without reaching it.

        Returns a dict with fact_id, old_trust, new_trust, helpful_count.
        Raises KeyError if fact_id does not exist.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")

            old_trust: float = row["trust_score"]
            if helpful:
                delta = _HELPFUL_DELTA * (1.0 - old_trust)
            else:
                delta = _UNHELPFUL_DELTA * old_trust  # -0.10 * old_trust
            new_trust = _clamp_trust(old_trust + delta)

            helpful_increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score    = ?,
                    helpful_count  = helpful_count + ?,
                    updated_at     = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, helpful_increment, fact_id),
            )
            self._conn.commit()

            return {
                "fact_id":      fact_id,
                "old_trust":    old_trust,
                "new_trust":    new_trust,
                "helpful_count": row["helpful_count"] + helpful_increment,
            }

    # ------------------------------------------------------------------
    # Entity helpers
    # ------------------------------------------------------------------

    def _auto_title(self, content: str, max_len: int = 40) -> str:
        """Generate a short title from content (strip HTML, first sentence)."""
        text = content.strip()
        # Strip HTML tags
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        # First sentence (。！？.!? or first N chars)
        for sep in ("。", "！", "？", ". ", "! ", "? "):
            idx = text.find(sep)
            if 0 < idx <= max_len:
                text = text[: idx + (1 if sep in "。！？" else 0)]
                break
        text = text.strip()
        if len(text) > max_len:
            text = text[:max_len]
        return text or content[:max_len]

    def _extract_entities(self, text: str) -> list[str]:
        """Extract entity candidates from text.

        Rules applied (in order):
        1. Capitalized multi-word phrases  e.g. "John Doe"
        2. Double-quoted terms             e.g. "Python"
        3. Single-quoted terms             e.g. 'pytest'
        4. AKA patterns                    e.g. "Guido aka BDFL" -> two entities
        5. Known entities from the entities table (covers lowercase/Chinese/domain names)

        Returns a deduplicated list preserving first-seen order.
        """
        seen: set[str] = set()
        candidates: list[str] = []

        def _add(name: str) -> None:
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))

        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))

        # Rule 5: match against known entities (lowercase/Chinese/domain names)
        try:
            known = self._conn.execute(
                "SELECT name FROM entities"
            ).fetchall()
            for row in known:
                ename = row["name"]
                if ename and len(ename) >= 2 and ename in text:
                    _add(ename)
        except Exception:
            pass

        return candidates

    def _touch(self, entity_ids: list[int]) -> None:
        """Clear 'reviewed' flag on entities (they changed → re-review needed).

        Called on every write path that can change an entity's meaning:
        linking a fact, adding relations, changing type. self_heal only
        reviews entities with reviewed=0, so a touched entity gets re-judged
        on the next run. Idempotent and cheap.
        """
        ids = [i for i in entity_ids if i]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self._conn.execute(
            f"UPDATE entities SET reviewed = 0 WHERE entity_id IN ({marks})",
            ids,
        )

    def mark_reviewed(self, entity_id: int, reviewed: bool) -> bool:
        """Set an entity's reviewed flag (1=已审查确认, 0=待审查).

        Used after human/agent confirmation of a self_heal needs_review item,
        or by self_heal itself when LLM judges an entity as keep.
        Returns True if the row existed.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT entity_id FROM entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE entities SET reviewed = ? WHERE entity_id = ?",
                (1 if reviewed else 0, entity_id),
            )
            self._conn.commit()
            return True

    def _link_entities(self, fact_id: int, entities: list) -> None:
        """Resolve and link entities to a fact (idempotent).

        entities: list of str (name) or (name, type) tuples.
        """
        touched: list[int] = []
        fresh: list[int] = []  # entities created by this call (need description)
        for item in entities:
            if isinstance(item, tuple) and len(item) >= 1:
                name, etype = item[0], (item[1] if len(item) > 1 else "")
            else:
                name, etype = item, ""
            exists = self._conn.execute(
                "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
            ).fetchone()
            entity_id = self._resolve_entity(name, etype)
            self._link_fact_entity(fact_id, entity_id)
            touched.append(entity_id)
            if exists is None:
                fresh.append(entity_id)
        if touched:
            self._touch(touched)
            self._conn.commit()
        # 自动画像：仅对本次新建的实体生成描述（存量空描述实体由 batch_describe 统一补）
        if fresh:
            self.ensure_entity_descriptions(fresh)

    def add_relations(self, triplets: list[tuple[str, str, str]], fact_id: int = 0) -> int:
        """Store (source, relation, target) triplets, resolving entity ids.

        Returns the number of relations stored.
        """
        added = 0
        touched: list[int] = []
        fresh: list[int] = []  # entities created by this call (need description)
        with self._lock:
            for source, relation, target in triplets:
                src_exists = self._conn.execute(
                    "SELECT entity_id FROM entities WHERE name LIKE ?", (source,)
                ).fetchone()
                tgt_exists = self._conn.execute(
                    "SELECT entity_id FROM entities WHERE name LIKE ?", (target,)
                ).fetchone()
                src_id = self._resolve_entity(source)
                tgt_id = self._resolve_entity(target)
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO entity_relations
                        (source_entity, relation, target_entity, source_label, target_label, fact_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (src_id, relation, tgt_id, source, target, fact_id),
                )
                if cur.rowcount:
                    added += 1
                    touched.extend([src_id, tgt_id])
                if src_exists is None:
                    fresh.append(src_id)
                if tgt_exists is None:
                    fresh.append(tgt_id)
            if touched:
                self._touch(touched)
            self._conn.commit()
        # 自动画像：仅对本次新建的实体生成描述
        if fresh:
            self.ensure_entity_descriptions(list(dict.fromkeys(fresh)))
        return added

    def update_entity_description(self, entity_id: int, description: str) -> None:
        """Store an LLM-generated description for an entity (idempotent)."""
        with self._lock:
            self._conn.execute(
                "UPDATE entities SET description=? WHERE entity_id=?",
                (description, entity_id),
            )
            self._conn.commit()

    def ensure_entity_descriptions(self, entity_ids: list[int]) -> None:
        """Generate LLM descriptions for entities that currently have none.

        Called after linking entities/relations so newly-created entities
        automatically get a profile. Skips entities that already have a
        description or that have no facts/relations to aggregate. Any LLM
        failure is silently ignored (existing description is kept).
        """
        if not entity_ids:
            return
        try:
            from .llm_extract import generate_entity_description
        except Exception:
            try:
                from llm_extract import generate_entity_description
            except Exception:
                return
        with self._lock:
            for eid in entity_ids:
                row = self._conn.execute(
                    "SELECT name, description FROM entities WHERE entity_id=?",
                    (eid,),
                ).fetchone()
                if not row:
                    continue
                name, desc = row["name"], row["description"] or ""
                if desc.strip():
                    continue
                facts = self.get_entity_facts(eid)
                relations = self.get_entity_relations(eid)
                if not facts and not relations:
                    continue
                try:
                    new_desc = generate_entity_description(name, facts, relations)
                except Exception:
                    continue
                if new_desc:
                    self._conn.execute(
                        "UPDATE entities SET description=? WHERE entity_id=?",
                        (new_desc, eid),
                    )
                    self._conn.commit()

    def get_entity_facts(self, entity_id: int) -> list[str]:
        """Return all fact contents linked to an entity."""
        rows = self._conn.execute(
            """
            SELECT f.content FROM facts f
            JOIN fact_entities fe ON f.fact_id = fe.fact_id
            WHERE fe.entity_id = ?
            """,
            (entity_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_entity_relations(self, entity_id: int) -> list[tuple[str, str, str]]:
        """Return (source_label, relation, target_label) for an entity (as source or target)."""
        rows = self._conn.execute(
            """
            SELECT source_label, relation, target_label FROM entity_relations
            WHERE source_entity = ? OR target_entity = ?
            """,
            (entity_id, entity_id),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def _resolve_entity(self, name: str, entity_type: str = "") -> int:
        """Find an existing entity by name or alias (case-insensitive) or create one.

        entity_type: optional type for newly created entities (e.g. "service").
        Existing entities are never re-typed (preserves curated types).

        Returns the entity_id.
        """
        # Exact name match
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])

        # Search aliases — aliases stored as comma-separated; use LIKE with % boundaries
        alias_row = self._conn.execute(
            """
            SELECT entity_id FROM entities
            WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'
            """,
            (name,),
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])

        # Create new entity (with type if provided)
        cur = self._conn.execute(
            "INSERT INTO entities (name, entity_type) VALUES (?, ?)",
            (name, entity_type or "unknown"),
        )
        self._conn.commit()
        return int(cur.lastrowid)  # type: ignore[return-value]

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        """Insert into fact_entities, silently ignore if the link already exists."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO fact_entities (fact_id, entity_id)
            VALUES (?, ?)
            """,
            (fact_id, entity_id),
        )
        self._conn.commit()

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store HRR vector for a fact. No-op if numpy unavailable."""
        with self._lock:
            if not self._hrr_available:
                return

            # Get entities linked to this fact
            rows = self._conn.execute(
                """
                SELECT e.name FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.entity_id
                WHERE fe.fact_id = ?
                """,
                (fact_id,),
            ).fetchall()
            entities = [row["name"] for row in rows]

            vector = hrr.encode_fact(content, entities, self.hrr_dim)
            self._conn.execute(
                "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
                (hrr.phases_to_bytes(vector), fact_id),
            )
            self._conn.commit()

    def _rebuild_bank(self, category: str) -> None:
        """Full rebuild of a category's memory bank from all its fact vectors."""
        with self._lock:
            if not self._hrr_available:
                return

            bank_name = f"cat:{category}"
            rows = self._conn.execute(
                "SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL",
                (category,),
            ).fetchall()

            if not rows:
                self._conn.execute("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
                self._conn.commit()
                return

            vectors = [hrr.bytes_to_phases(row["hrr_vector"], dim=self.hrr_dim) for row in rows]
            bank_vector = hrr.bundle(*vectors)
            fact_count = len(vectors)

            # Check SNR
            hrr.snr_estimate(self.hrr_dim, fact_count)

            self._conn.execute(
                """
                INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bank_name) DO UPDATE SET
                    vector = excluded.vector,
                    dim = excluded.dim,
                    fact_count = excluded.fact_count,
                    updated_at = excluded.updated_at
                """,
                (bank_name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, fact_count),
            )
            self._conn.commit()

    def rebuild_all_vectors(self, dim: int | None = None) -> int:
        """Recompute all HRR vectors + banks from text. For recovery/migration.

        Returns the number of facts processed.
        """
        with self._lock:
            if not self._hrr_available:
                return 0

            if dim is not None:
                self.hrr_dim = dim

            rows = self._conn.execute(
                "SELECT fact_id, content, category FROM facts"
            ).fetchall()

            categories: set[str] = set()
            for row in rows:
                self._compute_hrr_vector(row["fact_id"], row["content"])
                categories.add(row["category"])

            for category in categories:
                self._rebuild_bank(category)

            return len(rows)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        return dict(row)

    def close(self) -> None:
        """Release this instance's reference to the shared connection.

        The underlying connection is closed only when the last MemoryStore
        referencing the same database is closed, so closing one instance can
        never break sibling instances that still hold it. Idempotent.
        """
        if getattr(self, "_entry", None) is None:
            return
        with MemoryStore._shared_guard:
            entry = self._entry
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
