"""File-based message bus for parallel Cursor + Claude VS Code collaboration.

All runtime state lives in git-ignored ``.agent-bus/``. Committed code here defines
the schema and CLI; see ``docs/AGENT_COLLAB.md`` for usage.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

AgentName = Literal["cursor", "claude", "human", "broadcast"]
MessageType = Literal[
    "task",
    "handoff",
    "question",
    "answer",
    "done",
    "blocker",
    "claim",
    "release",
    "ping",
]
Priority = Literal["normal", "urgent"]

AGENTS = ("cursor", "claude", "human")
MESSAGE_TYPES = (
    "task",
    "handoff",
    "question",
    "answer",
    "done",
    "blocker",
    "claim",
    "release",
    "ping",
)


def repo_root(start: Path | None = None) -> Path:
    """Walk up from *start* (or cwd) to find the repo root (contains CLAUDE.md)."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    return here


def bus_dir(root: Path | None = None) -> Path:
    return repo_root(root) / ".agent-bus"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_bus(root: Path | None = None) -> Path:
    """Create ``.agent-bus/`` layout if missing; return bus directory."""
    root_path = repo_root(root)
    base = bus_dir(root_path)
    for sub in ("inbox", "archive", "notify"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    canary = base / "canary.json"
    if not canary.is_file():
        _write_json(
            canary,
            {"seq": {a: 0 for a in AGENTS}, "broadcast": 0, "updated_at": _now_iso()},
        )
    claims = base / "claims.json"
    if not claims.is_file():
        _write_json(claims, {"claims": []})
    manifest = base / "manifest.json"
    if not manifest.is_file():
        _write_json(
            manifest,
            {
                "created_at": _now_iso(),
                "agents": {
                    "cursor": {"status": "idle", "updated_at": _now_iso()},
                    "claude": {"status": "idle", "updated_at": _now_iso()},
                },
            },
        )
    return base


@dataclass
class Message:
    id: str
    from_agent: str
    to_agent: str
    type: str
    subject: str
    body: str
    priority: str = "normal"
    refs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    read_at: str | None = None
    ack_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from": self.from_agent,
            "to": self.to_agent,
            "type": self.type,
            "priority": self.priority,
            "subject": self.subject,
            "body": self.body,
            "refs": self.refs,
            "created_at": self.created_at,
            "read_at": self.read_at,
            "ack_at": self.ack_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            id=data["id"],
            from_agent=data["from"],
            to_agent=data["to"],
            type=data["type"],
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            priority=data.get("priority", "normal"),
            refs=list(data.get("refs") or []),
            created_at=data.get("created_at", _now_iso()),
            read_at=data.get("read_at"),
            ack_at=data.get("ack_at"),
        )


def _message_path(base: Path, msg: Message) -> Path:
    stamp = msg.created_at.replace(":", "").replace("-", "")[:15]
    safe_subj = "".join(c if c.isalnum() else "-" for c in msg.subject[:40]).strip("-")
    name = f"{stamp}-{msg.from_agent}-to-{msg.to_agent}-{msg.id[:8]}"
    if safe_subj:
        name = f"{name}-{safe_subj}"
    return base / "inbox" / f"{name}.json"


def _bump_canary(base: Path, recipients: set[str]) -> None:
    canary_path = base / "canary.json"
    canary = _read_json(canary_path, {"seq": {}, "broadcast": 0})
    seq = canary.setdefault("seq", {})
    for agent in recipients:
        if agent == "broadcast":
            canary["broadcast"] = int(canary.get("broadcast", 0)) + 1
            for name in AGENTS:
                seq[name] = int(seq.get(name, 0)) + 1
        else:
            seq[agent] = int(seq.get(agent, 0)) + 1
    canary["updated_at"] = _now_iso()
    _write_json(canary_path, canary)
    notify_dir = base / "notify"
    notify_dir.mkdir(parents=True, exist_ok=True)
    for agent in recipients:
        if agent == "broadcast":
            for name in AGENTS:
                (notify_dir / name).write_text(canary["updated_at"], encoding="utf-8")
        else:
            (notify_dir / agent).write_text(canary["updated_at"], encoding="utf-8")


