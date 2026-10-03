"""Web entry point for Vercel.

  POST /webhook       Telegram sends every message here
  GET  /              the dashboard (password protected)
  GET  /setup         one-time setup: creates tables, connects Telegram
  GET  /cron/<name>   called daily by Vercel (recurring entries, reminder)
"""
import hashlib
import hmac
import os
import time

from flask import Flask, jsonify, make_response, redirect, request

import bot
import dashboard
import store
import tg

app = Flask(__name__)

PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()


def _sign(key: str, label: str) -> str:
    return hmac.new(key.encode(), label.encode(), hashlib.sha256).hexdigest()


# Telegram sends this back with every webhook call, proving the call is real.
WEBHOOK_SECRET = _sign(tg.BOT_TOKEN or "unset", "webhook")


def logged_in() -> bool:
    expected = _sign(PASSWORD, "session" + tg.BOT_TOKEN)
    return bool(PASSWORD) and hmac.compare_digest(
        request.cookies.get("session", ""), expected)


def login_page(error: str = "", status: int = 200):
    html = dashboard.LOGIN_PAGE.replace(
        "__ERROR__", f'<div class="error">{error}</div>' if error else "")
    return html, status


# ---------------------------------------------------------------- Telegram
@app.post("/webhook")
def webhook():
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent, WEBHOOK_SECRET):
        return "forbidden", 403
    bot.handle_update(request.get_json(silent=True) or {})
    return "ok"            # always 200, so Telegram doesn't resend the message


# ---------------------------------------------------------------- dashboard
@app.get("/")
def home():
    if not logged_in():
        return redirect("/login")
    return dashboard.PAGE


@app.get("/api/data")
def api_data():
    if not logged_in():
        return jsonify(error="Please log in."), 401
    try:
        return jsonify(dashboard.build_data(request.args.get("month")))
    except store.DatabaseError as error:
        return jsonify(error=f"Database problem: {error}. Open /setup once.")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return login_page("No password is set. Add DASHBOARD_PASSWORD in "
                          "Vercel and redeploy.", 503)
    if request.method == "GET":
        return login_page()
    if not hmac.compare_digest(request.form.get("password", ""), PASSWORD):
        time.sleep(1)                      # slow down password guessing
        return login_page("Wrong password.", 401)
    response = make_response(redirect("/"))
    response.set_cookie("session", _sign(PASSWORD, "session" + tg.BOT_TOKEN),
                        max_age=30 * 24 * 3600, httponly=True, secure=True,
                        samesite="Lax")
    return response


@app.get("/logout")
def logout():
    response = make_response(redirect("/login"))
    response.delete_cookie("session")
    return response


# ---------------------------------------------------------------- setup
@app.get("/setup")
def setup():
    if not logged_in():
        return redirect("/login")
    lines = []

    def step(name, action):
        try:
            action()
            lines.append(f"OK    {name}")
        except Exception as error:
            lines.append(f"FAIL  {name}: {error}")

    for key in ("BOT_TOKEN", "OWNER_ID", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        if not os.environ.get(key, "").strip():
            lines.append(f"FAIL  {key} is not set in Vercel")

    base = "https://" + request.host
    step("Create database tables", store.init_db)
    step("Remember this address", lambda: bot.set_meta("base_url", base))
    step("Connect Telegram to " + base + "/webhook", lambda: tg.call(
        "setWebhook", url=base + "/webhook", secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"]))
    step("Set the command menu", lambda: tg.call("setMyCommands", commands=[
        {"command": c, "description": d} for c, d in bot.MENU]))
    step("Send a test message", lambda: tg.send(
        bot.OWNER_ID, "Your money bot is online. Try: coffee 25k"))

    ok = not any(line.startswith("FAIL") for line in lines)
    lines.append("")
    lines.append("All done. Check Telegram for the test message." if ok else
                 "Something failed. Fix it in Vercel, redeploy, and open /setup again.")
    return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}


# ---------------------------------------------------------------- daily jobs
@app.get("/cron/<name>")
def cron(name):
    if CRON_SECRET and request.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
        return "forbidden", 403
    try:
        return jsonify(sent=bot.run_scheduled())
    except Exception as error:
        print("Cron error:", repr(error))
        return jsonify(error=str(error)), 500
