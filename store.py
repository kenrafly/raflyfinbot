"""Database access.

Online (Vercel): talks to a Turso database over HTTPS. Turso is SQLite in the
cloud, so the SQL is the same as on your laptop.
Offline (testing): if TURSO_DATABASE_URL is not set, uses a local SQLite file.
"""
import http.client
import json
import os
import sqlite3
import threading
from urllib.parse import urlparse

TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
LOCAL_PATH = os.environ.get("DB_PATH", "money.db")

_local = threading.local()          # one kept-alive connection per thread


class DatabaseError(Exception):
    pass


class Result:
    def __init__(self, rows: list[dict], lastrowid: int | None, rowcount: int):
        self.rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount


# ---------------------------------------------------------------- Turso
def _encode(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _decode(cell: dict):
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    return cell.get("value")


def _post(body: bytes) -> dict:
    url = urlparse(TURSO_URL.replace("libsql://", "https://", 1))
    headers = {"Authorization": f"Bearer {TURSO_TOKEN}",
               "Content-Type": "application/json"}
    for attempt in (1, 2):
        try:
            conn = getattr(_local, "conn", None)
            if conn is None:
                maker = (http.client.HTTPConnection if url.scheme == "http"
                         else http.client.HTTPSConnection)
                conn = _local.conn = maker(url.netloc, timeout=20)
            conn.request("POST", "/v2/pipeline", body, headers)
            response = conn.getresponse()
            data = response.read()
            break
        except (http.client.HTTPException, OSError):
            _local.conn = None              # stale connection: reconnect once
            if attempt == 2:
                raise DatabaseError("Could not reach the database.")
    if response.status != 200:
        raise DatabaseError(f"Database returned HTTP {response.status}: "
                            f"{data[:200].decode('utf-8', 'replace')}")
    return json.loads(data)


def _turso_batch(statements: list[tuple]) -> list[Result]:
    requests = [{"type": "execute",
                 "stmt": {"sql": sql, "args": [_encode(v) for v in params]}}
                for sql, params in statements]
    requests.append({"type": "close"})
    reply = _post(json.dumps({"requests": requests}).encode())
    results = []
    for item in reply["results"][:len(statements)]:
        if item.get("type") != "ok":
            raise DatabaseError(item.get("error", {}).get("message", "database error"))
        result = item["response"]["result"]
        names = [c["name"] for c in result["cols"]]
        rows = [dict(zip(names, (_decode(c) for c in row))) for row in result["rows"]]
        rowid = result.get("last_insert_rowid")
        results.append(Result(rows, int(rowid) if rowid is not None else None,
                              result.get("affected_row_count", 0)))
    return results


# ---------------------------------------------------------------- local file
def _sqlite_batch(statements: list[tuple]) -> list[Result]:
    conn = sqlite3.connect(LOCAL_PATH)
    conn.row_factory = sqlite3.Row
    try:
        results = []
        for sql, params in statements:
            try:
                cur = conn.execute(sql, params)
            except sqlite3.Error as error:
                raise DatabaseError(str(error))
            results.append(Result([dict(r) for r in cur.fetchall()],
                                  cur.lastrowid, cur.rowcount))
        conn.commit()
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------- public
def batch(statements: list[tuple]) -> list[Result]:
    """Run several statements in one round trip. Each item is (sql, params)."""
    statements = [(s[0], tuple(s[1]) if len(s) > 1 else ()) for s in statements]
    if TURSO_URL:
        return _turso_batch(statements)
    return _sqlite_batch(statements)


def query(sql: str, params: tuple = ()) -> list[dict]:
    return batch([(sql, params)])[0].rows


def run(sql: str, params: tuple = ()) -> Result:
    return batch([(sql, params)])[0]


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS transactions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT    NOT NULL,
        type       TEXT    NOT NULL,
        amount     INTEGER NOT NULL,
        category   TEXT    NOT NULL,
        account    TEXT    NOT NULL DEFAULT 'cash',
        note       TEXT    NOT NULL DEFAULT '',
        to_account TEXT)""",
    "CREATE INDEX IF NOT EXISTS transactions_ts ON transactions (ts)",
    "CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, amount INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS recurring (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        day      INTEGER NOT NULL,
        text     TEXT    NOT NULL,
        last_run TEXT    NOT NULL DEFAULT '')""",
    "CREATE TABLE IF NOT EXISTS keywords (word TEXT PRIMARY KEY, category TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY)",
    "INSERT OR IGNORE INTO accounts (name) VALUES ('cash')",
]


def init_db() -> None:
    batch([(sql, ()) for sql in SCHEMA])
