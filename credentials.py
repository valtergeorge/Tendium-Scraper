"""Local storage for Tendium login credentials, used to auto-fill the login
form via Playwright instead of requiring interactive manual login.

SECURITY NOTE: credentials are stored in plaintext in credentials.json,
file permissions restricted to the current user (chmod 600). This is a
pragmatic tradeoff for a local, single-user tool — the file never leaves
this machine and is only ever used to fill Tendium's own login form. If
that's not an acceptable tradeoff for your environment, use the manual
login flow instead (leave credentials.json absent) and skip this module.
"""

import json
import os
import stat

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(PROJECT_DIR, "credentials.json")


def has_credentials() -> bool:
    return os.path.isfile(CREDENTIALS_PATH)


def load_credentials():
    """Return {"email": ..., "password": ...} or None if unset/unreadable."""
    if not os.path.isfile(CREDENTIALS_PATH):
        return None
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("email") and data.get("password"):
            return {"email": data["email"], "password": data["password"]}
    except (json.JSONDecodeError, OSError):
        pass
    return None


def get_saved_email():
    creds = load_credentials()
    return creds["email"] if creds else None


def save_credentials(email: str, password: str):
    email = (email or "").strip()
    if not email or not password:
        raise ValueError("Both email and password are required.")
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump({"email": email, "password": password}, f)
    try:
        os.chmod(CREDENTIALS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # rw for owner only
    except OSError:
        pass


def clear_credentials():
    if os.path.isfile(CREDENTIALS_PATH):
        os.remove(CREDENTIALS_PATH)
