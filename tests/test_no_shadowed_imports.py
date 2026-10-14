"""
A function-level import of a name the function already used above it makes that name local to the whole function,
so the earlier use raises UnboundLocalError. This took down every AI reply (the draft_reply worker failed on all
channels) when a `from shared.db.models import Customer` slipped into the middle of services/workers/respond.py.

Pure code reading: no database, no network.
"""

import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
CHECKED = ("services", "shared", "scripts")


def _problems() -> list[tuple]:
    found = []
    for top in CHECKED:
        for dirpath, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                tree = ast.parse(open(path, encoding="utf-8").read())
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    first_import: dict[str, int] = {}
                    for node in ast.walk(fn):
                        if isinstance(node, (ast.Import, ast.ImportFrom)):
                            for alias in node.names:
                                first_import.setdefault((alias.asname or alias.name).split(".")[0], node.lineno)
                    for imported, line in first_import.items():
                        for node in ast.walk(fn):
                            if isinstance(node, ast.Name) and node.id == imported and node.lineno < line:
                                found.append((os.path.relpath(path, ROOT), fn.name, imported, node.lineno, line))
                                break
    return found


def test_no_function_uses_a_name_before_importing_it_locally():
    assert _problems() == []