def post_message(
    *,
    from_agent: str,
    to_agent: str,
    msg_type: str,
    subject: str,
    body: str = "",
    priority: str = "normal",
    refs: list[str] | None = None,
    root: Path | None = None,
) -> Message:
    base = ensure_bus(root)
    msg = Message(
        id=str(uuid.uuid4()),
        from_agent=from_agent,
        to_agent=to_agent,
        type=msg_type,
        subject=subject,
        body=body,
        priority=priority,
        refs=refs or [],
    )
    _write_json(_message_path(base, msg), msg.to_dict())
    recipients = {to_agent} if to_agent != "broadcast" else {"broadcast"}
    _bump_canary(base, recipients)
    return msg


def _iter_messages(base: Path, include_archived: bool = False):
    dirs = [base / "inbox"]
    if include_archived:
        dirs.append(base / "archive")
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                yield path, Message.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError):
                continue


def list_messages(
    *,
    agent: str | None = None,
    unread_only: bool = False,
    include_archived: bool = False,
    root: Path | None = None,
) -> list[tuple[Path, Message]]:
    base = ensure_bus(root)
    out: list[tuple[Path, Message]] = []
    for path, msg in _iter_messages(base, include_archived=include_archived):
        if agent and msg.to_agent not in (agent, "broadcast"):
            continue
        if unread_only and msg.read_at is not None:
            continue
        out.append((path, msg))
    out.sort(key=lambda item: item[1].created_at)
    return out


def mark_read(message_id: str, *, root: Path | None = None) -> bool:
    base = ensure_bus(root)
    for path, msg in _iter_messages(base, include_archived=True):
        if msg.id == message_id or msg.id.startswith(message_id):
            if msg.read_at is None:
                msg.read_at = _now_iso()
                _write_json(path, msg.to_dict())
            return True
    return False


def ack_message(message_id: str, *, root: Path | None = None) -> bool:
    base = ensure_bus(root)
    for path, msg in _iter_messages(base, include_archived=True):
        if msg.id == message_id or msg.id.startswith(message_id):
            msg.ack_at = _now_iso()
            if msg.read_at is None:
                msg.read_at = _now_iso()
            _write_json(path, msg.to_dict())
            archive = base / "archive" / path.name
            if path.parent.name == "inbox":
                archive.parent.mkdir(parents=True, exist_ok=True)
                path.replace(archive)
            return True
    return False


def set_status(agent: str, status: str, *, root: Path | None = None) -> None:
    base = ensure_bus(root)
    manifest_path = base / "manifest.json"
    manifest = _read_json(manifest_path, {"agents": {}})
    agents = manifest.setdefault("agents", {})
    agents[agent] = {"status": status, "updated_at": _now_iso()}
    _write_json(manifest_path, manifest)


def get_canary_seq(agent: str, *, root: Path | None = None) -> int:
    base = ensure_bus(root)
    canary = _read_json(base / "canary.json", {"seq": {}})
    return int(canary.get("seq", {}).get(agent, 0))


def claim_paths(
    agent: str,
    paths: list[str],
    reason: str,
    *,
    root: Path | None = None,
) -> None:
    base = ensure_bus(root)
    claims_path = base / "claims.json"
    data = _read_json(claims_path, {"claims": []})
    normalized = [p.strip().replace("\\", "/") for p in paths if p.strip()]
    existing = [
        c
        for c in data.get("claims", [])
        if not (c.get("agent") == agent and set(c.get("paths", [])) == set(normalized))
    ]
    existing.append(
        {
            "agent": agent,
            "paths": normalized,
            "reason": reason,
            "since": _now_iso(),
        }
    )
    data["claims"] = existing
    _write_json(claims_path, data)
    post_message(
        from_agent=agent,
        to_agent="broadcast",
        msg_type="claim",
        subject=reason or f"Claim on {', '.join(normalized)}",
        body="\n".join(normalized),
        refs=normalized,
        root=root,
    )


def release_claims(agent: str, paths: list[str] | None = None, *, root: Path | None = None) -> int:
    base = ensure_bus(root)
    claims_path = base / "claims.json"
    data = _read_json(claims_path, {"claims": []})
    released: list[str] = []
    kept: list[dict[str, Any]] = []
    path_set = {p.strip().replace("\\", "/") for p in (paths or [])}
    for claim in data.get("claims", []):
        if claim.get("agent") != agent:
            kept.append(claim)
            continue
        claim_paths_set = set(claim.get("paths") or [])
        if paths and not (claim_paths_set & path_set):
            kept.append(claim)
            continue
        released.extend(sorted(claim_paths_set))
    data["claims"] = kept
    _write_json(claims_path, data)
    if released:
        post_message(
            from_agent=agent,
            to_agent="broadcast",
            msg_type="release",
            subject="Released file claims",
            body="\n".join(released),
            refs=released,
            root=root,
        )
    return len(released)


