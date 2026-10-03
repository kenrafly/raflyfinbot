"""Dashboard: gathers the numbers (one database trip) and holds the web page."""
import calendar
import re

import bot
import store


def previous_months(month: str, count: int) -> list[str]:
    """The `count` months ending with `month`, oldest first."""
    year, mon = int(month[:4]), int(month[5:7])
    result = []
    for _ in range(count):
        result.append(f"{year}-{mon:02d}")
        year, mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
    return result[::-1]


def build_data(month: str | None) -> dict:
    this_month = bot.now().strftime("%Y-%m")
    if not month or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        month = this_month
    span = bot.month_range(month)
    in_month = "ts >= ? AND ts < ?"

    (months, totals, by_category, per_day, per_month, accounts, grouped,
     budgets, recurring, transactions) = (r.rows for r in store.batch([
        ("SELECT DISTINCT substr(ts, 1, 7) AS m FROM transactions ORDER BY m DESC", ()),
        (f"SELECT type, SUM(amount) AS total FROM transactions "
         f"WHERE {in_month} GROUP BY type", span),
        (f"SELECT category, SUM(amount) AS total FROM transactions "
         f"WHERE {in_month} AND type = 'expense' GROUP BY category "
         f"ORDER BY total DESC", span),
        (f"SELECT substr(ts, 9, 2) AS d, SUM(amount) AS total FROM transactions "
         f"WHERE {in_month} AND type = 'expense' GROUP BY d", span),
        ("SELECT substr(ts, 1, 7) AS m, type, SUM(amount) AS total FROM transactions "
         "WHERE type IN ('income', 'expense') GROUP BY m, type", ()),
        ("SELECT name FROM accounts ORDER BY rowid", ()),
        (bot.BALANCE_SQL, ()),
        ("SELECT category, amount FROM budgets ORDER BY category", ()),
        ("SELECT day, text FROM recurring ORDER BY day, id", ()),
        (f"SELECT id, ts, type, amount, category, account, to_account, note "
         f"FROM transactions WHERE {in_month} ORDER BY ts DESC, id DESC", span),
    ]))

    month_list = [r["m"] for r in months]
    if this_month not in month_list:
        month_list.insert(0, this_month)
    total = {r["type"]: r["total"] for r in totals}
    income, expense = total.get("income", 0), total.get("expense", 0)

    spent_on_day = {r["d"]: r["total"] for r in per_day}
    days = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    daily = [{"day": d, "total": spent_on_day.get(f"{d:02d}", 0)}
             for d in range(1, days + 1)]

    by_month = {(r["m"], r["type"]): r["total"] for r in per_month}
    trend = [{"month": m, "income": by_month.get((m, "income"), 0),
              "expense": by_month.get((m, "expense"), 0)}
             for m in previous_months(month, 6)]

    balance = bot.balances_from([r["name"] for r in accounts], grouped)
    spent = {c["category"]: c["total"] for c in by_category}

    return {
        "months": month_list, "month": month,
        "income": income, "expense": expense, "net": income - expense,
        "balance": sum(balance.values()),
        "by_category": by_category, "daily": daily, "trend": trend,
        "accounts": [{"name": n, "balance": b} for n, b in balance.items()],
        "budgets": [{"category": r["category"], "budget": r["amount"],
                     "spent": spent.get(r["category"], 0)} for r in budgets],
        "recurring": recurring, "transactions": transactions,
    }


