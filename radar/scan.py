"""Stage 3 - what the repo actually imports, found with `ast`, not regex.

Regex over source finds the string `BaseSettings` in a comment, in a
docstring, in `my_own.BaseSettings`, and misses `import pydantic as pd`
followed by `pd.BaseSettings`. The AST knows the difference, and every node
carries a real line number - which is where the `file:line` in an alert
comes from.

Scope model: alias bindings are tracked per file, not per function scope. A
local variable that shadows an imported name inside one function can
therefore produce an extra reference site. Import sites - the evidence an
alert leads with - are exact.
"""

from __future__ import annotations

import ast
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

from .models import SymbolUsage, UsageSite

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "__pycache__", "node_modules", "site-packages",
    ".venv", "venv", "env", "build", "dist", ".eggs",
}


@dataclass
class ScanResult:
    """Every name the repo binds from an absolute import, keyed by its
    fully-qualified name."""

    root: Path
    usages: Dict[str, SymbolUsage] = field(default_factory=dict)
    files_scanned: int = 0
    files_skipped: List[Tuple[str, str]] = field(default_factory=list)
    star_imports: List[UsageSite] = field(default_factory=list)

    def modules(self) -> Set[str]:
        """Top-level module names bound anywhere in the repo."""
        return {u.module.split(".")[0] for u in self.usages.values()}

    def for_module(self, top_level: str) -> List[SymbolUsage]:
        """Usages belonging to one top-level import package, e.g. 'pydantic'."""
        return [
            u for u in self.usages.values()
            if u.qualified_name == top_level
            or u.qualified_name.startswith(top_level + ".")
        ]

    def star_imports_for(self, top_level: str) -> List[UsageSite]:
        return [
            s for s in self.star_imports
            if s.local_name == top_level or s.local_name.startswith(top_level + ".")
        ]


class _FileVisitor(ast.NodeVisitor):
    """Collects qualified-name usage for a single module.

    `aliases` maps a name bound in this file to the fully-qualified name it
    refers to: `from pydantic import BaseSettings as BS` gives
    `{"BS": "pydantic.BaseSettings"}`; `import pydantic as pd` gives
    `{"pd": "pydantic"}`.
    """

    def __init__(self, relpath: str):
        self.relpath = relpath
        self.aliases: Dict[str, str] = {}
        self.sites: List[Tuple[str, UsageSite]] = []  # (qualified_name, site)
        self.star_imports: List[UsageSite] = []

    # -- imports ----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            qualified = alias.name  # `import a.b` is always absolute
            if alias.asname:
                self.aliases[alias.asname] = qualified
                bound = alias.asname
            else:
                # `import a.b` binds `a`, and a later `a.b.c` must still
                # resolve - so bind the head to itself.
                bound = qualified.split(".")[0]
                self.aliases[bound] = bound
            self._record(qualified, node.lineno, "import", bound)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:  # `from . import x` - first-party code, not a dependency
            return
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self.star_imports.append(
                    UsageSite(file=self.relpath, line=node.lineno,
                              kind="import", local_name=module)
                )
                continue
            qualified = f"{module}.{alias.name}" if module else alias.name
            bound = alias.asname or alias.name
            self.aliases[bound] = qualified
            self._record(qualified, node.lineno, "import", bound)

    # -- uses -------------------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Resolve a dotted chain such as `pd.BaseSettings` through aliases."""
        parts: List[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Load):
            target = self.aliases.get(current.id)
            if target:
                parts.reverse()
                qualified = ".".join([target] + parts)
                self._record(qualified, node.lineno, "reference", current.id)
                return  # do not also record the base Name on its own
        # Not a plain dotted chain (`f().x`, `d["k"].y`) - keep walking.
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return  # a store rebinds the name; it is not a use of the import
        target = self.aliases.get(node.id)
        if target:
            self._record(target, node.lineno, "reference", node.id)

    # -- helpers ----------------------------------------------------------

    def _record(self, qualified: str, line: int, kind: str, local_name: str) -> None:
        self.sites.append(
            (qualified, UsageSite(file=self.relpath, line=line,
                                  kind=kind, local_name=local_name))
        )


def scan_file(
    path: Path, root: Path
) -> Tuple[List[Tuple[str, UsageSite]], List[UsageSite], Optional[str]]:
    """Parse one file. Returns (sites, star_imports, error)."""
    relpath = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return [], [], f"unreadable: {exc}"
    try:
        with warnings.catch_warnings():
            # Someone else's invalid escape sequence is not our warning to emit.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # A file written for a different Python version is not a scan
        # failure; record it so a report can say how much it actually saw.
        return [], [], f"syntax error at line {exc.lineno}: {exc.msg}"
    visitor = _FileVisitor(relpath)
    visitor.visit(tree)
    return visitor.sites, visitor.star_imports, None


def iter_python_files(root: Path, skip_dirs: Iterable[str] = SKIP_DIRS):
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            if name.endswith((".py", ".pyi")):
                yield Path(dirpath) / name


def scan_repo(
    root: Union[str, os.PathLike], skip_dirs: Iterable[str] = SKIP_DIRS
) -> ScanResult:
    """Walk a repo and collect every fully-qualified imported name it uses."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"not a directory: {root_path}")

    result = ScanResult(root=root_path)
    for path in iter_python_files(root_path, skip_dirs):
        sites, stars, error = scan_file(path, root_path)
        if error:
            result.files_skipped.append(
                (path.relative_to(root_path).as_posix(), error)
            )
            continue
        result.files_scanned += 1
        result.star_imports.extend(stars)
        for qualified, site in sites:
            usage = result.usages.get(qualified)
            if usage is None:
                usage = SymbolUsage(
                    qualified_name=qualified,
                    module=(qualified.rsplit(".", 1)[0]
                            if "." in qualified else qualified),
                )
                result.usages[qualified] = usage
            usage.sites.append(site)

    # Deterministic evidence order: the import first, then by file and line.
    for usage in result.usages.values():
        usage.sites.sort(key=lambda s: (s.kind != "import", s.file, s.line))
    return result
