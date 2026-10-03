"""Money bot logic: understands messages, stores entries, answers commands.

This version is called by Telegram through a webhook (see app.py) instead of
running all day, so it works on Vercel.
"""
import calendar
import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import store
import tg

OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("BOT_TZ", "Asia/Jakarta"))
except Exception:
    TZ = timezone(timedelta(hours=7))      # Jakarta time (UTC+7)

REMINDER_HOUR = 20      # the evening check runs around 21:00 Jakarta time


def now() -> datetime:
    return datetime.now(TZ)


# ================================================================ vocabulary
DEFAULT_KEYWORDS = {
    "food": ["coffee", "kopi", "lunch", "dinner", "breakfast", "makan", "food",
             "snack", "resto", "warung", "groceries", "belanja", "gofood",
             "grabfood", "sarapan", "jajan", "minum"],
    "transport": ["grab", "gojek", "taxi", "bus", "train", "kereta", "bensin",
                  "fuel", "parkir", "parking", "toll", "tol", "ojek", "mrt"],
    "bills": ["rent", "kos", "listrik", "electricity", "internet", "wifi",
              "pulsa", "water", "air", "subscription", "kuota"],
    "shopping": ["clothes", "baju", "shoes", "sepatu", "gadget"],
    "health": ["doctor", "dokter", "obat", "medicine", "gym"],
    "fun": ["movie", "bioskop", "game", "netflix", "spotify", "concert"],
}
INCOME_WORDS = {"salary", "gaji", "bonus", "freelance", "refund", "dividend"}
TRANSFER_WORDS = {"transfer", "tf", "pindah"}
YESTERDAY_WORDS = {"yesterday", "kemarin"}
FILLER_WORDS = {"to", "ke", "from", "dari"}

MULTIPLIERS = {"k": 1_000, "rb": 1_000, "ribu": 1_000,
               "jt": 1_000_000, "juta": 1_000_000}
AMOUNT_RE = re.compile(
    r"(?<![\w.,])(\d[\d.,]*)\s*(k|rb|ribu|jt|juta)?(?!\w)", re.IGNORECASE)


class Context:
    """What the bot needs to know to read a message: your accounts and the
    words you taught it. Loaded once per message, in a single database trip."""

    def __init__(self, accounts: list[str], taught: dict[str, str],
                 duplicate: bool = False):
        self.accounts = accounts or ["cash"]
        self.keywords = {w: c for c, words in DEFAULT_KEYWORDS.items() for w in words}
        self.keywords.update(taught)
        self.duplicate = duplicate


def load_context(update_id: int | None = None) -> Context:
    statements = [("SELECT name FROM accounts ORDER BY rowid", ()),
                  ("SELECT word, category FROM keywords", ())]
    if update_id is not None:      # remember this update so a retry is ignored
        statements += [("INSERT OR IGNORE INTO updates (id) VALUES (?)", (update_id,)),
                       ("DELETE FROM updates WHERE id < ?", (update_id - 500,))]
    results = store.batch(statements)
    duplicate = update_id is not None and results[2].rowcount == 0
    return Context([r["name"] for r in results[0].rows],
                   {r["word"]: r["category"] for r in results[1].rows}, duplicate)


def categories() -> list[str]:
    names = list(DEFAULT_KEYWORDS)
    extra = store.query(
        "SELECT category FROM keywords UNION SELECT category FROM budgets "
        "UNION SELECT category FROM transactions WHERE type = 'expense'")
    for r in extra:
        if r["category"] not in names and r["category"] != "other":
            names.append(r["category"])
    return names + ["other"]


# ================================================================ parsing
def parse_amount(number: str, suffix: str | None) -> int | None:
    """'25' + 'k' -> 25000, '1.5' + 'jt' -> 1500000, '25.000' -> 25000."""
    number = number.rstrip(".,")
    if suffix:
        try:
            return round(float(number.replace(",", ".")) * MULTIPLIERS[suffix.lower()])
        except ValueError:
            return None
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", number):
        return int(re.sub(r"[.,]", "", number))
    if number.isdigit():
        return int(number)
    return None                     # a bare "2.5" is ambiguous


