#!/usr/bin/env python3
"""
Local web dashboard for managing the Tendium scraper.

Run it with the project's venv:
    .venv/bin/python manage.py

Then open http://127.0.0.1:5151 in your browser.

From the dashboard you can:
    - Start / stop a scraper run.
    - Complete the one-time manual login (the scraper opens a real, visible
      browser window on this machine; once you've logged in, click
      "I've logged in" in the dashboard to let the run continue and save
      the session).
    - Edit scraper settings (headless mode, target URL, CSV write mode,
      timeouts) — stored in config.json, shared with tendium_scraper.py.
    - View the live log output of the current/last run.
    - Browse the most recently scraped tenders from tendium_tenders.csv.
    - Reset the saved session to force a fresh manual login.

This UI only ever runs the scraper as a subprocess of the same Python
interpreter (venv) manage.py itself is running under, so no separate
installation step is needed.
"""

import csv
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

import credentials
import scraper_config

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRAPER_SCRIPT = os.path.join(PROJECT_DIR, "tendium_scraper.py")
SESSION_DIR = os.path.join(PROJECT_DIR, "tendium_session")
OUTPUT_CSV = os.path.join(PROJECT_DIR, "tendium_tenders.csv")
LOG_PATH = os.path.join(PROJECT_DIR, "run.log")
LOGIN_SIGNAL_FILE = os.path.join(PROJECT_DIR, "login_confirmed.flag")

LOG_TAIL_LINES = 400
TABLE_ROW_LIMIT = 200

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "process": None,       # subprocess.Popen or None
    "log_file": None,      # open file handle the subprocess writes to
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "needs_login": False,
}


def session_initialized() -> bool:
    return os.path.isdir(SESSION_DIR) and len(os.listdir(SESSION_DIR)) > 0


def _refresh_state_locked():
    """Detect if a running subprocess has finished; update state if so."""
    proc = _state["process"]
    if proc is not None:
        exit_code = proc.poll()
        if exit_code is not None:
            _state["exit_code"] = exit_code
            _state["finished_at"] = time.time()
            _state["needs_login"] = False
            _state["process"] = None
            if _state["log_file"]:
                try:
                    _state["log_file"].close()
                except OSError:
                    pass
                _state["log_file"] = None


def get_status() -> dict:
    with _lock:
        _refresh_state_locked()
        running = _state["process"] is not None
        return {
            "running": running,
            "needs_login": _state["needs_login"] and running,
            "started_at": _state["started_at"],
            "finished_at": _state["finished_at"],
            "exit_code": _state["exit_code"],
            "session_initialized": session_initialized(),
        }


def start_run() -> bool:
    """Start the scraper subprocess. Returns False if one is already running."""
    with _lock:
        _refresh_state_locked()
        if _state["process"] is not None:
            return False

        if os.path.isfile(LOGIN_SIGNAL_FILE):
            os.remove(LOGIN_SIGNAL_FILE)

        needs_login = not session_initialized()

        log_file = open(LOG_PATH, "a", encoding="utf-8")
        log_file.write(f"\n{'=' * 70}\nRun started at {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'=' * 70}\n")
        log_file.flush()

        proc = subprocess.Popen(
            [sys.executable, SCRAPER_SCRIPT, "--login-signal-file", LOGIN_SIGNAL_FILE],
            cwd=PROJECT_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

        _state["process"] = proc
        _state["log_file"] = log_file
        _state["started_at"] = time.time()
        _state["finished_at"] = None
        _state["exit_code"] = None
        _state["needs_login"] = needs_login
        return True


def stop_run() -> bool:
    with _lock:
        _refresh_state_locked()
        proc = _state["process"]
        if proc is None:
            return False
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _refresh_state_locked()
        return True


def confirm_login() -> bool:
    with _lock:
        if _state["process"] is None:
            return False
        with open(LOGIN_SIGNAL_FILE, "w", encoding="utf-8") as f:
            f.write("confirmed\n")
        return True


def read_log_tail(n=LOG_TAIL_LINES) -> str:
    if not os.path.isfile(LOG_PATH):
        return ""
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:])


_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y")


def _try_parse_date(value):
    if not value:
        return None
    value = value.strip()[:19]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def annotate_renewal_status(row, now, window_days):
    """
    Attach a human-readable renewal_label + renewal_css (badge color class)
    to a tender/contract row based on contract_end, so both the initial
    server-render and the JS refresh path can just display these directly
    instead of duplicating date math in two languages.
    """
    dt = _try_parse_date(row.get("contract_end"))
    if not dt:
        row["renewal_label"] = "—"
        row["renewal_css"] = "idle"
        return row

    days = (dt - now).days
    if days < 0:
        row["renewal_label"] = f"Expired {abs(days)}d ago"
        row["renewal_css"] = "err"
    elif days <= window_days:
        row["renewal_label"] = f"{days}d left"
        row["renewal_css"] = "running"
    else:
        row["renewal_label"] = f"{days}d left"
        row["renewal_css"] = "ok"
    return row


