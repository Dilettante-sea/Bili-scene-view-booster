from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL,
  bvid TEXT NOT NULL,
  title TEXT, up TEXT, partition TEXT,
  tags_json TEXT, stats_json TEXT,
  source_query TEXT,
  screen TEXT, one_line TEXT, reason TEXT, hypothesis TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deep_tasks (
  id INTEGER PRIMARY KEY,
  bvid TEXT NOT NULL,
  batch_id TEXT,
  dsh_task_id TEXT,
  state TEXT NOT NULL DEFAULT 'pending',
  hypothesis TEXT,
  must_id TEXT,
  origin TEXT DEFAULT 'auto',
  wake_umo TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  deep_started_at TEXT, deep_finished_at TEXT,
  artifacts_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS musts (
  id TEXT PRIMARY KEY,
  bvid TEXT NOT NULL,
  memory_id INTEGER,
  one_line TEXT, why TEXT,
  push_text TEXT,
  source TEXT NOT NULL DEFAULT 'auto',
  origin TEXT,
  queue TEXT NOT NULL DEFAULT 'primary',
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  discussion_status TEXT,
  produced_at TEXT, pushed_at TEXT, classified_at TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS quota (
  date TEXT PRIMARY KEY,
  deep_used INTEGER NOT NULL DEFAULT 0,
  must_produced INTEGER NOT NULL DEFAULT 0,
  maybe_shared INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  bvid TEXT, must_id TEXT, memory_id INTEGER,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS admin_tags (
  tag TEXT PRIMARY KEY,
  search_query TEXT,
  base_weight REAL NOT NULL DEFAULT 1.0,
  locked INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  note TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS taste_tags (
  tag TEXT PRIMARY KEY,
  weight REAL NOT NULL DEFAULT 0,
  manual_delta REAL NOT NULL DEFAULT 0,
  n_eff REAL NOT NULL DEFAULT 0,
  sum_reward REAL NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT,
  last_signal_at TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS block_tags (
  tag TEXT PRIMARY KEY,
  reason TEXT,
  created_by TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS tag_aliases (
  alias TEXT PRIMARY KEY,
  tag TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tag_events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  tag TEXT NOT NULL,
  bvid TEXT,
  query_tag TEXT,
  signal TEXT,
  reward REAL,
  share REAL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS tag_audit (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT,
  action TEXT,
  owner TEXT,
  tag TEXT,
  before_json TEXT,
  after_json TEXT,
  bvid TEXT,
  must_id TEXT,
  reason TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path, audit_path: Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path = Path(audit_path) if audit_path else None
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def init(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(musts)")}
            if "push_text" not in cols:
                self.conn.execute("ALTER TABLE musts ADD COLUMN push_text TEXT")
            cand_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(candidates)")}
            if "source_query" not in cand_cols:
                self.conn.execute("ALTER TABLE candidates ADD COLUMN source_query TEXT")
            taste_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(taste_tags)")}
            if "manual_delta" not in taste_cols:
                self.conn.execute("ALTER TABLE taste_tags ADD COLUMN manual_delta REAL NOT NULL DEFAULT 0")
            must_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(musts)")}
            if "source" not in must_cols:
                self.conn.execute("ALTER TABLE musts ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'")
            if "origin" not in must_cols:
                self.conn.execute("ALTER TABLE musts ADD COLUMN origin TEXT")
            deep_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(deep_tasks)")}
            if "must_id" not in deep_cols:
                self.conn.execute("ALTER TABLE deep_tasks ADD COLUMN must_id TEXT")
            if "origin" not in deep_cols:
                self.conn.execute("ALTER TABLE deep_tasks ADD COLUMN origin TEXT DEFAULT 'auto'")
            if "wake_umo" not in deep_cols:
                self.conn.execute("ALTER TABLE deep_tasks ADD COLUMN wake_umo TEXT")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]

    def set_kv(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now_iso()),
            )
            self.conn.commit()

    def add_event(self, kind: str, **payload: Any) -> None:
        at = now_iso()
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(at,kind,bvid,must_id,memory_id,payload_json) VALUES(?,?,?,?,?,?)",
                (
                    at,
                    kind,
                    payload.get("bvid"),
                    payload.get("must_id"),
                    payload.get("memory_id"),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            self.conn.commit()
        if self.audit_path:
            try:
                line = json.dumps({"at": at, "kind": kind, **payload}, ensure_ascii=False, default=str)
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        if table not in {"candidates", "deep_tasks", "musts", "events", "quota"}:
            raise ValueError("unknown table")
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where:
            sql += " WHERE " + where
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0

    def recent_bvids(self, limit: int = 200) -> set[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT bvid FROM candidates UNION SELECT bvid FROM deep_tasks UNION SELECT bvid FROM musts"
            ).fetchall()
        return {str(r["bvid"]) for r in rows if r["bvid"]}

    def add_candidates(self, batch_id: str, candidates: list[dict]) -> None:
        with self._lock:
            for c in candidates:
                self.conn.execute(
                    "INSERT INTO candidates(batch_id,bvid,title,up,partition,tags_json,stats_json,source_query,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        c.get("bvid"),
                        c.get("title"),
                        c.get("up"),
                        c.get("partition"),
                        json.dumps(c.get("tags") or [], ensure_ascii=False),
                        json.dumps({"stat": c.get("stat"), "rates": c.get("rates")}, ensure_ascii=False),
                        c.get("source"),
                        now_iso(),
                    ),
                )
            self.conn.commit()

    def update_candidate_screen(self, batch_id: str, bvid: str, screen: str, one_line: str, reason: str, hypothesis: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE candidates SET screen=?, one_line=?, reason=?, hypothesis=? WHERE batch_id=? AND bvid=?",
                (screen, one_line, reason, hypothesis, batch_id, bvid),
            )
            self.conn.commit()

    def candidate(self, bvid: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM candidates WHERE bvid=? ORDER BY id DESC LIMIT 1", (bvid,)
            ).fetchone()
        return dict(row) if row else None

    def candidates_by_batch(self, batch_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM candidates WHERE batch_id=? ORDER BY id ASC", (batch_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_deep_by_bvid(self, bvid: str, **fields: Any) -> None:
        allowed = {"state", "deep_finished_at", "artifacts_json"}
        sets = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(bvid)
        with self._lock:
            self.conn.execute(
                f"UPDATE deep_tasks SET {', '.join(sets)} WHERE bvid=? AND state IN ('running','judging')",
                values,
            )
            self.conn.commit()

    # ---- must queue ---------------------------------------------------
    def add_must(self, must_id: str, bvid: str, memory_id: int, one_line: str, why: str, push_text: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO musts(id,bvid,memory_id,one_line,why,push_text,queue,state,attempts,discussion_status,produced_at) "
                "VALUES(?,?,?,?,?,?, 'primary','pending',0,'pending',?)",
                (must_id, bvid, int(memory_id or 0), one_line, why, push_text, now_iso()),
            )
            self.conn.commit()

    def add_admin_must(self, must_id: str, bvid: str, title: str, note: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO musts(id,bvid,memory_id,one_line,why,push_text,source,origin,queue,state,attempts,discussion_status,produced_at) "
                "VALUES(?,?,0,?,?,'','admin','admin_chat','admin','admin_pending',0,'pending',?)",
                (must_id, bvid, title or bvid, note or "管理员在对话里发来的视频", now_iso()),
            )
            self.conn.commit()

    def admin_must_for_bvid(self, bvid: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM musts WHERE bvid=? AND source='admin' "
                "AND state IN ('admin_pending','admin_reading','admin_discussing') "
                "ORDER BY produced_at DESC LIMIT 1",
                (bvid,),
            ).fetchone()
        return dict(row) if row else None

    def mark_admin_judged(self, must_id: str, one_line: str, why: str, memory_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE musts SET one_line=?, why=?, memory_id=?, state='admin_discussing', "
                "pushed_at=?, discussion_status='pending' WHERE id=? AND source='admin'",
                (one_line, why, int(memory_id or 0), now_iso(), must_id),
            )
            self.conn.commit()

    def get_must(self, must_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM musts WHERE id=?", (must_id,)).fetchone()
        return dict(row) if row else None

    def first_must(self, queue: str | None = None) -> dict | None:
        sql = "SELECT * FROM musts WHERE state='pending'"
        params: tuple = ()
        if queue:
            sql += " AND queue=?"
            params = (queue,)
        sql += " ORDER BY produced_at ASC LIMIT 1"
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def pushed_must(self) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM musts WHERE state IN "
                "('pushing','pushed','admin_pending','admin_reading','admin_discussing') "
                "ORDER BY COALESCE(pushed_at, produced_at) ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def update_must(self, must_id: str, **fields: Any) -> None:
        allowed = {
            "queue",
            "state",
            "attempts",
            "discussion_status",
            "pushed_at",
            "classified_at",
            "note",
            "memory_id",
            "push_text",
        }
        sets = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(must_id)
        with self._lock:
            self.conn.execute(f"UPDATE musts SET {', '.join(sets)} WHERE id=?", values)
            self.conn.commit()

    def search_undiscussed(self, query: str, limit: int = 5) -> list[dict]:
        q = f"%{str(query or '').strip()}%"
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM musts WHERE state='archived_shallow' AND "
                "(one_line LIKE ? OR why LIKE ? OR bvid LIKE ?) ORDER BY classified_at DESC LIMIT ?",
                (q, q, q, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_musts(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM musts WHERE state IN "
                "('pending','pushing','pushed','admin_pending','admin_reading','admin_discussing') "
                "ORDER BY produced_at ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_deep(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM deep_tasks ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def events_for(self, bvid: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE bvid=? ORDER BY id DESC LIMIT ?",
                (bvid, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_must_by_memory(self, memory_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM musts WHERE memory_id=? ORDER BY produced_at DESC LIMIT 1",
                (int(memory_id),),
            ).fetchone()
        return dict(row) if row else None

    # ---- quota ---------------------------------------------------------
    def _quota_row(self, date: str) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT * FROM quota WHERE date=?", (date,)).fetchone()
            if row:
                return dict(row)
            self.conn.execute("INSERT INTO quota(date) VALUES(?)", (date,))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM quota WHERE date=?", (date,)).fetchone()
        return dict(row)

    def quota(self, date: str) -> dict:
        return self._quota_row(date)

    def bump_quota(self, date: str, field: str, amount: int = 1) -> int:
        if field not in {"deep_used", "must_produced", "maybe_shared"}:
            raise ValueError("unknown quota field")
        self._quota_row(date)
        with self._lock:
            self.conn.execute(f"UPDATE quota SET {field}={field}+? WHERE date=?", (amount, date))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM quota WHERE date=?", (date,)).fetchone()
        return int(row[field]) if row else 0

    def add_deep_task(
        self,
        bvid: str,
        dsh_task_id: str,
        batch_id: str,
        hypothesis: str,
        must_id: str = "",
        origin: str = "auto",
        wake_umo: str = "",
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO deep_tasks(bvid,batch_id,dsh_task_id,state,hypothesis,must_id,origin,wake_umo,deep_started_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bvid,
                    batch_id,
                    dsh_task_id,
                    "running",
                    hypothesis,
                    must_id or None,
                    origin,
                    wake_umo,
                    now_iso(),
                    now_iso(),
                ),
            )
            self.conn.commit()

    def update_deep_task(self, dsh_task_id: str, **fields: Any) -> None:
        allowed = {"state", "attempts", "deep_finished_at", "artifacts_json", "hypothesis"}
        sets = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(dsh_task_id)
        with self._lock:
            self.conn.execute(f"UPDATE deep_tasks SET {', '.join(sets)} WHERE dsh_task_id=?", values)
            self.conn.commit()

    def running_deep_tasks(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM deep_tasks WHERE state IN ('running','judging') ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        return {
            "candidates": self.count("candidates"),
            "deep_pending": self.count("deep_tasks", "state IN ('pending','running')"),
            "must_primary": self.count("musts", "queue='primary' AND state IN ('pending','pushing','pushed')"),
            "must_secondary": self.count("musts", "queue='secondary' AND state='pending'"),
            "must_pushed": self.count("musts", "state='pushed'"),
            "must_archived": self.count("musts", "state IN ('archived_done','discarded_not_interested','archived_shallow')"),
        }
