"""Copy your laptop's money.db into the online database. Run it ONCE, on
your laptop, after /setup has succeeded:

    py migrate.py

It reads TURSO_DATABASE_URL and TURSO_AUTH_TOKEN from settings.txt and the
old data from money.db (both in this folder). Do not upload this file's
settings.txt or money.db to GitHub.
"""
import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
settings = BASE / "settings.txt"
if settings.exists():
    for line in settings.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

if not os.environ.get("TURSO_DATABASE_URL") or not os.environ.get("TURSO_AUTH_TOKEN"):
    sys.exit("Add TURSO_DATABASE_URL and TURSO_AUTH_TOKEN to settings.txt first.")

import store  # noqa: E402  (must come after the settings are loaded)

source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "money.db"
if not source_path.exists():
    sys.exit(f"Can't find {source_path}")
source = sqlite3.connect(source_path)
source.row_factory = sqlite3.Row


def read(sql: str) -> list[sqlite3.Row]:
    try:
        return source.execute(sql).fetchall()
    except sqlite3.OperationalError:       # table or column missing in old file
        return []


store.init_db()
existing = store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
if existing and "--force" not in sys.argv:
    sys.exit(f"The online database already has {existing} entries. Nothing was "
             "copied, to avoid duplicates.")

statements = []
for r in read("SELECT name FROM accounts"):
    statements.append(("INSERT OR IGNORE INTO accounts (name) VALUES (?)", (r["name"],)))
columns = [c["name"] for c in read("PRAGMA table_info(transactions)")]
to_account = "to_account" if "to_account" in columns else "NULL AS to_account"
transactions = read(f"SELECT id, ts, type, amount, category, account, note, "
                    f"{to_account} FROM transactions ORDER BY id")
for r in transactions:
    statements.append((
        "INSERT OR IGNORE INTO transactions (id, ts, type, amount, category, "
        "account, note, to_account) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (r["id"], r["ts"], r["type"], r["amount"], r["category"], r["account"],
         r["note"], r["to_account"])))
    statements.append(("INSERT OR IGNORE INTO accounts (name) VALUES (?)",
                       (r["account"],)))
for r in read("SELECT category, amount FROM budgets"):
    statements.append(("INSERT OR REPLACE INTO budgets (category, amount) "
                       "VALUES (?, ?)", (r["category"], r["amount"])))
for r in read("SELECT day, text, last_run FROM recurring"):
    statements.append(("INSERT INTO recurring (day, text, last_run) VALUES (?, ?, ?)",
                       (r["day"], r["text"], r["last_run"])))
for r in read("SELECT word, category FROM keywords"):
    statements.append(("INSERT OR REPLACE INTO keywords (word, category) "
                       "VALUES (?, ?)", (r["word"], r["category"])))
if any(r["value"] for r in read("SELECT value FROM meta WHERE key = 'remind_time'")):
    statements.append(("INSERT OR REPLACE INTO meta (key, value) "
                       "VALUES ('remind', 'on')", ()))

for start in range(0, len(statements), 100):
    store.batch(statements[start:start + 100])

online = store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
print(f"Copied {len(transactions)} entries. The online database now has {online}.")