def find_amount(text: str) -> tuple[int, str] | None:
    """Return (amount, text without the amount). The last number wins."""
    matches = list(AMOUNT_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    amount = parse_amount(m.group(1), m.group(2))
    if not amount or amount <= 0:
        return None
    rest = re.sub(r"\s+", " ", text[:m.start()] + " " + text[m.end():]).strip()
    return amount, rest


def parse_message(text: str, ctx: Context) -> dict | None:
    """Turn a typed message into a transaction.

    Returns None when there is no amount, {'error': ...} when something is
    missing, otherwise a dict ready for save_transaction()."""
    text = text.strip()
    is_income = text.startswith("+")
    text = text.lstrip("+").strip()

    found = find_amount(text)
    if not found:
        return None
    amount, rest = found
    known = ctx.accounts
    words = rest.split()
    lower = [w.lower() for w in words]

    days_ago = 0
    if any(w in YESTERDAY_WORDS for w in lower):
        days_ago = 1
        words = [w for w in words if w.lower() not in YESTERDAY_WORDS]
        lower = [w.lower() for w in words]

    if lower and lower[0] in TRANSFER_WORDS:
        named = [w for w in lower[1:] if w in known]
        if len(named) < 2 or named[0] == named[1]:
            return {"error": "For a transfer, name two of your accounts, e.g.\n"
                             f"transfer 500k {known[0]} bca\n"
                             "Your accounts: " + ", ".join(known)}
        return {"type": "transfer", "amount": amount, "category": "transfer",
                "account": named[0], "to_account": named[1],
                "note": "transfer", "days_ago": days_ago}

    account = next((w for w in lower if w in known), known[0])
    note_words = [w for w in words if w.lower() != account]
    while note_words and note_words[-1].lower() in FILLER_WORDS:
        note_words.pop()            # "coffee 25k from bca" -> "coffee"
    note = " ".join(note_words)

    plain = re.findall(r"[a-z]+", note.lower())
    if set(plain) & INCOME_WORDS:
        is_income = True
    if is_income:
        category = "income"
    else:
        category = next((ctx.keywords[w] for w in plain if w in ctx.keywords), "other")

    return {"type": "income" if is_income else "expense", "amount": amount,
            "category": category, "account": account, "to_account": None,
            "note": note or "(no note)", "days_ago": days_ago}


# ================================================================ data
def month_range(month: str) -> tuple[str, str]:
    """'2026-10' -> ('2026-10-01', '2026-11-01')."""
    year, mon = int(month[:4]), int(month[5:7])
    nxt = f"{year + 1}-01" if mon == 12 else f"{year}-{mon + 1:02d}"
    return f"{month}-01", f"{nxt}-01"


def save_transaction(tx: dict) -> tuple[dict, str | None]:
    """Store the entry. Returns (the stored row, budget alert or None).
    Insert and budget check happen in one database trip."""
    when = now() - timedelta(days=tx.get("days_ago", 0))
    ts = when.strftime("%Y-%m-%d %H:%M:%S")
    statements = [(
        "INSERT INTO transactions (ts, type, amount, category, account, "
        "to_account, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, tx["type"], tx["amount"], tx["category"], tx["account"],
         tx.get("to_account"), tx["note"]))]
    check_budget = tx["type"] == "expense" and not tx.get("days_ago")
    if check_budget:
        start, end = month_range(now().strftime("%Y-%m"))
        statements += [
            ("SELECT amount FROM budgets WHERE category = ?", (tx["category"],)),
            ("SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
             "WHERE type = 'expense' AND category = ? AND ts >= ? AND ts < ?",
             (tx["category"], start, end))]
    results = store.batch(statements)
    row = {"id": results[0].lastrowid, "ts": ts, "type": tx["type"],
           "amount": tx["amount"], "category": tx["category"],
           "account": tx["account"], "to_account": tx.get("to_account"),
           "note": tx["note"]}
    alert = None
    if check_budget and results[1].rows:
        alert = budget_alert(tx["category"], tx["amount"],
                             results[1].rows[0]["amount"],
                             results[2].rows[0]["total"])
    return row, alert


