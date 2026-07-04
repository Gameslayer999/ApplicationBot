"""Repo map — a compact, always-fresh structural index of the codebase.

Purpose (CLAUDE.md Agent Guideline #0): make every code change faster by giving an
agent a one-shot map of the repo — every module, its classes/functions with line
numbers, and which first-party modules depend on which — instead of grepping around to
re-orient each session. Structural, not semantic: it is parsed fresh on every run, so it
never goes stale the way an embedding index would (see DECISIONS.md).

Python-only today, via the stdlib `ast` module (zero dependencies). If standalone
non-Python source (`.js`, `.ts`, `.html`) ever lands in the repo, add a parser for it in
`_symbols_for()` — that is the single dispatch point; everything downstream is
language-agnostic.

Usage:
    python -m applicationbot.repo_map            # print the map to stdout
    python -m applicationbot.repo_map --out .repo-map.md
    python -m applicationbot.repo_map --json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from dataclasses import dataclass, field, asdict

# Directories never worth mapping (deps, VCS, caches, build output).
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".eggs",
}


@dataclass
class Symbol:
    kind: str          # "function" | "class" | "method"
    name: str
    signature: str     # e.g. "(self, x, y=1) -> bool"
    line: int
    decorators: list[str] = field(default_factory=list)
    methods: list["Symbol"] = field(default_factory=list)  # classes only


@dataclass
class FileMap:
    path: str
    lines: int
    doc: str = ""                                   # first line of module docstring
    imports: list[str] = field(default_factory=list)  # first-party modules imported
    constants: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    error: str = ""                                 # set if the file could not be parsed


def _sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"({args}){ret}"


def _decorators(node) -> list[str]:
    return [ast.unparse(d) for d in node.decorator_list]


def _symbols_for(path: str, source: str) -> FileMap:
    """Parse one source file into a FileMap. Dispatch on extension here to add languages."""
    fm = FileMap(path=path, lines=source.count("\n") + 1)
    if not path.endswith(".py"):
        # Non-Python source: no parser yet. Add a tree-sitter backend here if needed.
        return fm
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        fm.error = f"SyntaxError: {e.msg} (line {e.lineno})"
        return fm

    fm.doc = (ast.get_docstring(tree) or "").strip().split("\n", 1)[0]

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            fm.imports.extend(_import_names(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fm.symbols.append(Symbol(
                kind="function", name=node.name, signature=_sig(node),
                line=node.lineno, decorators=_decorators(node),
            ))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            methods = [
                Symbol(kind="method", name=m.name, signature=_sig(m),
                       line=m.lineno, decorators=_decorators(m))
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            fm.symbols.append(Symbol(
                kind="class", name=node.name,
                signature=f"({bases})" if bases else "",
                line=node.lineno, decorators=_decorators(node), methods=methods,
            ))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    fm.constants.append(t.id)
    return fm


def _import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    # ImportFrom: "from .models import X" -> ".models"; "from a.b import X" -> "a.b"
    prefix = "." * node.level
    return [prefix + (node.module or "")]


def collect(root: str) -> list[FileMap]:
    maps: list[FileMap] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError) as e:
                maps.append(FileMap(path=rel, lines=0, error=str(e)))
                continue
            maps.append(_symbols_for(rel, source))
    maps.sort(key=lambda m: m.path)
    return maps


def _module_key(path: str) -> str:
    """'applicationbot/models.py' -> 'applicationbot.models' for import matching."""
    return path[:-3].replace(os.sep, ".") if path.endswith(".py") else path


def reverse_deps(maps: list[FileMap]) -> dict[str, list[str]]:
    """For each first-party module, which other first-party modules import it."""
    modules = {_module_key(m.path) for m in maps}
    # last path segment -> full module key, to resolve 'from .models' and 'import models'
    by_leaf: dict[str, str] = {}
    for mod in modules:
        by_leaf.setdefault(mod.rsplit(".", 1)[-1], mod)

    rdeps: dict[str, set[str]] = {mod: set() for mod in modules}
    for m in maps:
        importer = _module_key(m.path)
        for imp in m.imports:
            leaf = imp.lstrip(".").rsplit(".", 1)[-1] or imp.lstrip(".")
            target = by_leaf.get(leaf)
            if target and target != importer:
                rdeps[target].add(importer)
    return {k: sorted(v) for k, v in rdeps.items() if v}


def render_markdown(maps: list[FileMap], root: str) -> str:
    total_lines = sum(m.lines for m in maps)
    out = [
        "# Repo map — ApplicationBot",
        f"_{len(maps)} Python files · {total_lines} lines · "
        "regenerate with `python -m applicationbot.repo_map`_",
        "",
    ]
    for m in maps:
        out.append(f"## {m.path}  ({m.lines} lines)")
        if m.error:
            out.append(f"⚠ could not parse: {m.error}")
            out.append("")
            continue
        if m.doc:
            out.append(m.doc)
        if m.imports:
            out.append(f"_imports:_ {', '.join(sorted(set(m.imports)))}")
        if m.constants:
            out.append(f"_constants:_ {', '.join(m.constants)}")
        for s in m.symbols:
            deco = f" {' '.join('@' + d for d in s.decorators)}" if s.decorators else ""
            if s.kind == "class":
                out.append(f"- **class {s.name}{s.signature}**  ·{s.line}{deco}")
                for meth in s.methods:
                    mdeco = f" {' '.join('@' + d for d in meth.decorators)}" if meth.decorators else ""
                    out.append(f"    - {meth.name}{meth.signature}  ·{meth.line}{mdeco}")
            else:
                out.append(f"- {s.name}{s.signature}  ·{s.line}{deco}")
        out.append("")

    rdeps = reverse_deps(maps)
    if rdeps:
        out.append("## Dependency graph (who imports each module)")
        for mod in sorted(rdeps):
            out.append(f"- **{mod}** ← {', '.join(rdeps[mod])}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m applicationbot.repo_map",
        description="Print a compact structural map of the codebase.",
    )
    parser.add_argument("--root", default=".", help="Repo root to map (default: cwd)")
    parser.add_argument("--out", help="Write the map to this file instead of stdout")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    args = parser.parse_args(argv)

    maps = collect(args.root)
    if args.json:
        payload = {
            "files": [asdict(m) for m in maps],
            "reverse_deps": reverse_deps(maps),
        }
        text = json.dumps(payload, indent=2)
    else:
        text = render_markdown(maps, args.root)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Wrote {args.out} — {len(maps)} files mapped.")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
