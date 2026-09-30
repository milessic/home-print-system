#!/usr/bin/env python3
"""
users.py - user management for the home printing system.

Stores users in a JSON file (users.json) as:
{
  "alice": {"salt": "<hex>", "hash": "<hex>"},
  "bob":   {"salt": "<hex>", "hash": "<hex>"}
}

Passwords are hashed with PBKDF2-HMAC-SHA256 (100,000 iterations) and a
per-user random salt. Plaintext passwords are never stored.

CLI usage:
    python3 users.py --add username password
    python3 users.py --delete username
    python3 users.py --list
    python3 users.py --passwd username newpassword

This file is also imported by main.py to verify logins.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

PBKDF2_ITERATIONS = 100_000
HASH_ALGO = "sha256"


def load_users() -> dict:
    """Load the users dict from disk. Returns {} if the file doesn't exist yet."""
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users: dict) -> None:
    """Write the users dict to disk atomically-ish (write then replace)."""
    tmp_path = USERS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, sort_keys=True)
    os.replace(tmp_path, USERS_FILE)


def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(HASH_ALGO, password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return derived.hex()


def make_credentials(password: str) -> dict:
    """Create a fresh {salt, hash} pair for a new/changed password."""
    salt_hex = secrets.token_hex(16)
    return {"salt": salt_hex, "hash": _hash_password(password, salt_hex)}


def add_user(username: str, password: str, overwrite: bool = True) -> None:
    users = load_users()
    if username in users and not overwrite:
        raise ValueError(f"User '{username}' already exists")
    users[username] = make_credentials(password)
    save_users(users)


def delete_user(username: str) -> bool:
    users = load_users()
    if username not in users:
        return False
    del users[username]
    save_users(users)
    return True


def verify_password(username: str, password: str) -> bool:
    """Return True if username/password match a stored, hashed credential."""
    if not username or not password:
        return False
    users = load_users()
    record = users.get(username)
    if not record:
        return False
    try:
        expected = record["hash"]
        salt_hex = record["salt"]
    except KeyError:
        return False
    candidate = _hash_password(password, salt_hex)
    # Constant-time comparison to avoid timing side-channels.
    return secrets.compare_digest(candidate, expected)


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Manage users for the home printing system."
    )
    parser.add_argument(
        "--add",
        nargs=2,
        metavar=("USERNAME", "PASSWORD"),
        help="Add (or update) a user, e.g. --add alice hunter2",
    )
    parser.add_argument(
        "--passwd",
        nargs=2,
        metavar=("USERNAME", "NEWPASSWORD"),
        help="Change an existing user's password",
    )
    parser.add_argument("--delete", metavar="USERNAME", help="Delete a user")
    parser.add_argument("--list", action="store_true", help="List all usernames")

    args = parser.parse_args()

    if not any([args.add, args.passwd, args.delete, args.list]):
        parser.print_help()
        sys.exit(1)

    if args.add:
        username, password = args.add
        add_user(username, password)
        print(f"User '{username}' added/updated.")

    if args.passwd:
        username, password = args.passwd
        users = load_users()
        if username not in users:
            print(f"User '{username}' does not exist.", file=sys.stderr)
            sys.exit(1)
        add_user(username, password)
        print(f"Password updated for '{username}'.")

    if args.delete:
        if delete_user(args.delete):
            print(f"User '{args.delete}' deleted.")
        else:
            print(f"User '{args.delete}' not found.", file=sys.stderr)
            sys.exit(1)

    if args.list:
        users = load_users()
        if not users:
            print("No users found.")
        else:
            for name in sorted(users):
                print(name)


if __name__ == "__main__":
    _cli()
