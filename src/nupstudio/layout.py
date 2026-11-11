"""Resolve the active executable/metadata/DLC layout of a (cross-gen) dump workspace.

Priority for the executable (exefs) context, highest first:
  1. updates/<version>/program1/exefs   (native Switch 2 edition)
  2. updates/<version>/exefs            (legacy update)
  3. base/exefs                         (legacy base game)
  4. exefs/                             (root direct layout)

Metadata candidates start with the resolved layer and fall back to legacy controls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

NATIVE_PROGRAM = "program1"
NATIVE_CONTROL = "Acd_1"
LEGACY_NAMES = ("exefs", "control")

_TITLE_ID = re.compile(r"[0-9a-fA-F]{16}")


class LayoutError(Exception):
    """Raised when a packing operation would violate the layout safeguards."""


@dataclass(frozen=True)
class EditionLayout:
    workspace: Path
    kind: str  # "native", "legacy_update", "legacy_base" or "root"
    exefs: Path
    version: int = 0
    metadata_dir: Path | None = None
    native_root: Path | None = None
    dlc_dirs: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def is_native(self) -> bool:
        return self.kind == "native"


def _child_dir(parent: Path, name: str) -> Path | None:
    """Case-insensitive, symlink-free subdirectory lookup."""
    direct = parent / name
    if direct.is_dir() and not direct.is_symlink():
        return direct
    try:
        for child in parent.iterdir():
            if child.name.casefold() == name.casefold() and child.is_dir() and not child.is_symlink():
                return child
    except OSError:
        pass
    return None


def _update_dirs(workspace: Path) -> list[tuple[int, Path]]:
    updates = _child_dir(workspace, "updates")
    if not updates:
        return []
    found = []
    try:
        for child in updates.iterdir():
            if child.name.isdecimal() and child.is_dir() and not child.is_symlink():
                found.append((int(child.name), child))
    except OSError:
        return []
    return sorted(found, key=lambda item: item[0], reverse=True)


def _first_dir(parent: Path, *relative: str) -> Path | None:
    current: Path | None = parent
    for name in relative:
        current = _child_dir(current, name) if current else None
    return current


def find_root_dlc(workspace: Path) -> tuple[Path, ...]:
    """Return root-level dlc/<title_id>/ folders (versioned or not)."""
    dlc = _child_dir(workspace, "dlc")
    if not dlc:
        return ()
    try:
        return tuple(sorted(
            (c for c in dlc.iterdir()
             if _TITLE_ID.fullmatch(c.name) and c.is_dir() and not c.is_symlink()),
            key=lambda c: c.name.casefold(),
        ))
    except OSError:
        return ()


def resolve_layout(workspace: str | Path) -> EditionLayout | None:
    """Apply the fallback hierarchy to ``workspace``; return None if no exefs is found."""
    root = Path(workspace)
    dlc = find_root_dlc(root)
    updates = _update_dirs(root)

    for version, update in updates:
        program = _child_dir(update, NATIVE_PROGRAM)
        exefs = _child_dir(program, "exefs") if program else None
        if program and exefs:
            control = _first_dir(program, "control", NATIVE_CONTROL)
            return EditionLayout(root, "native", exefs, version, control, program, dlc)

    for version, update in updates:
        exefs = _child_dir(update, "exefs")
        if exefs:
            control = (_first_dir(update, "control", NATIVE_CONTROL)
                       or _child_dir(update, "control")
                       or _first_dir(root, "base", "control"))
            return EditionLayout(root, "legacy_update", exefs, version, control, None, dlc)

    exefs = _first_dir(root, "base", "exefs")
    if exefs:
        return EditionLayout(root, "legacy_base", exefs, 0,
                             _first_dir(root, "base", "control"), None, dlc)

    exefs = _child_dir(root, "exefs")
    if exefs:
        return EditionLayout(root, "root", exefs, 0, _child_dir(root, "control"), None, dlc)
    return None


def metadata_directories(layout: EditionLayout) -> tuple[Path, ...]:
    """Return control directories in metadata fallback order."""
    root = layout.workspace
    candidates: list[Path | None] = []
    if layout.kind == "native" and layout.native_root:
        update = layout.native_root.parent
        candidates.extend((
            _first_dir(layout.native_root, "control", NATIVE_CONTROL),
            _first_dir(layout.native_root, "control"),
            _first_dir(update, "control", NATIVE_CONTROL),
            _first_dir(update, "control"),
            _first_dir(root, "base", "control"),
        ))
    elif layout.kind == "legacy_update":
        update = layout.exefs.parent
        candidates.extend((
            _first_dir(update, "control", NATIVE_CONTROL),
            _first_dir(update, "control"),
            _first_dir(root, "base", "control"),
        ))
    return tuple(dict.fromkeys(path for path in candidates if path is not None))


def legacy_exclusions(package_root: Path) -> frozenset[str]:
    """Top-level names to skip when packing an update folder that holds a native program1.

    Legacy exefs/control siblings must not be packed alongside the native footprint.
    """
    program = _child_dir(package_root, NATIVE_PROGRAM)
    if not (program and _child_dir(program, "exefs")):
        return frozenset()
    return frozenset(
        child.name.casefold() for name in LEGACY_NAMES
        if (child := _child_dir(package_root, name))
    )


def _is_legacy_source(path: Path) -> bool:
    parts = [p.casefold() for p in path.parts]
    for i, part in enumerate(parts[:-1]):
        if part == "exefs" and i >= 1 and parts[i - 1] == "base":
            return True
        if part == "exefs" and i >= 2 and parts[i - 2] == "updates" and parts[i - 1].isdecimal():
            return True
    return False


def ensure_no_legacy_overwrite(packages) -> None:
    """Overwrite block: legacy exefs content must never land in a program1/ footprint.

    ``packages`` is an iterable of objects with ``entries`` having ``rel``, ``source``
    and ``is_dir`` attributes. Raises LayoutError on any violation, including
    case-insensitive duplicate destinations.
    """
    for package in packages:
        seen: set[str] = set()
        for entry in package.entries:
            if entry.is_dir:
                continue
            key = entry.rel.casefold()
            if key in seen:
                raise LayoutError(f"Duplicate destination in package: {entry.rel}")
            seen.add(key)
            if entry.rel.split("/", 1)[0].casefold() != NATIVE_PROGRAM or entry.source is None:
                continue
            source = Path(entry.source)
            if _is_legacy_source(source):
                raise LayoutError(
                    f"Refusing to pack legacy file {source} into native footprint {entry.rel}."
                )
            rel_parts = entry.rel.split("/")
            if [p.casefold() for p in source.parts[-len(rel_parts):]] != [p.casefold() for p in rel_parts]:
                raise LayoutError(f"Source {source} does not match native destination {entry.rel}.")
