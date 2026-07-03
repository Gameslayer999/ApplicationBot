# Parallel agent collaboration (Cursor ↔ Claude VS Code)

ApplicationBot uses a **file-based agent bus** so Cursor and the Claude VS Code
extension can work in parallel without stepping on each other. All runtime state is
git-ignored under `.agent-bus/`; the committed CLI is `applicationbot/agent_bus.py`.

## Quick start

```bash
# Once per clone (creates .agent-bus/)
python -m applicationbot.agent_bus init

# Terminal A — Cursor side (leave running while you work)
python -m applicationbot.agent_bus watch --agent cursor

# Terminal B — Claude VS Code side
python -m applicationbot.agent_bus watch --agent claude
```

When the canary bumps, the watcher prints an alert **immediately** — you do not need
to wait for the other agent's prompt to finish.

## Ritual (both agents)

At the **start** of every session:

1. `python -m applicationbot.agent_bus read --agent <cursor|claude> --unread`
2. Ack handled messages: `python -m applicationbot.agent_bus ack <id-prefix>`
3. Before editing shared files, **claim** them:
   `python -m applicationbot.agent_bus claim --agent cursor --paths applicationbot/web.py --reason "PDF export"`

At the **end** of a work chunk (or when handing off):

```bash
python -m applicationbot.agent_bus post \
  --from cursor \
  --to claude \
  --type handoff \
  --subject "PDF export wired" \
  --body "Added /export/pdf route. Needs styling pass." \
  --refs applicationbot/web.py

python -m applicationbot.agent_bus release --agent cursor --paths applicationbot/web.py
python -m applicationbot.agent_bus status --agent cursor --set-status idle
```

## Message types

| Type | Use |
|------|-----|
| `task` | Ask the other agent to do something |
| `handoff` | Work is done on your side; context for them |
| `question` / `answer` | Async Q&A |
| `done` | Task complete notification |
| `blocker` | Waiting on human or external dependency |
| `claim` / `release` | Auto-posted when paths are claimed/released |
| `ping` | Lightweight "still working" signal |

Use `--priority urgent` for blockers that need immediate attention.

Recipients: `--to cursor`, `--to claude`, or `--to broadcast` (both agents).

## Cursor integration

Project hooks in `.cursor/hooks.json`:

- **sessionStart** — injects unread inbox + active claims into Cursor's context
- **stop** — if unread mail remains, suggests reading the bus before ending

Restart Cursor after pulling hook changes. Verify in **Settings → Hooks**.

## Claude VS Code integration

Add this to your session prompt (or pin it in Claude's project instructions):

```
Before coding: run `python -m applicationbot.agent_bus context --agent claude` and follow it.
After a chunk of work: post a handoff to cursor and release your claims.
Keep `python -m applicationbot.agent_bus watch --agent claude` running in a terminal tab.
```

`CLAUDE.md` includes a short **Parallel agents** section with the same ritual.

## Layout (git-ignored)

```
.agent-bus/
  canary.json      # sequence counters per agent (poll target)
  notify/          # touched on each post (mtime watch fallback)
    cursor
    claude
  inbox/           # pending messages (*.json)
  archive/         # ack'd messages
  claims.json      # who owns which paths
  manifest.json    # agent status (idle / working / blocked)
```

## CLI reference

```bash
scripts/agent-bus init
scripts/agent-bus post --from claude --to cursor --type task --subject "..." --body "..."
scripts/agent-bus read --agent cursor [--unread]
scripts/agent-bus ack <message-id-prefix>
scripts/agent-bus claim --agent claude --paths path/to/file --reason "..."
scripts/agent-bus release --agent claude [--paths path/to/file]
scripts/agent-bus status [--agent cursor --set-status working]
scripts/agent-bus context --agent claude
scripts/agent-bus watch --agent claude [--interval 1.0]
```

## Avoiding conflicts

1. **Claim before edit** — the other agent's hook/context shows your claims.
2. **Small handoffs** — one feature slice per message, with `--refs` pointing at files.
3. **Ack when done** — keeps inbox noise down; archived messages stay in `.agent-bus/archive/`.
4. **Broadcast blockers** — `--to broadcast --type blocker --priority urgent` when stopped.

## Limitations

- This is **near-real-time** (default 1s poll), not a websocket. Good enough for two
  humans+agents in the same repo.
- Nothing prevents an agent from ignoring the bus — discipline + hooks + watch terminals
  make it practical.
- `.agent-bus/` is local only; not shared across machines unless you sync it yourself
  (intentionally git-ignored).
