"""Bcrypt password hashing with legacy plaintext migration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import bcrypt

USER_FILE = Path(os.environ.get("CLEARSPEND_USER_DB", "users_db.json"))


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_hash(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def verify_user_password(record: dict[str, Any], password: str) -> bool:
    if record.get("pw_hash"):
        return _verify_hash(password, str(record["pw_hash"]))
    if record.get("pw") is not None:
        return str(record["pw"]) == password
    return False


def upgrade_plaintext_if_needed(
    username: str, password: str, record: dict[str, Any], accounts: dict
) -> None:
    if record.get("pw_hash") or "pw" not in record:
        return
    if str(record["pw"]) != password:
        return
    record["pw_hash"] = _hash_password(password)
    del record["pw"]
    accounts[username] = record
    save_accounts(accounts)


def register_user(
    username: str,
    password: str,
    name: str,
    org: str,
    role: str = "user",
) -> None:
    acc = load_accounts()
    acc[username] = {
        "pw_hash": _hash_password(password),
        "name": name,
        "org": org,
        "role": role,
    }
    save_accounts(acc)


def save_accounts(accounts: dict) -> None:
    with open(USER_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)


def load_accounts() -> dict:
    if USER_FILE.is_file():
        with open(USER_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "admin": {
            "pw_hash": _hash_password("uic2026"),
            "name": "Raghini Kumar",
            "org": "UIC",
            "role": "admin",
        }
    }


def init_user_file_if_missing() -> None:
    """Create users_db.json with a hashed default admin if the file does not exist."""
    if USER_FILE.is_file():
        return
    acc = {
        "admin": {
            "pw_hash": _hash_password("uic2026"),
            "name": "Raghini Kumar",
            "org": "UIC",
            "role": "admin",
        }
    }
    save_accounts(acc)


def ensure_default_admin_hashed() -> None:
    """One-time: if file has legacy plaintext admin, migrate on disk."""
    if not USER_FILE.is_file():
        acc = load_accounts()
        save_accounts(acc)
        return
    try:
        with open(USER_FILE, encoding="utf-8") as f:
            acc = json.load(f)
    except json.JSONDecodeError:
        return
    changed = False
    for uname, rec in list(acc.items()):
        if "pw" in rec and "pw_hash" not in rec:
            rec["pw_hash"] = _hash_password(str(rec["pw"]))
            del rec["pw"]
            if "role" not in rec:
                rec["role"] = "admin" if uname == "admin" else "user"
            acc[uname] = rec
            changed = True
        elif "role" not in rec:
            rec["role"] = "admin" if uname == "admin" else "user"
            acc[uname] = rec
            changed = True
    if changed:
        save_accounts(acc)


def user_role(record: dict[str, Any]) -> str:
    return str(record.get("role") or "user")


def is_admin(record: dict[str, Any]) -> bool:
    return user_role(record) == "admin"