def format_inbox(agent: str, *, unread_only: bool = False, root: Path | None = None) -> str:
    rows = list_messages(agent=agent, unread_only=unread_only, root=root)
    if not rows:
        return f"No {'unread ' if unread_only else ''}messages for {agent}."
    lines = [f"=== Agent bus inbox ({agent}) — {len(rows)} message(s) ==="]
    for _path, msg in rows:
        flag = " [UNREAD]" if msg.read_at is None else ""
        urgent = " [URGENT]" if msg.priority == "urgent" else ""
        lines.append(
            f"\n[{msg.id[:8]}] {msg.type}{urgent}{flag} from {msg.from_agent}\n"
            f"  {msg.subject}\n"
            f"  {msg.body[:500]}{'…' if len(msg.body) > 500 else ''}"
        )
        if msg.refs:
            lines.append(f"  refs: {', '.join(msg.refs)}")
    return "\n".join(lines)


def format_context_for_agent(agent: str, *, root: Path | None = None) -> str:
    """Compact block for injection into an agent session (hooks / CLAUDE.md ritual)."""
    base = ensure_bus(root)
    unread = list_messages(agent=agent, unread_only=True, root=root)
    claims = _read_json(base / "claims.json", {"claims": []}).get("claims", [])
    manifest = _read_json(base / "manifest.json", {"agents": {}})
    other = "claude" if agent == "cursor" else "cursor"
    other_status = manifest.get("agents", {}).get(other, {}).get("status", "unknown")

    parts = [
        "## Agent bus (parallel Cursor ↔ Claude)",
        f"You are **{agent}**. Check `.agent-bus/` via `python -m applicationbot.agent_bus read --agent {agent}`.",
        f"Post updates: `python -m applicationbot.agent_bus post --from {agent} --to {other} --type handoff --subject '…' --body '…'`.",
        f"Other agent ({other}) status: {other_status}.",
    ]
    if unread:
        parts.append(f"\n**{len(unread)} unread message(s) — read and ack before overlapping work:**")
        for _path, msg in unread[:8]:
            parts.append(
                f"- [{msg.id[:8]}] **{msg.type}** from {msg.from_agent}: {msg.subject}"
            )
        if len(unread) > 8:
            parts.append(f"- … and {len(unread) - 8} more")
    else:
        parts.append("\nNo unread messages.")

    active_claims = [c for c in claims if c.get("agent") != agent]
    if active_claims:
        parts.append("\n**Paths claimed by the other agent (avoid editing):**")
        for claim in active_claims:
            paths = ", ".join(claim.get("paths") or [])
            parts.append(f"- {claim.get('agent')}: {paths} — {claim.get('reason', '')}")
    my_claims = [c for c in claims if c.get("agent") == agent]
    if my_claims:
        parts.append("\n**Your active claims:**")
        for claim in my_claims:
            parts.append(f"- {', '.join(claim.get('paths') or [])}")

    parts.append(
        "\nClaim before editing shared files: "
        f"`python -m applicationbot.agent_bus claim --agent {agent} --paths path/to/file --reason '…'`"
    )
    return "\n".join(parts)


def watch_agent(agent: str, *, interval: float = 1.0, root: Path | None = None) -> None:
    """Poll canary seq; print a line when new mail arrives (for a side terminal)."""
    base = ensure_bus(root)
    notify_file = base / "notify" / agent
    last_seq = get_canary_seq(agent, root=root)
    last_mtime = notify_file.stat().st_mtime if notify_file.is_file() else 0.0
    print(f"Watching agent bus for {agent} (seq={last_seq}). Ctrl+C to stop.", flush=True)
    while True:
        time.sleep(interval)
        seq = get_canary_seq(agent, root=root)
        mtime = notify_file.stat().st_mtime if notify_file.is_file() else 0.0
        if seq != last_seq or mtime != last_mtime:
            last_seq, last_mtime = seq, mtime
            unread = list_messages(agent=agent, unread_only=True, root=root)
            print(
                f"\n[{_now_iso()}] Canary bumped (seq={seq}). "
                f"{len(unread)} unread — run: python -m applicationbot.agent_bus read --agent {agent}",
                flush=True,
            )
            for _path, msg in unread[-3:]:
                print(f"  • [{msg.id[:8]}] {msg.from_agent}: {msg.subject}", flush=True)


