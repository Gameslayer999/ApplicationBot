"""Per-tenant credential store for account-gated portals — Workday first (decision 050).

Account-gated ATSs (Workday) require a candidate account per employer *tenant* (each company
runs its own Workday instance at `{tenant}.myworkdayjobs.com`). The bot creates/uses one
account per tenant; the settled design (NEXT_STEPS, 2026-07-09) is:

  • **Passwords live in the OS keychain** (`keyring`), never in plaintext YAML (Guideline #12).
  • A git-ignored, non-secret **index** (`profile/workday_accounts.json`) records which tenants
    have an account and the login email — so the store is listable (keyring can't enumerate)
    and the user can see, and manually log into, every Workday the bot made an account on.

The keychain entry stores `{"email", "password"}` as JSON under one (service, tenant) key. The
`backend` is injected (defaults to the `keyring` module) so tests run against an in-memory fake
with no real keychain. CLI: `python -m applicationbot.credentials list|get <tenant>|delete <tenant>`.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SERVICE = "applicationbot-workday"
DEFAULT_INDEX = Path("profile/workday_accounts.json")


@dataclass
class Account:
    tenant: str    # the tenant host, e.g. "acme.wd1.myworkdayjobs.com"
    email: str
    password: str


def tenant_of(url_or_host: str) -> str:
    """The Workday tenant key for a URL or host — the hostname, lowercased. Each employer's
    Workday lives on its own host, so the host is the natural per-account key."""
    s = (url_or_host or "").strip()
    if "://" in s:
        s = urllib.parse.urlsplit(s).netloc
    elif "/" in s:
        s = s.split("/", 1)[0]
    return s.lower().strip()


def _keyring():
    import keyring  # lazy: only needed when actually touching the keychain

    return keyring


def _load_index(index_path: str | Path) -> dict:
    p = Path(index_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(index: dict, index_path: str | Path) -> None:
    p = Path(index_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_account(account: Account, *, backend=None, index_path: str | Path = DEFAULT_INDEX) -> None:
    """Store the tenant's password in the keychain and record tenant→email in the index."""
    backend = backend or _keyring()
    backend.set_password(SERVICE, account.tenant,
                         json.dumps({"email": account.email, "password": account.password}))
    index = _load_index(index_path)
    index[account.tenant] = account.email
    _save_index(index, index_path)


def get_account(tenant: str, *, backend=None, index_path: str | Path = DEFAULT_INDEX) -> Optional[Account]:
    """The stored Account for a tenant (host or URL accepted), or None if none saved."""
    backend = backend or _keyring()
    tenant = tenant_of(tenant)
    try:
        raw = backend.get_password(SERVICE, tenant)
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    return Account(tenant=tenant, email=d.get("email", ""), password=d.get("password", ""))


def has_account(tenant: str, *, backend=None, index_path: str | Path = DEFAULT_INDEX) -> bool:
    return get_account(tenant, backend=backend, index_path=index_path) is not None


def list_accounts(index_path: str | Path = DEFAULT_INDEX) -> list[tuple[str, str]]:
    """(tenant, email) for every stored account, from the non-secret index — no keychain reads,
    so it never prompts. Passwords are fetched only on an explicit `get`."""
    return sorted(_load_index(index_path).items())


def delete_account(tenant: str, *, backend=None, index_path: str | Path = DEFAULT_INDEX) -> bool:
    backend = backend or _keyring()
    tenant = tenant_of(tenant)
    existed = tenant in _load_index(index_path)
    try:
        backend.delete_password(SERVICE, tenant)
    except Exception:
        pass
    index = _load_index(index_path)
    if tenant in index:
        del index[tenant]
        _save_index(index, index_path)
    return existed


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Manage saved Workday logins (passwords in the OS keychain).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="List tenants with a saved account (no passwords).")
    g = sub.add_parser("get", help="Print the email + password for a tenant (reveals a secret).")
    g.add_argument("tenant")
    d = sub.add_parser("delete", help="Remove a tenant's saved account.")
    d.add_argument("tenant")
    args = ap.parse_args(argv)

    if args.cmd == "get":
        acct = get_account(args.tenant)
        if not acct:
            print(f"No saved account for {tenant_of(args.tenant)}.")
            return 1
        print(f"tenant:   {acct.tenant}\nemail:    {acct.email}\npassword: {acct.password}")
        return 0
    if args.cmd == "delete":
        ok = delete_account(args.tenant)
        print("Deleted." if ok else f"No saved account for {tenant_of(args.tenant)}.")
        return 0
    rows = list_accounts()
    if not rows:
        print("No saved Workday accounts yet.")
        return 0
    print(f"{len(rows)} saved Workday account(s):")
    for tenant, email in rows:
        print(f"  {tenant}  ({email})")
    print("\nReveal a password with:  python -m applicationbot.credentials get <tenant>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
