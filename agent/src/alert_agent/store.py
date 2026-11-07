"""SQLite state (data-model.md). One connection behind a lock; every public call hops to a thread."""
import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# reported_only stays open so Alertmanager repeats fold into it instead of posting again.
OPEN_STATES = ("new", "held", "queued", "analysing", "diagnosed", "reported_only", "folded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    am_fingerprint TEXT,
    status TEXT NOT NULL,
    labels TEXT NOT NULL,
    annotations TEXT NOT NULL,
    starts_at TEXT,
    ends_at TEXT,
    received_at TEXT NOT NULL,
    incident_id INTEGER REFERENCES incidents(id),
    processed INTEGER NOT NULL DEFAULT 0   -- not in data-model.md: lets intake skip markers that never get an incident
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    source TEXT, rule TEXT, rule_ref TEXT, namespace TEXT, service TEXT, route TEXT,
    severity TEXT,
    protected INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    correlation_id INTEGER,
    gate_reason TEXT,
    verdict TEXT,
    message_id INTEGER,
    held_at TEXT,          -- not in data-model.md: start of the hold, for the 10 minute ceiling
    gate_checked_at TEXT   -- not in data-model.md: paces the 30 s re-check
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id),
    model TEXT,
    started_at TEXT, finished_at TEXT,
    tool_calls INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL,
    outcome TEXT,
    engine TEXT NOT NULL DEFAULT 'native',  -- not in data-model.md: native | holmes, for the engine comparison
    duration_seconds REAL,
    verdict TEXT  -- not in data-model.md: this run's verdict JSON, so each engine can be graded
);
CREATE INDEX IF NOT EXISTS idx_incidents_key_state ON incidents(key, state);
CREATE INDEX IF NOT EXISTS idx_alerts_processed ON alerts(processed, id);
"""

INCIDENT_COLUMNS = {
    "key", "source", "rule", "rule_ref", "namespace", "service", "route", "severity", "protected", "state",
    "occurrences", "first_seen", "last_seen", "correlation_id", "gate_reason", "verdict", "message_id",
    "held_at", "gate_checked_at",
}


def now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _check(fields: dict) -> None:
    bad = set(fields) - INCIDENT_COLUMNS
    if bad:
        raise ValueError(f"unknown incident columns: {sorted(bad)}")


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
            for col, ddl in (("engine", "TEXT NOT NULL DEFAULT 'native'"), ("duration_seconds", "REAL"), ("verdict", "TEXT")):
                if col not in cols:  # database created before the engine switch
                    self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn, *args):
        def call():
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(call)

    # Helpers below run under the lock.
    def _all(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _write(self, sql, params=()):
        with self._conn:
            return self._conn.execute(sql, params)

    @staticmethod
    def _decode(rows):
        for r in rows:
            r["labels"] = json.loads(r["labels"])
            r["annotations"] = json.loads(r["annotations"])
        return rows

    # --- alerts ---
    async def ping(self) -> None:
        await self._run(self._one, "SELECT 1")

    async def insert_alerts(self, rows: list[dict]) -> int:
        def op():
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO alerts (am_fingerprint, status, labels, annotations, starts_at, ends_at, received_at)"
                    " VALUES (:am_fingerprint, :status, :labels, :annotations, :starts_at, :ends_at, :received_at)",
                    rows,
                )
            return len(rows)

        return await self._run(op)

    async def unprocessed_alerts(self) -> list[dict]:
        return await self._run(lambda: self._decode(self._all("SELECT * FROM alerts WHERE processed = 0 ORDER BY id")))

    async def mark_alert(self, alert_id: int, incident_id: int | None) -> None:
        await self._run(self._write, "UPDATE alerts SET processed = 1, incident_id = ? WHERE id = ?", (incident_id, alert_id))

    async def alerts_for_incident(self, incident_id: int, limit: int = 3) -> list[dict]:
        return await self._run(
            lambda: self._decode(
                self._all("SELECT * FROM alerts WHERE incident_id = ? ORDER BY id DESC LIMIT ?", (incident_id, limit))
            )
        )

    async def latest_alert_status(self, incident_id: int) -> str | None:
        row = await self._run(
            self._one, "SELECT status FROM alerts WHERE incident_id = ? ORDER BY id DESC LIMIT 1", (incident_id,)
        )
        return row["status"] if row else None

    async def marker_active(self, namespace: str, service: str) -> bool:
        """True while the latest ServiceRollingOut notification for this service is firing."""
        row = await self._run(
            self._one,
            "SELECT status FROM alerts WHERE json_extract(labels, '$.alertname') = 'ServiceRollingOut'"
            " AND json_extract(labels, '$.namespace') = ? AND json_extract(labels, '$.service') = ?"
            " ORDER BY id DESC LIMIT 1",
            (namespace, service),
        )
        return bool(row and row["status"] == "firing")

    # --- incidents ---
    async def upsert_incident(self, fields: dict) -> int:
        """Insert, or update when `id` is present; returns the id."""
        fields = dict(fields)
        incident_id = fields.pop("id", None)
        _check(fields)

        def op():
            if incident_id is None:
                cols = ", ".join(fields)
                marks = ", ".join(f":{c}" for c in fields)
                return self._write(f"INSERT INTO incidents ({cols}) VALUES ({marks})", fields).lastrowid
            sets = ", ".join(f"{c} = :{c}" for c in fields)
            self._write(f"UPDATE incidents SET {sets} WHERE id = :id", {**fields, "id": incident_id})
            return incident_id

        return await self._run(op)

    async def set_state(self, incident_id: int, state: str, expect: str | None = None, **extra) -> bool:
        """Change state (only from `expect` when given); returns whether a row changed."""
        _check(extra)
        fields = {"state": state, **extra}
        sets = ", ".join(f"{c} = :{c}" for c in fields)
        sql = f"UPDATE incidents SET {sets} WHERE id = :id"
        params = {**fields, "id": incident_id}
        if expect is not None:
            sql += " AND state = :expect"
            params["expect"] = expect
        return (await self._run(self._write, sql, params)).rowcount > 0

    async def get_incident(self, incident_id: int) -> dict | None:
        return await self._run(self._one, "SELECT * FROM incidents WHERE id = ?", (incident_id,))

    async def open_incident_by_key(self, key: str) -> dict | None:
        marks = ",".join("?" * len(OPEN_STATES))
        return await self._run(
            self._one,
            f"SELECT * FROM incidents WHERE key = ? AND state IN ({marks}) ORDER BY id DESC LIMIT 1",
            (key, *OPEN_STATES),
        )

    async def incidents_in_state(self, *states: str) -> list[dict]:
        marks = ",".join("?" * len(states))
        return await self._run(
            self._all, f"SELECT * FROM incidents WHERE state IN ({marks}) ORDER BY first_seen, id", states
        )

    async def correlatable(self, namespace: str, since: str) -> dict | None:
        """A not yet analysed, non-protected incident of the same namespace opened since `since`."""
        return await self._run(
            self._one,
            "SELECT * FROM incidents WHERE namespace = ? AND protected = 0 AND state IN ('new','held','queued')"
            " AND first_seen >= ? ORDER BY id LIMIT 1",
            (namespace, since),
        )

    async def fold_peer(self, namespace: str, since: str, protected: int, rule: str, actor: str) -> dict | None:
        """A same-namespace incident already being or just analysed; protected ones must share rule and actor (last key part)."""
        sql = "SELECT * FROM incidents WHERE namespace = ? AND protected = ? AND state IN ('analysing','diagnosed') AND first_seen >= ?"
        params: list = [namespace, protected, since]
        if protected:
            sql += " AND rule = ? AND key LIKE ?"
            params += [rule, "%|" + actor]
        return await self._run(self._one, sql + " ORDER BY id DESC LIMIT 1", tuple(params))

    async def queued_protected_peer(self, namespace: str, since: str, rule: str, actor: str) -> dict | None:
        """A protected incident of the same rule and actor still waiting for its run: they share one."""
        return await self._run(
            self._one,
            "SELECT * FROM incidents WHERE namespace = ? AND protected = 1 AND state = 'queued' AND first_seen >= ? AND rule = ? AND key LIKE ?"
            " ORDER BY id LIMIT 1",
            (namespace, since, rule, "%|" + actor),
        )

    async def requeue_analysing(self) -> int:
        return (await self._run(self._write, "UPDATE incidents SET state = 'queued' WHERE state = 'analysing'")).rowcount

    # --- runs and cost ---
    async def record_run(self, run: dict) -> int:
        return (
            await self._run(
                self._write,
                "INSERT INTO runs (incident_id, model, started_at, finished_at, tool_calls, input_tokens,"
                " output_tokens, cost_usd, outcome, engine, duration_seconds, verdict) VALUES (:incident_id, :model, :started_at,"
                " :finished_at, :tool_calls, :input_tokens, :output_tokens, :cost_usd, :outcome, :engine, :duration_seconds, :verdict)",
                {"engine": "native", "duration_seconds": None, "verdict": None, **run},
            )
        ).lastrowid

    async def cost_today(self, tz: str, now: datetime | None = None) -> float:
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
        since = now_iso(local.replace(hour=0, minute=0, second=0, microsecond=0))
        row = await self._run(self._one, "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE started_at >= ?", (since,))
        return float(row["c"])
