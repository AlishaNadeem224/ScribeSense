"""Import rules (A2, K3 rule 9, K9, AGENTS.md §7), checked statically on the source tree.

Rules:
  R1  contracts/ imports only the standard library and other contracts.
  R2  only contracts/paths.py imports subprocess (every command goes through run_cmd, K3 rule 9).
  R3  only store/ imports sqlite3.
  R4  config/ and contracts/ import nothing from ui, store, adapters, safety, controller, reader, gi.
  R5  adapters/ never import the store, safety (journal), ui or controller.
  R6  A2: recovery entry points (safety/recovery.py, doctor.py, cli.py at module level) never reach
      Gtk, Adw, scribesense.ui or scribesense.reader — directly or transitively.
A self-test proves the checker catches a deliberate violation.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
GUI_TARGETS = ("gi.repository.Gtk", "gi.repository.Adw", "scribesense.ui", "scribesense.reader")
RECOVERY_ROOTS = ("scribesense.safety.recovery", "scribesense.doctor")
LAZY_ROOTS = ("scribesense.cli",)  # only module-level imports count (UI may be imported lazily)


@dataclass
class Module:
    name: str
    is_package: bool
    top_level: set[str] = field(default_factory=set)  # imports at module level
    all_imports: set[str] = field(default_factory=set)  # imports anywhere in the file
    gui_version_calls: bool = False  # gi.require_version("Gtk"/"Adw", ...)


def _module_name(path: Path, src: Path) -> tuple[str, bool]:
    rel = path.relative_to(src).with_suffix("")
    parts = list(rel.parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


def _resolve(node: ast.ImportFrom, mod: Module) -> str:
    if node.level == 0:
        return node.module or ""
    base = mod.name.split(".") if mod.is_package else mod.name.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def scan(src: Path) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for path in sorted(src.rglob("*.py")):
        name, is_pkg = _module_name(path, src)
        mod = Module(name, is_pkg)
        tree = ast.parse(path.read_text(), filename=str(path))
        top_nodes = set(map(id, tree.body))
        for node in ast.walk(tree):
            found: list[str] = []
            if isinstance(node, ast.Import):
                found = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve(node, mod)
                found = [base] + [f"{base}.{a.name}" for a in node.names if a.name != "*"]
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "require_version" and node.args
                  and isinstance(node.args[0], ast.Constant) and node.args[0].value in ("Gtk", "Adw")):
                mod.gui_version_calls = True
            mod.all_imports.update(found)
            if found and id(node) in top_nodes:
                mod.top_level.update(found)
        modules[name] = mod
    return modules


def _hits(imports: set[str], prefixes: tuple[str, ...]) -> set[str]:
    return {i for i in imports if any(i == p or i.startswith(p + ".") for p in prefixes)}


def gui_reachable(modules: dict[str, Module], roots: tuple[str, ...], lazy_roots: tuple[str, ...]) -> dict[str, set[str]]:
    """root -> GUI targets reachable from it (empty = clean)."""
    result: dict[str, set[str]] = {}
    for root in roots + lazy_roots:
        if root not in modules:
            continue
        seen: set[str] = set()
        stack = [root]
        bad: set[str] = set()
        while stack:
            name = stack.pop()
            if name in seen or name not in modules:
                continue
            seen.add(name)
            mod = modules[name]
            # Strict everywhere: any import in any reached module counts. Only the lazy root itself
            # (cli.py) may import UI inside functions, because its GUI commands load it on demand.
            lazy_self = name == root and root in lazy_roots
            imports = mod.top_level if lazy_self else mod.all_imports
            bad |= _hits(imports, GUI_TARGETS)
            if mod.gui_version_calls and not lazy_self:
                bad.add(f"{name}: gi.require_version(Gtk/Adw)")
            stack.extend(i for i in imports if i in modules)
            # importing a submodule also executes its parent packages
            stack.extend(".".join(name.split(".")[:k]) for k in range(1, name.count(".") + 1))
        result[root] = bad
    return result


def violations(src: Path) -> list[str]:
    modules = scan(src)
    out: list[str] = []
    for name, mod in modules.items():
        tops = {i.split(".")[0] for i in mod.all_imports}
        if name.startswith("scribesense.contracts"):
            for imp in mod.all_imports:
                if imp.split(".")[0] not in STDLIB and not imp.startswith("scribesense.contracts"):
                    out.append(f"R1 {name} imports {imp}")
        if "subprocess" in tops and name != "scribesense.contracts.paths":
            out.append(f"R2 {name} imports subprocess (use run_cmd)")
        if "sqlite3" in tops and not name.startswith("scribesense.store"):
            out.append(f"R3 {name} imports sqlite3")
        if name.startswith(("scribesense.config", "scribesense.contracts")):
            for imp in _hits(mod.all_imports, ("scribesense.ui", "scribesense.store", "scribesense.adapters",
                                               "scribesense.safety", "scribesense.controller",
                                               "scribesense.reader", "gi")):
                out.append(f"R4 {name} imports {imp}")
        if name.startswith("scribesense.adapters"):
            for imp in _hits(mod.all_imports, ("scribesense.store", "scribesense.safety",
                                               "scribesense.ui", "scribesense.controller")):
                out.append(f"R5 {name} imports {imp}")
    for root, bad in gui_reachable(modules, RECOVERY_ROOTS, LAZY_ROOTS).items():
        out.extend(f"R6 {root} reaches {b}" for b in sorted(bad))
    return out


def test_source_tree_follows_import_rules() -> None:
    assert violations(SRC) == []


def test_checker_catches_deliberate_violations(tmp_path: Path) -> None:
    pkg = tmp_path / "scribesense"
    for sub in ("contracts", "adapters", "safety", "ui", "config"):
        (pkg / sub).mkdir(parents=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "contracts" / "bad.py").write_text("import fontTools\n")                       # R1
    (pkg / "adapters" / "gtk.py").write_text("import subprocess\nfrom scribesense.safety import journal\n")  # R2, R5
    (pkg / "config" / "x.py").write_text("import sqlite3\nfrom ..ui import window\n")     # R3, R4
    (pkg / "ui" / "window.py").write_text("from gi.repository import Gtk\n")
    (pkg / "safety" / "helpers.py").write_text("from scribesense.ui import window\n")
    (pkg / "safety" / "recovery.py").write_text("from scribesense.safety import helpers\n")  # R6 transitive
    (pkg / "doctor.py").write_text("import gi\ngi.require_version('Gtk', '4.0')\n")         # R6 direct
    (pkg / "cli.py").write_text("def main():\n    from scribesense.ui import window\n")  # lazy: allowed
    found = violations(tmp_path)
    for rule in ("R1", "R2", "R3", "R4", "R5"):
        assert any(v.startswith(rule) for v in found), (rule, found)
    assert any(v.startswith("R6 scribesense.safety.recovery") for v in found), found
    assert any(v.startswith("R6 scribesense.doctor") for v in found), found
    assert not any(v.startswith("R6 scribesense.cli") for v in found), found