def read_tenders(limit=TABLE_ROW_LIMIT):
    """
    Read scraped rows, most urgent contract renewal first: sorted by
    contract_end ascending (an already-expired contract sorts before one
    expiring next month, which sorts before one expiring next year), with
    rows that have no parseable contract_end pushed to the bottom, most
    recently scraped first among those.
    """
    if not os.path.isfile(OUTPUT_CSV):
        return []
    with open(OUTPUT_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    rows.reverse()  # most recently scraped first (stable secondary order below)
    rows.sort(key=lambda r: (_try_parse_date(r.get("contract_end")) is None, _try_parse_date(r.get("contract_end")) or datetime.max))
    return rows[:limit]


def compute_stats(tenders, window_days):
    """
    Summary numbers for the dashboard's stat tiles, focused on contract
    renewal tracking: how many awarded contracts are already expired or
    about to expire within `window_days`, so a new procurement can be
    started with enough lead time.
    """
    tags = set()
    companies = set()
    expired = 0
    expiring_soon = 0
    now = datetime.now()
    soon_cutoff = now + timedelta(days=window_days)

    for t in tenders:
        for tag in (t.get("tags") or "").split("|"):
            if tag:
                tags.add(tag)
        if t.get("contracted_company"):
            companies.add(t["contracted_company"])
        contract_end = _try_parse_date(t.get("contract_end"))
        if contract_end:
            if contract_end < now:
                expired += 1
            elif contract_end <= soon_cutoff:
                expiring_soon += 1

    return {
        "total": len(tenders),
        "expired": expired,
        "expiring_soon": expiring_soon,
        "distinct_tags": len(tags),
        "distinct_companies": len(companies),
    }


@app.route("/")
def index():
    cfg = scraper_config.load_config()
    window_days = cfg.get("renewal_alert_days", 180)
    now = datetime.now()
    all_tenders = read_tenders(limit=None)
    for row in all_tenders:
        annotate_renewal_status(row, now, window_days)
    return render_template(
        "index.html",
        config=cfg,
        status=get_status(),
        tenders=all_tenders[:TABLE_ROW_LIMIT],
        stats=compute_stats(all_tenders, window_days),
        csv_exists=os.path.isfile(OUTPUT_CSV),
        active_page="dashboard",
    )


@app.route("/settings")
def settings_page():
    return render_template(
        "settings.html",
        config=scraper_config.load_config(),
        status=get_status(),
        log_tail=read_log_tail(),
        login_email=credentials.get_saved_email(),
        active_page="settings",
    )


@app.route("/run", methods=["POST"])
def run():
    start_run()
    return redirect(url_for("index"))


@app.route("/credentials", methods=["POST"])
def save_credentials_route():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not password:
        existing = credentials.load_credentials()
        password = existing["password"] if existing else ""
    if email and password:
        credentials.save_credentials(email, password)
    return redirect(url_for("settings_page"))


@app.route("/credentials/clear", methods=["POST"])
def clear_credentials_route():
    credentials.clear_credentials()
    return redirect(url_for("settings_page"))


@app.route("/stop", methods=["POST"])
def stop():
    stop_run()
    return redirect(url_for("index"))


@app.route("/confirm-login", methods=["POST"])
def confirm_login_route():
    confirm_login()
    return redirect(url_for("index"))


@app.route("/reset-session", methods=["POST"])
def reset_session():
    with _lock:
        _refresh_state_locked()
        if _state["process"] is not None:
            return redirect(url_for("index"))  # refuse while a run is active
    if os.path.isdir(SESSION_DIR):
        for root, dirs, files in os.walk(SESSION_DIR, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except OSError:
                    pass
    return redirect(url_for("index"))


@app.route("/config", methods=["POST"])
def update_config():
    form = request.form
    scraper_config.save_config({
        "headless": "headless" in form,  # checkbox present == checked
        "target_url": form.get("target_url", ""),
        "csv_write_mode": form.get("csv_write_mode", "append"),
        "nav_timeout_ms": form.get("nav_timeout_ms", ""),
        "default_timeout_ms": form.get("default_timeout_ms", ""),
        "fetch_details": "fetch_details" in form,
        "max_detail_pages": form.get("max_detail_pages", "0"),
        "renewal_alert_days": form.get("renewal_alert_days", "180"),
    })
    return redirect(url_for("settings_page"))


@app.route("/download/csv")
def download_csv():
    if not os.path.isfile(OUTPUT_CSV):
        return "No CSV file yet — run the scraper first.", 404
    return send_file(OUTPUT_CSV, as_attachment=True, download_name="tendium_tenders.csv")


@app.route("/api/status")
def api_status():
    return jsonify(get_status())


@app.route("/api/log")
def api_log():
    return jsonify({"log": read_log_tail()})


@app.route("/api/tenders")
def api_tenders():
    cfg = scraper_config.load_config()
    window_days = cfg.get("renewal_alert_days", 180)
    now = datetime.now()
    all_tenders = read_tenders(limit=None)
    for row in all_tenders:
        annotate_renewal_status(row, now, window_days)
    return jsonify({
        "tenders": all_tenders[:TABLE_ROW_LIMIT],
        "stats": compute_stats(all_tenders, window_days),
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5151, debug=False)