LOGIN_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Money dashboard - log in</title>
<style>
  :root { color-scheme: light dark; --page: #f4f3f0; --surface: #fcfcfb;
          --line: #e4e2dc; --text: #0b0b0b; --text-2: #52514e; --accent: #2a78d6; }
  @media (prefers-color-scheme: dark) {
    :root { --page: #111110; --surface: #1a1a19; --line: #30302d;
            --text: #ffffff; --text-2: #c3c2b7; --accent: #3987e5; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: var(--page); color: var(--text); padding: 16px;
         font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { background: var(--surface); border: 1px solid var(--line);
         border-radius: 12px; padding: 24px; width: 100%; max-width: 340px; }
  h1 { font-size: 20px; margin: 0 0 16px; }
  label { display: block; color: var(--text-2); font-size: 13px; margin-bottom: 6px; }
  input, button { font: inherit; width: 100%; padding: 9px 11px; border-radius: 8px;
                  box-sizing: border-box; }
  input { border: 1px solid var(--line); background: var(--page); color: var(--text); }
  button { margin-top: 14px; border: 0; background: var(--accent); color: #fff;
           font-weight: 600; cursor: pointer; }
  .error { color: var(--text); font-weight: 600; font-size: 13px; margin-top: 12px; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>Money dashboard</h1>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autofocus required
         autocomplete="current-password">
  <button type="submit">Log in</button>
  __ERROR__
</form>
</body>
</html>
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Money dashboard</title>
<style>
  :root {
    color-scheme: light;
    --page: #f4f3f0; --surface: #fcfcfb; --line: #e4e2dc; --track: #eceae5;
    --text: #0b0b0b; --text-2: #52514e; --text-3: #7a7872;
    --series-1: #2a78d6; --series-2: #eb6834; --critical: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --page: #111110; --surface: #1a1a19; --line: #30302d; --track: #2a2a27;
      --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e86;
      --series-1: #3987e5; --series-2: #d95926; --critical: #d03b3b;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page); color: var(--text);
         font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; }
  header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
           justify-content: space-between; margin-bottom: 20px; }
  h1 { font-size: 22px; margin: 0; font-weight: 650; }
  h2 { font-size: 15px; margin: 0 0 14px; font-weight: 600; }
  select, input { font: inherit; color: var(--text); background: var(--surface);
           border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; }
  .updated { color: var(--text-3); font-size: 13px; margin-right: 10px; }
  .logout { color: var(--text-2); font-size: 13px; margin-left: 12px; }
  .card { background: var(--surface); border: 1px solid var(--line);
          border-radius: 12px; padding: 18px; min-width: 0; }
  .tiles { display: grid; gap: 12px; margin-bottom: 12px;
           grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
  .tile .label { color: var(--text-2); font-size: 13px; }
  .tile .value { font-size: 24px; font-weight: 650; margin-top: 4px;
                 font-variant-numeric: tabular-nums; white-space: nowrap; }
  .tile .hint { color: var(--text-3); font-size: 12px; margin-top: 2px; }
  .grid { display: grid; gap: 12px; margin-bottom: 12px;
          grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
  .empty { color: var(--text-3); padding: 20px 0; text-align: center; }
  .note { color: var(--text-3); font-size: 12px; margin-bottom: 8px; }

  /* horizontal bars: spending by category */
  .hbar { display: grid; grid-template-columns: 96px 1fr auto; gap: 10px;
          align-items: center; padding: 5px 0; }
  .hbar .name { color: var(--text-2); overflow: hidden; text-overflow: ellipsis;
                white-space: nowrap; }
  .hbar .track { height: 14px; }
  .hbar .fill { height: 100%; min-width: 2px; background: var(--series-1);
                border-radius: 0 4px 4px 0; }
  .amount { font-variant-numeric: tabular-nums; white-space: nowrap; }

  /* columns: per day, and income vs expenses per month */
  .cols { display: flex; align-items: flex-end; gap: 2px; height: 170px;
          border-bottom: 1px solid var(--line); }
  .col { flex: 1; height: 100%; display: flex; align-items: flex-end;
         justify-content: center; gap: 2px; cursor: default; min-width: 0; }
  .col .bar { flex: 1; max-width: 28px; background: var(--series-1);
              border-radius: 4px 4px 0 0; }
  .col .bar.b2 { background: var(--series-2); }
  .col:hover .bar { filter: brightness(1.15); }
  .cols.wide { gap: 14px; }
  .axis { display: flex; gap: 2px; margin-top: 6px; color: var(--text-3);
          font-size: 11px; }
  .axis.wide { gap: 14px; }
  .axis span { flex: 1; text-align: center; min-width: 0; }
  .legend { display: flex; gap: 16px; color: var(--text-2); font-size: 13px;
            margin-bottom: 10px; }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
              margin-right: 6px; background: var(--series-1); }
  .legend i.b2 { background: var(--series-2); }

  /* budgets */
  .budget { padding: 6px 0 10px; }
  .budget .top { display: flex; justify-content: space-between; gap: 10px; }
  .budget .top span:last-child { color: var(--text-2); font-size: 13px;
                                 font-variant-numeric: tabular-nums; }
  .budget .track { height: 8px; border-radius: 4px; background: var(--track);
                   margin: 6px 0 4px; overflow: hidden; }
  .budget .fill { height: 100%; background: var(--series-1); border-radius: 4px; }
  .budget.over .fill { background: var(--critical); }
  .budget .state { font-size: 12px; color: var(--text-3); }
  .budget.over .state { color: var(--text); font-weight: 600; }

  .rowlist > div { display: flex; justify-content: space-between; gap: 10px;
                   padding: 7px 0; border-bottom: 1px solid var(--line); }
  .rowlist > div:last-child { border-bottom: 0; }
  .rowlist .sub { color: var(--text-2); }

  #tip { position: fixed; pointer-events: none; display: none; z-index: 5;
         background: var(--text); color: var(--surface); font-size: 12px;
         padding: 5px 8px; border-radius: 6px; white-space: nowrap; }

  .tablehead { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               justify-content: space-between; margin-bottom: 14px; }
  .tablehead h2 { margin: 0; }
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 560px; }
  th { text-align: left; color: var(--text-2); font-weight: 500; font-size: 13px;
       padding: 0 10px 8px 0; border-bottom: 1px solid var(--line); }
  td { padding: 9px 10px 9px 0; border-bottom: 1px solid var(--line);
       vertical-align: top; }
  tr:last-child td { border-bottom: 0; }
  td.num, th.num { text-align: right; padding-right: 0;
                   font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.when, td.id { color: var(--text-2); white-space: nowrap; }
  .tag { display: inline-block; border: 1px solid var(--line); border-radius: 999px;
         padding: 1px 8px; font-size: 12px; color: var(--text-2); }
</style>
</head>
<body>
<main>
  <header>
    <h1>Money dashboard</h1>
    <div><span class="updated" id="updated"></span>
      <label>Month <select id="month"></select></label>
      <a class="logout" href="/logout">Log out</a></div>
  </header>

  <section class="tiles">
    <div class="card tile"><div class="label">Income</div>
      <div class="value" id="income">-</div><div class="hint">selected month</div></div>
    <div class="card tile"><div class="label">Expenses</div>
      <div class="value" id="expense">-</div><div class="hint">selected month</div></div>
    <div class="card tile"><div class="label">Net</div>
      <div class="value" id="net">-</div><div class="hint">income minus expenses</div></div>
    <div class="card tile"><div class="label">Total balance</div>
      <div class="value" id="balance">-</div><div class="hint">all accounts, today</div></div>
  </section>

  <section class="grid">
    <div class="card"><h2>Spending by category</h2><div id="categories"></div></div>
    <div class="card"><h2>Spending per day</h2><div id="daily"></div></div>
  </section>

  <section class="grid">
    <div class="card"><h2>Income and expenses, last 6 months</h2><div id="trend"></div></div>
    <div class="card"><h2>Budgets</h2><div id="budgets"></div></div>
  </section>

  <section class="grid">
    <div class="card"><h2>Accounts</h2><div class="rowlist" id="accounts"></div></div>
    <div class="card"><h2>Recurring entries</h2><div class="rowlist" id="recurring"></div></div>
  </section>

  <section class="card">
    <div class="tablehead"><h2>Transactions</h2>
      <input id="filter" type="search" placeholder="Filter: coffee, food, bca..."
             aria-label="Filter transactions"></div>
    <div class="scroll"><table>
      <thead><tr><th>#</th><th>Date</th><th>Note</th><th>Category</th><th>Account</th>
        <th class="num">Amount</th></tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
    <div class="empty" id="no-rows" hidden>No transactions to show.</div>
  </section>
</main>
<div id="tip"></div>

<script>
const $ = id => document.getElementById(id);
const rp = n => (n < 0 ? "-" : "") + "Rp" + Math.abs(n).toLocaleString("id-ID");
const short = n => n >= 1e6 ? (n / 1e6).toLocaleString("id-ID", {maximumFractionDigits: 1}) + " jt"
                 : n >= 1e3 ? Math.round(n / 1e3) + " rb" : String(n);
const monthName = (m, opts) => new Date(m + "-01").toLocaleDateString("en-GB", opts);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const empty = text => el("div", "empty", text);
const tip = $("tip");
function hover(node, text) {
  node.addEventListener("mousemove", ev => {
    tip.textContent = text; tip.style.display = "block";
    const x = Math.min(ev.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = Math.max(8, x) + "px"; tip.style.top = (ev.clientY - 34) + "px";
  });
  node.addEventListener("mouseleave", () => { tip.style.display = "none"; });
}

let current = null, data = null;

function renderRows() {
  const term = $("filter").value.trim().toLowerCase();
  const shown = data.transactions.filter(t => !term ||
    [t.note, t.category, t.account, t.to_account || "", t.type].join(" ")
      .toLowerCase().includes(term));
  $("rows").replaceChildren(...shown.map(t => {
    const tr = el("tr");
    const tag = el("td"); tag.append(el("span", "tag", t.category));
    const account = t.type === "transfer" ? t.account + " → " + t.to_account : t.account;
    const amount = t.type === "income" ? "+" + rp(t.amount)
                 : t.type === "expense" ? "-" + rp(t.amount) : rp(t.amount);
    tr.append(el("td", "id", t.id),
              el("td", "when", t.ts.slice(8, 10) + "/" + t.ts.slice(5, 7) + " " + t.ts.slice(11, 16)),
              el("td", "", t.note), tag, el("td", "", account), el("td", "num", amount));
    return tr;
  }));
  $("no-rows").hidden = shown.length > 0;
}

function render(d) {
  data = d;
  const sel = $("month");
  sel.replaceChildren(...d.months.map(m => {
    const o = el("option", "", monthName(m, {month: "long", year: "numeric"}));
    o.value = m; return o;
  }));
  sel.value = d.month;

  $("income").textContent = rp(d.income);
  $("expense").textContent = rp(d.expense);
  $("net").textContent = rp(d.net);
  $("balance").textContent = rp(d.balance);

  // spending by category
  if (!d.by_category.length) {
    $("categories").replaceChildren(empty("No spending this month yet."));
  } else {
    const max = d.by_category[0].total;
    $("categories").replaceChildren(...d.by_category.map(c => {
      const row = el("div", "hbar"), track = el("div", "track"), fill = el("div", "fill");
      fill.style.width = (c.total / max * 100) + "%";
      track.append(fill);
      row.append(el("div", "name", c.category), track, el("div", "amount", rp(c.total)));
      hover(row, c.category + ": " + rp(c.total) + " (" +
            Math.round(c.total / d.expense * 100) + "% of spending)");
      return row;
    }));
  }

  // spending per day
  const peak = Math.max(...d.daily.map(x => x.total));
  if (!peak) {
    $("daily").replaceChildren(empty("No spending this month yet."));
  } else {
    const cols = el("div", "cols"), axis = el("div", "axis");
    d.daily.forEach(x => {
      const col = el("div", "col"), bar = el("div", "bar");
      bar.style.height = (x.total / peak * 100) + "%";
      bar.style.maxWidth = "none";
      col.append(bar); cols.append(col);
      hover(col, "Day " + x.day + ": " + rp(x.total));
      axis.append(el("span", "", (x.day === 1 || x.day % 5 === 0) ? x.day : ""));
    });
    $("daily").replaceChildren(el("div", "note", "Highest day: " + rp(peak)), cols, axis);
  }

  // income vs expenses, six months
  const top = Math.max(...d.trend.map(x => Math.max(x.income, x.expense)));
  if (!top) {
    $("trend").replaceChildren(empty("No income or expenses yet."));
  } else {
    const legend = el("div", "legend");
    const a = el("span"); a.append(el("i"), "Income");
    const b = el("span"); b.append(el("i", "b2"), "Expenses");
    legend.append(a, b);
    const cols = el("div", "cols wide"), axis = el("div", "axis wide");
    d.trend.forEach(x => {
      const col = el("div", "col");
      const inc = el("div", "bar"), exp = el("div", "bar b2");
      inc.style.height = (x.income / top * 100) + "%";
      exp.style.height = (x.expense / top * 100) + "%";
      col.append(inc, exp); cols.append(col);
      hover(col, monthName(x.month, {month: "short", year: "numeric"}) + ": income " +
            rp(x.income) + ", expenses " + rp(x.expense));
      axis.append(el("span", "", monthName(x.month, {month: "short"})));
    });
    $("trend").replaceChildren(legend, el("div", "note", "Top of chart: Rp" + short(top)),
                               cols, axis);
  }

  // budgets
  if (!d.budgets.length) {
    $("budgets").replaceChildren(empty("No budgets yet. In Telegram: /budget food 1.5jt"));
  } else {
    $("budgets").replaceChildren(...d.budgets.map(b => {
      const pct = b.budget ? Math.round(b.spent / b.budget * 100) : 0;
      const over = b.spent > b.budget;
      const box = el("div", "budget" + (over ? " over" : ""));
      const topRow = el("div", "top");
      topRow.append(el("span", "", b.category), el("span", "", rp(b.spent) + " of " + rp(b.budget)));
      const track = el("div", "track"), fill = el("div", "fill");
      fill.style.width = Math.min(pct, 100) + "%";
      track.append(fill);
      box.append(topRow, track, el("div", "state", over
        ? "Over budget by " + rp(b.spent - b.budget) + " (" + pct + "%)"
        : pct + "% used, " + rp(b.budget - b.spent) + " left"));
      return box;
    }));
  }

  // accounts and recurring
  $("accounts").replaceChildren(...(d.accounts.length ? d.accounts.map(a => {
    const row = el("div");
    row.append(el("span", "", a.name), el("span", "amount", rp(a.balance)));
    return row;
  }) : [empty("No accounts yet.")]));
  $("recurring").replaceChildren(...(d.recurring.length ? d.recurring.map(r => {
    const row = el("div");
    row.append(el("span", "", r.text), el("span", "sub", "day " + r.day));
    return row;
  }) : [empty("None yet. In Telegram: /addrecurring 1 kos 1.25jt")]));

  renderRows();
  $("updated").textContent = "Updated " + new Date().toLocaleTimeString("en-GB");
}

async function load(month) {
  try {
    const res = await fetch("/api/data" + (month ? "?month=" + month : ""));
    if (res.status === 401) { location.href = "/login"; return; }
    const d = await res.json();
    if (d.error) { $("updated").textContent = d.error; return; }
    current = d.month;
    render(d);
  } catch (e) {
    $("updated").textContent = "Can't reach the dashboard server";
  }
}

$("month").addEventListener("change", ev => load(ev.target.value));
$("filter").addEventListener("input", renderRows);
load();
setInterval(() => load(current), 60000);   // pick up new entries from the bot
</script>
</body>
</html>
"""