def _cmd_init(_args: argparse.Namespace) -> int:
    base = ensure_bus()
    print(f"Agent bus ready at {base}")
    return 0


def _cmd_post(args: argparse.Namespace) -> int:
    msg = post_message(
        from_agent=args.from_agent,
        to_agent=args.to_agent,
        msg_type=args.type,
        subject=args.subject,
        body=args.body or "",
        priority=args.priority,
        refs=args.refs or [],
    )
    print(f"Posted {msg.id} → {args.to_agent}")
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    print(format_inbox(args.agent, unread_only=args.unread, root=repo_root()))
    return 0


def _cmd_ack(args: argparse.Namespace) -> int:
    if not ack_message(args.id, root=repo_root()):
        print(f"No message matching id {args.id!r}", file=sys.stderr)
        return 1
    print(f"Acknowledged {args.id}")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    claim_paths(args.agent, args.paths, args.reason or "", root=repo_root())
    print(f"Claimed {len(args.paths)} path(s) for {args.agent}")
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    n = release_claims(args.agent, args.paths, root=repo_root())
    print(f"Released {n} path(s) for {args.agent}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    base = ensure_bus()
    canary = _read_json(base / "canary.json", {})
    manifest = _read_json(base / "manifest.json", {})
    claims = _read_json(base / "claims.json", {"claims": []})
    unread_cursor = len(list_messages(agent="cursor", unread_only=True))
    unread_claude = len(list_messages(agent="claude", unread_only=True))
    print("Agent bus status")
    print(f"  directory: {base}")
    print(f"  canary seq: {canary.get('seq', {})}")
    print(f"  unread: cursor={unread_cursor}, claude={unread_claude}")
    print(f"  agents: {json.dumps(manifest.get('agents', {}), indent=4)}")
    print(f"  claims: {len(claims.get('claims', []))} active")
    if args.agent and args.set_status:
        set_status(args.agent, args.set_status)
        print(f"  set {args.agent} → {args.set_status}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    print(format_context_for_agent(args.agent, root=repo_root()))
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        watch_agent(args.agent, interval=args.interval, root=repo_root())
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ApplicationBot agent collaboration bus")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create .agent-bus/ if missing")

    p_post = sub.add_parser("post", help="Send a message")
    p_post.add_argument("--from", dest="from_agent", required=True, choices=AGENTS)
    p_post.add_argument("--to", dest="to_agent", required=True, choices=[*AGENTS, "broadcast"])
    p_post.add_argument("--type", dest="type", required=True, choices=MESSAGE_TYPES)
    p_post.add_argument("--subject", required=True)
    p_post.add_argument("--body", default="")
    p_post.add_argument("--priority", default="normal", choices=("normal", "urgent"))
    p_post.add_argument("--refs", nargs="*", default=[])

    p_read = sub.add_parser("read", help="Show inbox")
    p_read.add_argument("--agent", required=True, choices=AGENTS)
    p_read.add_argument("--unread", action="store_true")

    p_ack = sub.add_parser("ack", help="Acknowledge and archive a message")
    p_ack.add_argument("id", help="Message id or prefix")

    p_claim = sub.add_parser("claim", help="Claim paths to avoid edit conflicts")
    p_claim.add_argument("--agent", required=True, choices=AGENTS)
    p_claim.add_argument("--paths", nargs="+", required=True)
    p_claim.add_argument("--reason", default="")

    p_release = sub.add_parser("release", help="Release path claims")
    p_release.add_argument("--agent", required=True, choices=AGENTS)
    p_release.add_argument("--paths", nargs="*", default=[])

    p_status = sub.add_parser("status", help="Show bus state")
    p_status.add_argument("--agent", choices=AGENTS)
    p_status.add_argument("--set-status", dest="set_status")

    p_context = sub.add_parser("context", help="Print session context block for an agent")
    p_context.add_argument("--agent", required=True, choices=AGENTS)

    p_watch = sub.add_parser("watch", help="Poll canary; alert on new mail")
    p_watch.add_argument("--agent", required=True, choices=AGENTS)
    p_watch.add_argument("--interval", type=float, default=1.0)

    args = parser.parse_args(argv)
    handlers = {
        "init": _cmd_init,
        "post": _cmd_post,
        "read": _cmd_read,
        "ack": _cmd_ack,
        "claim": _cmd_claim,
        "release": _cmd_release,
        "status": _cmd_status,
        "context": _cmd_context,
        "watch": _cmd_watch,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