def budget_alert(category: str, amount: int, budget: int, after: int) -> str | None:
    """Warn when an expense pushes its category past 80% or 100% of budget."""
    before = after - amount
    if before < budget <= after:
        return (f"Budget reached: {category} is at {money(after)} "
                f"of {money(budget)} this month.")
    if before < 0.8 * budget <= after:
        return (f"Budget warning: {category} is at {money(after)} of "
                f"{money(budget)} ({round(after / budget * 100)}%) this month.")
    return None


def summary(start: str, end: str) -> dict:
    """Income and spending for start <= date < end. Transfers don't count."""
    data = store.query(
        "SELECT type, category, SUM(amount) AS total FROM transactions "
        "WHERE ts >= ? AND ts < ? AND type IN ('income', 'expense') "
        "GROUP BY type, category ORDER BY total DESC", (start, end))
    income = sum(r["total"] for r in data if r["type"] == "income")
    expense = sum(r["total"] for r in data if r["type"] == "expense")
    by_category = [(r["category"], r["total"]) for r in data if r["type"] == "expense"]
    return {"income": income, "expense": expense, "by_category": by_category}


def balances_from(account_names: list[str], grouped: list[dict]) -> dict[str, int]:
    """Account balances from rows of (type, account, to_account, total)."""
    balance = {name: 0 for name in account_names}
    for r in grouped:
        balance.setdefault(r["account"], 0)
        if r["type"] in ("income", "adjust"):
            balance[r["account"]] += r["total"]
        elif r["type"] == "expense":
            balance[r["account"]] -= r["total"]
        elif r["type"] == "transfer":
            balance[r["account"]] -= r["total"]
            if r["to_account"]:
                balance.setdefault(r["to_account"], 0)
                balance[r["to_account"]] += r["total"]
    return balance


BALANCE_SQL = ("SELECT type, account, to_account, SUM(amount) AS total "
               "FROM transactions GROUP BY type, account, to_account")


def account_balances(ctx: Context) -> dict[str, int]:
    return balances_from(ctx.accounts, store.query(BALANCE_SQL))


def run_due_recurring(today: datetime, ctx: Context) -> list[str]:
    """Log every recurring entry that is due. Returns messages to send."""
    month = today.strftime("%Y-%m")
    last_day = calendar.monthrange(today.year, today.month)[1]
    messages = []
    for item in store.query("SELECT * FROM recurring ORDER BY id"):
        if item["last_run"] == month or today.day < min(item["day"], last_day):
            continue
        # claim it first, so two overlapping runs can't both log it
        claimed = store.run(
            "UPDATE recurring SET last_run = ? WHERE id = ? AND last_run != ?",
            (month, item["id"], month))
        if not claimed.rowcount:
            continue
        tx = parse_message(item["text"], ctx)
        if not tx or "error" in tx:
            messages.append(f"Recurring entry {item['id']} ({item['text']}) could "
                            "not be logged. Remove it and add it again.")
            continue
        row, alert = save_transaction(tx)
        messages.append("Recurring logged:\n" + tx_line(row))
        if alert:
            messages.append(alert)
    return messages


def reminder_due(today: datetime) -> bool:
    """True once per day, in the evening, if nothing was logged today."""
    day = today.strftime("%Y-%m-%d")
    results = store.batch([
        ("SELECT key, value FROM meta WHERE key IN ('remind', 'reminded_on')", ()),
        ("SELECT 1 AS found FROM transactions WHERE ts >= ? LIMIT 1", (day,))])
    meta = {r["key"]: r["value"] for r in results[0].rows}
    if meta.get("remind") != "on" or today.hour < REMINDER_HOUR:
        return False
    if meta.get("reminded_on") == day:
        return False
    set_meta("reminded_on", day)
    return not results[1].rows


def set_meta(key: str, value: str) -> None:
    store.run("INSERT INTO meta (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_meta(key: str) -> str:
    found = store.query("SELECT value FROM meta WHERE key = ?", (key,))
    return (found[0]["value"] or "") if found else ""


def export_file(month: str | None) -> tuple[bytes, str]:
    """All transactions (or one month) as an Excel file; CSV if the openpyxl
    package is unavailable. Returns (file bytes, file name)."""
    if month:
        data = store.query("SELECT * FROM transactions WHERE ts >= ? AND ts < ? "
                           "ORDER BY ts, id", month_range(month))
    else:
        data = store.query("SELECT * FROM transactions ORDER BY ts, id")
    header = ["id", "date", "time", "type", "amount", "category", "account",
              "to_account", "note"]
    table = [[r["id"], r["ts"][:10], r["ts"][11:], r["type"], r["amount"],
              r["category"], r["account"], r["to_account"] or "", r["note"]]
             for r in data]
    name = f"transactions-{month or 'all'}"
    try:
        from openpyxl import Workbook
    except ImportError:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(table)
        return buffer.getvalue().encode("utf-8-sig"), name + ".csv"
    book = Workbook()
    sheet = book.active
    sheet.title = "Transactions"
    sheet.append(header)
    for line in table:
        sheet.append(line)
    for column, width in zip("ABCDEFGHI", (6, 12, 10, 10, 14, 14, 12, 12, 32)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue(), name + ".xlsx"


BACKUP_TABLES = ["transactions", "accounts", "budgets", "recurring", "keywords", "meta"]


def backup_file() -> tuple[bytes, str]:
    """Everything in the database as one JSON file."""
    results = store.batch([(f"SELECT * FROM {t}", ()) for t in BACKUP_TABLES])
    dump = {t: r.rows for t, r in zip(BACKUP_TABLES, results)}
    name = f"money-backup-{now().strftime('%Y-%m-%d')}.json"
    return json.dumps(dump, indent=1).encode(), name


# ================================================================ formatting
def money(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}Rp" + f"{abs(amount):,}".replace(",", ".")


def tx_line(r: dict) -> str:
    day = f"{r['ts'][8:10]}/{r['ts'][5:7]}"
    if r["type"] == "transfer":
        return f"#{r['id']} {day} {money(r['amount'])} {r['account']} -> {r['to_account']}"
    if r["type"] == "adjust":
        return f"#{r['id']} {day} {money(r['amount'])} balance adjustment ({r['account']})"
    sign = "+" if r["type"] == "income" else "-"
    return (f"#{r['id']} {day} {sign}{money(r['amount'])} {r['note']} "
            f"({r['category']}, {r['account']})")


def format_summary(title: str, s: dict) -> str:
    lines = [title, "",
             f"Income:  {money(s['income'])}",
             f"Expense: {money(s['expense'])}",
             f"Net:     {money(s['income'] - s['expense'])}"]
    if s["by_category"]:
        lines += ["", "Spending by category:"]
        lines += [f"  {cat}: {money(total)}" for cat, total in s["by_category"]]
    return "\n".join(lines)


def valid_month(text: str) -> str | None:
    text = text.strip()
    return text if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text) else None


def clean_name(text: str) -> str | None:
    name = text.strip().lower()
    return name if re.fullmatch(r"[a-z][a-z0-9_]{0,19}", name) else None


HELP = """Type a transaction to log it:
  coffee 25k
  grab 18rb gopay          (paid from an account)
  + freelance 1.5jt bca    (+ means income)
  kemarin makan 30k        (dated yesterday)
  transfer 500k bca gopay  (between accounts)

Reports
/today  /week  /month  (or /month 2026-09)
/list - last 10 entries (/list 30 for more)
/search coffee - find entries
/export - Excel file of everything (/export 2026-09 for one month)
/dashboard - link to your web dashboard

Fixing mistakes
/undo - delete the last entry
/delete 12 - delete entry #12

Accounts
/accounts - balances
/addaccount bca 2.5jt - new account with its current balance
/setbalance bca 3jt - correct a balance

Budgets
/budget food 1.5jt - monthly budget (0 removes it)
/budgets - progress this month

Automatic
/addrecurring 1 kos 1.25jt bca - log on day 1 of every month
/recurring - list them   /delrecurring 2 - remove one
/remind on - evening nudge (around 21:00) if nothing was logged (/remind off)

Categories
/categories - list them
/addword food bakso - teach a word (also creates new categories)

/backup - sends you a file with all your data"""

MENU = [("today", "Today's summary"), ("month", "This month's summary"),
        ("accounts", "Account balances"), ("list", "Recent entries"),
        ("budgets", "Budget progress"), ("undo", "Delete the last entry"),
        ("dashboard", "Open the web dashboard"), ("help", "All commands")]


# ================================================================ keyboards
def saved_keyboard(tx_id: int) -> dict:
    return tg.keyboard([[("Change category", f"cat:{tx_id}"),
                         ("Delete", f"del:{tx_id}")]])


def keyboard_for(row: dict) -> dict:
    if row["type"] == "expense":
        return saved_keyboard(row["id"])
    return tg.keyboard([[("Delete", f"del:{row['id']}")]])


def category_keyboard(tx_id: int) -> dict:
    buttons = [(c, f"set:{tx_id}:{c}") for c in categories()]
    grid = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    grid.append([("Cancel", f"back:{tx_id}")])
    return tg.keyboard(grid)


# ================================================================ commands
# Each command gets (chat id, the text after the command, context).

def cmd_help(chat, args, ctx):
    tg.send(chat, HELP)


def cmd_today(chat, args, ctx):
    start = now().strftime("%Y-%m-%d")
    end = (now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tg.send(chat, format_summary(f"Today ({start})", summary(start, end)))


def cmd_week(chat, args, ctx):
    monday = now() - timedelta(days=now().weekday())
    start = monday.strftime("%Y-%m-%d")
    end = (monday + timedelta(days=7)).strftime("%Y-%m-%d")
    tg.send(chat, format_summary(f"This week (from {start})", summary(start, end)))


def cmd_month(chat, args, ctx):
    if args and not valid_month(args):
        tg.send(chat, "Use the format /month 2026-09")
        return
    month = valid_month(args) or now().strftime("%Y-%m")
    tg.send(chat, format_summary(f"Month {month}", summary(*month_range(month))))


def cmd_accounts(chat, args, ctx):
    balances = account_balances(ctx)
    lines = ["Accounts", ""] + [f"  {n}: {money(b)}" for n, b in balances.items()]
    lines += ["", f"Total: {money(sum(balances.values()))}"]
    tg.send(chat, "\n".join(lines))


def cmd_list(chat, args, ctx):
    count = int(args) if args.isdigit() else 10
    found = store.query("SELECT * FROM transactions ORDER BY id DESC LIMIT ?",
                        (max(1, min(count, 50)),))
    tg.send(chat, "\n".join(tx_line(r) for r in found) if found else "No entries yet.")


def cmd_search(chat, args, ctx):
    if not args:
        tg.send(chat, "Use it like this: /search coffee")
        return
    like = f"%{args}%"
    found = store.query(
        "SELECT * FROM transactions WHERE note LIKE ? OR category LIKE ? "
        "OR account LIKE ? ORDER BY id DESC LIMIT 30", (like, like, like))
    if not found:
        tg.send(chat, f"Nothing found for '{args}'.")
        return
    total = sum(r["amount"] for r in found if r["type"] == "expense")
    tg.send(chat, "\n".join(tx_line(r) for r in found) +
            f"\n\nExpenses in these results: {money(total)}")


def cmd_export(chat, args, ctx):
    if args and not valid_month(args):
        tg.send(chat, "Use /export for everything or /export 2026-09")
        return
    data, name = export_file(valid_month(args))
    tg.send_document(chat, name, data)


def cmd_backup(chat, args, ctx):
    data, name = backup_file()
    tg.send_document(chat, name, data)


def cmd_dashboard(chat, args, ctx):
    url = get_meta("base_url")
    tg.send(chat, url if url else "The dashboard address isn't saved yet. "
                                  "Open /setup on your site once.")


def cmd_undo(chat, args, ctx):
    found = store.query("SELECT * FROM transactions ORDER BY id DESC LIMIT 1")
    if not found:
        tg.send(chat, "Nothing to delete.")
        return
    store.run("DELETE FROM transactions WHERE id = ?", (found[0]["id"],))
    tg.send(chat, "Deleted: " + tx_line(found[0]))


def cmd_delete(chat, args, ctx):
    arg = args.lstrip("#")
    if not arg.isdigit():
        tg.send(chat, "Use it like this: /delete 12  (see numbers in /list)")
        return
    results = store.batch([("SELECT * FROM transactions WHERE id = ?", (int(arg),)),
                           ("DELETE FROM transactions WHERE id = ?", (int(arg),))])
    found = results[0].rows
    tg.send(chat, "Deleted: " + tx_line(found[0]) if found else f"No entry #{arg}.")


def cmd_addaccount(chat, args, ctx):
    found = find_amount(args)
    name = clean_name(found[1] if found else args)
    if not name:
        tg.send(chat, "Use it like this: /addaccount bca 2.5jt\n"
                      "(one word; the balance is optional)")
        return
    if name in ctx.accounts:
        tg.send(chat, f"Account '{name}' already exists.")
        return
    store.run("INSERT OR IGNORE INTO accounts (name) VALUES (?)", (name,))
    if found:
        save_transaction({"type": "adjust", "amount": found[0], "category": "adjust",
                          "account": name, "note": "opening balance"})
    tg.send(chat, f"Account '{name}' added" +
            (f" with {money(found[0])}." if found else "."))


def cmd_setbalance(chat, args, ctx):
    parts = args.split()
    if len(parts) == 2 and parts[1] == "0":
        name, target = clean_name(parts[0]), 0
    else:
        found = find_amount(args)
        name, target = (clean_name(found[1]), found[0]) if found else (None, 0)
    if not name or name not in ctx.accounts:
        tg.send(chat, "Use it like this: /setbalance bca 3jt\n"
                      "Your accounts: " + ", ".join(ctx.accounts))
        return
    difference = target - account_balances(ctx)[name]
    if difference:
        save_transaction({"type": "adjust", "amount": difference,
                          "category": "adjust", "account": name,
                          "note": "balance correction"})
    tg.send(chat, f"{name} is now {money(target)} (adjusted by {money(difference)}).")


def cmd_budget(chat, args, ctx):
    parts = args.split()
    if len(parts) == 2 and parts[1] == "0" and clean_name(parts[0]):
        store.run("DELETE FROM budgets WHERE category = ?", (clean_name(parts[0]),))
        tg.send(chat, f"Budget for {clean_name(parts[0])} removed.")
        return
    found = find_amount(args)
    category = clean_name(found[1]) if found else None
    if not category:
        tg.send(chat, "Use it like this: /budget food 1.5jt\n"
                      "Categories: " + ", ".join(categories()))
        return
    start, end = month_range(now().strftime("%Y-%m"))
    results = store.batch([
        ("INSERT INTO budgets (category, amount) VALUES (?, ?) "
         "ON CONFLICT(category) DO UPDATE SET amount = excluded.amount",
         (category, found[0])),
        ("SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
         "WHERE type = 'expense' AND category = ? AND ts >= ? AND ts < ?",
         (category, start, end))])
    tg.send(chat, f"Budget set: {category} {money(found[0])} per month.\n"
                  f"Spent so far this month: {money(results[1].rows[0]['total'])}.")


def cmd_budgets(chat, args, ctx):
    start, end = month_range(now().strftime("%Y-%m"))
    status = store.query(
        "SELECT b.category, b.amount AS budget, COALESCE(SUM(t.amount), 0) AS spent "
        "FROM budgets b LEFT JOIN transactions t ON t.category = b.category "
        "AND t.type = 'expense' AND t.ts >= ? AND t.ts < ? "
        "GROUP BY b.category, b.amount ORDER BY b.category", (start, end))
    if not status:
        tg.send(chat, "No budgets yet. Set one with /budget food 1.5jt")
        return
    lines = ["Budgets this month", ""]
    for r in status:
        percent = round(r["spent"] / r["budget"] * 100) if r["budget"] else 0
        left = r["budget"] - r["spent"]
        tail = f"{money(left)} left" if left >= 0 else f"over by {money(-left)}"
        lines.append(f"  {r['category']}: {money(r['spent'])} of "
                     f"{money(r['budget'])} ({percent}%), {tail}")
    tg.send(chat, "\n".join(lines))


def cmd_addrecurring(chat, args, ctx):
    parts = args.split(maxsplit=1)
    usage = ("Use it like this: /addrecurring 1 kos 1.25jt bca\n"
             "(day of the month, then the entry exactly as you would type it)")
    if len(parts) < 2 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= 31:
        tg.send(chat, usage)
        return
    day, text = int(parts[0]), parts[1]
    tx = parse_message(text, ctx)
    if not tx or "error" in tx:
        tg.send(chat, (tx or {}).get("error", usage))
        return
    today = now()
    passed = today.day >= min(day, calendar.monthrange(today.year, today.month)[1])
    store.run("INSERT INTO recurring (day, text, last_run) VALUES (?, ?, ?)",
              (day, text, today.strftime("%Y-%m") if passed else ""))
    when = "starting next month" if passed else "starting this month"
    tg.send(chat, f"Recurring added: '{text}' on day {day} of every month, {when}.\n"
                  "It is logged in the morning (around 07:00).")


def cmd_recurring(chat, args, ctx):
    items = store.query("SELECT * FROM recurring ORDER BY day, id")
    if not items:
        tg.send(chat, "No recurring entries. Add one with\n"
                      "/addrecurring 1 kos 1.25jt bca")
        return
    tg.send(chat, "Recurring entries\n\n" + "\n".join(
        f"  {r['id']}. day {r['day']}: {r['text']}" for r in items))


def cmd_delrecurring(chat, args, ctx):
    removed = store.run("DELETE FROM recurring WHERE id = ?",
                        (int(args),)) if args.isdigit() else None
    if not removed or not removed.rowcount:
        tg.send(chat, "Use the number from /recurring, e.g. /delrecurring 2")
        return
    tg.send(chat, f"Recurring entry {args} removed.")


def cmd_remind(chat, args, ctx):
    choice = args.lower()
    if choice not in ("on", "off"):
        state = "on" if get_meta("remind") == "on" else "off"
        tg.send(chat, f"The evening reminder is {state}.\n"
                      "Change it with /remind on or /remind off")
        return
    set_meta("remind", choice)
    tg.send(chat, "I'll nudge you around 21:00 on days with no entries."
            if choice == "on" else "Evening reminder turned off.")


def cmd_categories(chat, args, ctx):
    tg.send(chat, "Categories: " + ", ".join(categories()) +
            "\n\nTeach a word with /addword food bakso")


def cmd_addword(chat, args, ctx):
    parts = args.lower().split()
    if len(parts) != 2 or not clean_name(parts[0]) or not parts[1].isalpha():
        tg.send(chat, "Use it like this: /addword food bakso")
        return
    category, word = parts
    store.run("INSERT INTO keywords (word, category) VALUES (?, ?) "
              "ON CONFLICT(word) DO UPDATE SET category = excluded.category",
              (word, category))
    tg.send(chat, f"Got it: '{word}' now goes to {category}.")


COMMANDS = {
    "start": cmd_help, "help": cmd_help, "today": cmd_today, "week": cmd_week,
    "month": cmd_month, "balance": cmd_accounts, "accounts": cmd_accounts,
    "list": cmd_list, "search": cmd_search, "export": cmd_export,
    "backup": cmd_backup, "dashboard": cmd_dashboard, "undo": cmd_undo,
    "delete": cmd_delete, "addaccount": cmd_addaccount,
    "setbalance": cmd_setbalance, "budget": cmd_budget, "budgets": cmd_budgets,
    "addrecurring": cmd_addrecurring, "recurring": cmd_recurring,
    "delrecurring": cmd_delrecurring, "remind": cmd_remind,
    "categories": cmd_categories, "addword": cmd_addword,
}


# ================================================================ buttons
def handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    message = cb.get("message") or {}
    chat, message_id = message.get("chat", {}).get("id"), message.get("message_id")
    action, _, rest = data.partition(":")
    notice = None

    if action == "cat" and rest.isdigit():
        tg.call("editMessageReplyMarkup", chat_id=chat, message_id=message_id,
                reply_markup=category_keyboard(int(rest)))
    elif action == "back" and rest.isdigit():
        tg.call("editMessageReplyMarkup", chat_id=chat, message_id=message_id,
                reply_markup=saved_keyboard(int(rest)))
    elif action == "set":
        tx_id, _, category = rest.partition(":")
        if tx_id.isdigit() and clean_name(category):
            results = store.batch([
                ("UPDATE transactions SET category = ? WHERE id = ?",
                 (category, int(tx_id))),
                ("SELECT * FROM transactions WHERE id = ?", (int(tx_id),))])
            if results[1].rows:
                row = results[1].rows[0]
                tg.call("editMessageText", chat_id=chat, message_id=message_id,
                        text="Saved " + tx_line(row),
                        reply_markup=saved_keyboard(row["id"]))
                notice = f"Moved to {category}"
            else:
                notice = "That entry no longer exists."
    elif action == "del" and rest.isdigit():
        results = store.batch([
            ("SELECT * FROM transactions WHERE id = ?", (int(rest),)),
            ("DELETE FROM transactions WHERE id = ?", (int(rest),))])
        if results[0].rows:
            tg.call("editMessageText", chat_id=chat, message_id=message_id,
                    text="Deleted " + tx_line(results[0].rows[0]))
            notice = "Deleted"
        else:
            notice = "Already deleted"
    tg.call("answerCallbackQuery", callback_query_id=cb["id"], text=notice)


# ================================================================ entry points
def handle_message(message: dict, ctx: Context) -> None:
    chat = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        tg.send(chat, "Send me text, e.g. coffee 25k")
        return
    if text.startswith("/"):
        command, _, args = text.partition(" ")
        handler = COMMANDS.get(command[1:].split("@")[0].lower())
        if handler:
            handler(chat, args.strip(), ctx)
        else:
            tg.send(chat, "I don't know that command. See /help")
        return
    tx = parse_message(text, ctx)
    if not tx:
        tg.send(chat, "I couldn't find an amount. Try: coffee 25k")
        return
    if "error" in tx:
        tg.send(chat, tx["error"])
        return
    row, alert = save_transaction(tx)
    tg.send(chat, "Saved " + tx_line(row), keyboard_for(row))
    if alert:
        tg.send(chat, alert)


def handle_update(update: dict) -> None:
    """Called once for every update Telegram sends to the webhook."""
    message = update.get("message")
    callback = update.get("callback_query")
    sender = ((message or callback or {}).get("from") or {}).get("id")
    if not sender:
        return
    if not OWNER_ID:
        if message:
            tg.send(message["chat"]["id"],
                    f"Your Telegram user ID is {sender}.\n"
                    "Add it in Vercel as OWNER_ID and redeploy.")
        return
    if sender != OWNER_ID:
        return                                  # ignore everyone else
    try:
        ctx = load_context(update.get("update_id"))
        if ctx.duplicate:
            return                              # Telegram retried; already handled
        if message:
            handle_message(message, ctx)
        elif callback:
            handle_callback(callback)
    except Exception as error:
        print("Error handling update:", repr(error))
        try:
            tg.send(OWNER_ID, "Something went wrong. Check /list to see whether "
                              "your entry was saved before trying again.\n"
                              f"({type(error).__name__}: {error})")
        except Exception:
            pass


def run_scheduled() -> list[str]:
    """Called by the daily cron jobs. Returns what was sent."""
    ctx = load_context()
    today = now()
    messages = run_due_recurring(today, ctx)
    if reminder_due(today):
        messages.append("You haven't logged anything today. "
                        "Type an entry, e.g. coffee 25k")
    for text in messages:
        tg.send(OWNER_ID, text)
    return messages
