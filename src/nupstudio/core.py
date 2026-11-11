from __future__ import annotations

import hashlib
import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import zstandard

from .layout import (
    EditionLayout,
    LayoutError,
    ensure_no_legacy_overwrite,
    legacy_exclusions,
    metadata_directories,
    resolve_layout,
)

HEADER_SIZE = 128
PACKAGE_SIZE = 64
DIRECTORY_SIZE = 24
FILE_SIZE = 80
MAX_ZSTD_FILE_SIZE = 256 * 1024 * 1024
TLV_NAMES = {3: "icon", 4: "version", 5: "title", 6: "publisher"}
Progress = Callable[[int, str], None]


class NupError(Exception):
    """Raised when a NUP operation cannot be completed safely."""


@dataclass
class Entry:
    name: str
    parent: int
    is_dir: bool
    rel: str
    source: Path | None = None
    size: int = 0
    stored: int = 0
    offset: int = 0
    digest: bytes = b""
    codec: int = 0
    staged: Path | None = None
    dir_id: int = -1
    source_offset: int = 0


@dataclass
class Package:
    title_id: int = 0
    version: int = 0
    kind: int = 0
    flags: int = 0
    entries: list[Entry] = field(default_factory=list)
    metadata: bytes = b""
    root_dir: int = 0
    meta_offset: int = 0


@dataclass
class Container:
    path: Path
    packages: list[Package]
    file_size: int


def _progress(callback: Progress | None, percent: int, message: str) -> None:
    if callback:
        callback(max(0, min(percent, 100)), message)


def _align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def _tlv(kind: int, value: bytes) -> bytes:
    if len(value) > 0xFFFFFFFF:
        raise NupError("A metadata field is too large.")
    return struct.pack("<HHI", kind, 0, len(value)) + value


def _parse_metadata(data: bytes) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise NupError("Package metadata contains a truncated field header.")
        kind, reserved, size = struct.unpack_from("<HHI", data, offset)
        offset += 8
        if reserved or size > len(data) - offset:
            raise NupError("Package metadata contains an invalid field.")
        value = data[offset : offset + size]
        offset += size
        fields[kind] = value
    return fields


def _metadata_values(data: bytes) -> dict[str, str | bool]:
    fields = _parse_metadata(data)
    values: dict[str, str | bool] = {"has_icon": 3 in fields}
    for kind, name in TLV_NAMES.items():
        if kind in fields and kind != 3:
            values[name] = fields[kind].decode("utf-8", errors="replace")
    if 2 in fields:
        nacp = _decode_nacp(fields[2])
        for key in ("title", "publisher", "version"):
            if not values.get(key) and nacp.get(key):
                values[key] = nacp[key]
    return values


def _decode_nacp(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}

    def decode_field(value: bytes) -> str:
        raw = value.split(b"\0", 1)[0]
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return ""  # encrypted/corrupt text (e.g. Switch 2 control data) is not a real string
        if any(not char.isprintable() for char in text):
            return ""
        return text

    for offset in range(0, min(len(data), 16 * 0x300), 0x300):
        title = decode_field(data[offset : offset + 0x200])
        publisher = decode_field(data[offset + 0x200 : offset + 0x300])
        if title and not result.get("title"):
            result["title"] = title
        if publisher and not result.get("publisher"):
            result["publisher"] = publisher
        if result.get("title") and result.get("publisher"):
            break
    if len(data) >= 0x3070:
        result["version"] = decode_field(data[0x3060:0x3070])
    return result


def _make_metadata(
    *,
    title: str | None = None,
    publisher: str | None = None,
    version: str | None = None,
    icon_path: Path | None = None,
    source_name: str | None = None,
    dump_files: Iterable[Entry] = (),
    selected_nacp: Entry | None = None,
) -> bytes:
    additions: list[tuple[int, bytes]] = []
    for kind, value in ((5, title), (6, publisher), (4, version)):
        if value:
            additions.append((kind, value.encode("utf-8")))
    if icon_path:
        try:
            icon_data = icon_path.read_bytes()
        except OSError as exc:
            raise NupError(f"Cannot read icon file: {exc}") from exc
        if len(icon_data) > 64 * 1024 * 1024:
            raise NupError("Icon files larger than 64 MiB are not supported.")
        additions.append((3, icon_data))

    dump_files = list(dump_files)
    found_npdm = False
    found_build_id = False
    nacp_entry = selected_nacp
    if nacp_entry and nacp_entry.source:
        if nacp_entry.size > 64 * 1024 * 1024:
            raise NupError("NACP metadata larger than 64 MiB is not supported.")
        try:
            additions.append((2, nacp_entry.source.read_bytes()))
        except OSError as exc:
            raise NupError(f"Cannot read NACP file {nacp_entry.source}: {exc}") from exc
    for entry in dump_files:
        if entry.is_dir or not entry.source:
            continue
        key = entry.rel.casefold()
        if key in ("exefs/main.npdm", "main.npdm") and not found_npdm:
            if entry.size > 64 * 1024 * 1024:
                raise NupError("NPDM metadata larger than 64 MiB is not supported.")
            additions.append((1, entry.source.read_bytes()))
            found_npdm = True
        elif key in ("exefs/main", "main") and not found_build_id and entry.size >= 0x60:
            with entry.source.open("rb") as source:
                header = source.read(0x60)
            if header[:4] == b"NSO0" or header[0x10:0x14] == b"NRO0":
                additions.append((7, header[0x40:0x60]))
                found_build_id = True
    if source_name:
        additions.append((8, source_name.encode("utf-8")))
    metadata = b"".join(_tlv(kind, value) for kind, value in additions)
    if len(metadata) > 64 * 1024 * 1024:
        raise NupError("Combined package metadata larger than 64 MiB is not supported.")
    return metadata


def _parse_uint(text: str, *, base: int = 10, limit: int = 0xFFFFFFFF) -> int:
    try:
        value = int(text, 16 if base == 0 and text.lower().startswith("0x") else
                    10 if base == 0 else base)
    except ValueError as exc:
        raise NupError("Enter a valid numeric value.") from exc
    if value < 0 or value > limit:
        raise NupError("Numeric value is outside the supported range.")
    return value


def _classify_package(path: Path) -> tuple[int, int, int]:
    parts = [part.casefold() for part in path.parts]
    kind, title_id, version = 0, 0, 0
    for index, part in enumerate(parts):
        if part == "updates" and index + 1 < len(parts) and parts[index + 1].isdecimal():
            kind, version = 1, int(parts[index + 1])
        elif part == "dlc" and index + 2 < len(parts):
            candidate = parts[index + 1]
            if re.fullmatch(r"[0-9a-fA-F]{16}", candidate) and parts[index + 2].isdecimal():
                kind, title_id, version = 2, int(candidate, 16), int(parts[index + 2])
        elif part == "dlc" and index + 2 == len(parts):
            candidate = parts[index + 1]
            if re.fullmatch(r"[0-9a-fA-F]{16}", candidate):
                kind, title_id, version = 2, int(candidate, 16), 0
    return kind, title_id, version


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _is_package(path: Path) -> bool:
    parts = path.parts
    update_root = (
        len(parts) >= 2 and parts[-2].casefold() == "updates"
        and parts[-1].isdecimal()
    )
    dlc_root = (
        len(parts) >= 3 and parts[-3].casefold() == "dlc"
        and re.fullmatch(r"[0-9a-fA-F]{16}", parts[-2]) is not None
        and parts[-1].isdecimal()
    )
    # Unversioned root-level dlc/<title_id>/ blocks are retained as DLC packages.
    dlc_unversioned = (
        len(parts) >= 2 and parts[-2].casefold() == "dlc"
        and re.fullmatch(r"[0-9a-fA-F]{16}", parts[-1]) is not None
        and not any(c.is_dir() and c.name.isdecimal() for c in _safe_iterdir(path))
    )
    return update_root or dlc_root or dlc_unversioned or any(
        (path / name).is_dir() for name in ("exefs", "romfs")
    )


def _has_packable_files(path: Path) -> bool:
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            children = current.iterdir()
            for child in children:
                if child.name.casefold() == ".listing" or child.is_symlink():
                    continue
                if child.is_file():
                    return True
                if child.is_dir():
                    pending.append(child)
        except OSError as exc:
            raise NupError(f"Cannot inspect package folder {current}: {exc}") from exc
    return False


def _find_packages(
    source: Path,
    *,
    require_packable_files: bool = True,
    scanned_entries: dict[Path, list[Entry]] | None = None,
) -> list[Path]:
    def is_packable(path: Path) -> bool:
        if not require_packable_files:
            return True
        if scanned_entries is None:
            return _has_packable_files(path)
        entries = _scan_package(path, 0, skip_top=legacy_exclusions(path))
        if any(not entry.is_dir for entry in entries):
            scanned_entries[path] = entries
            return True
        return False

    if not source.is_dir():
        raise NupError("Select an existing package folder or dump root.")
    if _is_package(source) and is_packable(source):
        return [source]
    found: list[Path] = []
    pending = [(source, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 64:
            raise NupError("Package discovery exceeded the maximum folder depth.")
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.encode("utf-8"))
        except OSError as exc:
            raise NupError(f"Cannot read folder {current}: {exc}") from exc
        for child in children:
            if child.name.casefold() == ".listing" or child.is_symlink() or not child.is_dir():
                continue
            if _is_package(child):
                if is_packable(child):
                    found.append(child)
            else:
                pending.append((child, depth + 1))
    found = list(dict.fromkeys(found))  # one entry per workspace folder
    found.sort(key=lambda path: (
        _classify_package(path)[0],
        _classify_package(path)[2],
        _classify_package(path)[1],
        path.as_posix().casefold(),
    ))
    if not found:
        raise NupError("No package folders containing exefs/ or romfs/ were found.")
    base_packages = [path for path in found if _classify_package(path)[0] == 0]
    if len(base_packages) > 1:
        base_paths = "\n  • ".join(p.relative_to(source).as_posix() for p in base_packages)
        raise NupError(f"A NUP container can contain at most one base package, but found {len(base_packages)}:\n  • {base_paths}")
    return found


def _npdm_title_id(entries: list[Entry]) -> int:
    for entry in entries:
        if entry.is_dir or not entry.source or entry.rel.casefold() != "exefs/main.npdm":
            continue
        try:
            data = entry.source.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NupError(f"Cannot read NPDM file {entry.source}: {exc}") from exc
        position = data.find(b"ACI0")
        while position >= 0 and position + 24 <= len(data):
            title_id = struct.unpack_from("<Q", data, position + 16)[0]
            if title_id:
                return title_id
            position = data.find(b"ACI0", position + 1)
    return 0


_NACP_MIN_SIZE = 0x3070
_NACP_MAX_DEPTH = 6


def _nacp_rank(rel: str) -> tuple[int, int, str] | None:
    parts = rel.casefold().split("/")
    if parts[-1] != "control.nacp" or len(parts) > _NACP_MAX_DEPTH:
        return None
    parent = parts[-2] if len(parts) > 1 else ""
    if parent == "control":
        preferred = 0
    elif parent == "romfs":
        preferred = 1
    elif any(part == "updates" or part.startswith("program") for part in parts[:-1]):
        preferred = 2
    elif not parent:
        preferred = 3
    else:
        preferred = 4
    return preferred, len(parts), "/".join(parts)


def _nacp_candidates(entries: Iterable[Entry]) -> Iterable[tuple[Entry, dict[str, str]]]:
    """Yield readable, sufficiently sized control files in rank order."""
    ranked = []
    for entry in entries:
        if entry.is_dir or not entry.source or entry.size < _NACP_MIN_SIZE:
            continue
        rank = _nacp_rank(entry.rel)
        if rank:
            ranked.append((rank, entry))
    for _, entry in sorted(ranked, key=lambda item: item[0]):
        try:
            with entry.source.open("rb") as source:
                data = source.read(_NACP_MIN_SIZE)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NupError(f"Cannot read NACP file {entry.source}: {exc}") from exc
        if len(data) >= _NACP_MIN_SIZE:
            yield entry, _decode_nacp(data)


def _find_nacp(entries: Iterable[Entry]) -> tuple[Entry, dict[str, str]] | None:
    return next(iter(_nacp_candidates(entries)), None)


def _metadata_entries(root: Path) -> list[Entry]:
    entries: list[Entry] = []

    def onerror(exc: OSError) -> None:
        raise NupError(f"Cannot read package folder {exc.filename}: {exc}") from exc

    for current, dirs, files in os.walk(root, topdown=True, onerror=onerror):
        directory = Path(current)
        relative = directory.relative_to(root)
        depth = len(relative.parts)
        dirs[:] = [
            name for name in dirs
            if name.casefold() != ".listing" and not (directory / name).is_symlink()
        ]
        if depth >= _NACP_MAX_DEPTH - 1:
            dirs.clear()
        elif directory.name.casefold() == "romfs":
            if not any(name.casefold() == "control.nacp" for name in files) and not any(
                name.casefold() == "control" for name in dirs
            ):
                dirs.clear()
        for name in files:
            folded = name.casefold()
            rel = (relative / name).as_posix()
            if not (
                rel.casefold() == "exefs/main.npdm"
                or folded == "control.nacp"
                or (folded.startswith("icon_") and folded.endswith(".dat"))
            ):
                continue
            path = directory / name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise NupError(f"Cannot inspect file {path}: {exc}") from exc
            entries.append(Entry(name, 0, False, rel, source=path, size=size))
    return entries


def _find_icon(entries: Iterable[Entry], nacp: Entry) -> Entry | None:
    """Return the control icon (icon_*.dat) stored beside the chosen control.nacp."""
    folder = nacp.rel.rpartition("/")[0].casefold()
    icons = [entry for entry in entries
             if not entry.is_dir and entry.source and 0 < entry.size <= 64 * 1024 * 1024
             and entry.rel.rpartition("/")[0].casefold() == folder
             and entry.name.casefold().startswith("icon_")
             and entry.name.casefold().endswith(".dat")]
    if not icons:
        return None
    icons.sort(key=lambda item: (item.name.casefold() != "icon_americanenglish.dat",
                                 item.name.casefold()))
    return icons[0]


def _collect_metadata(groups: Iterable[list[Entry]]) -> tuple[dict[str, str], Entry | None, Entry | None]:
    fields: dict[str, str] = {}
    first_nacp = None
    icon = None
    seen: set[Path] = set()
    for entries in groups:
        for nacp, values in _nacp_candidates(entries):
            if nacp.source in seen:
                continue
            if nacp.source:
                seen.add(nacp.source)
            if first_nacp is None:
                first_nacp = nacp
            for key, value in values.items():
                if value and not fields.get(key):
                    fields[key] = value
            if icon is None:
                icon = _find_icon(entries, nacp)
    return fields, first_nacp, icon


def _layout_metadata(layout: EditionLayout) -> tuple[dict[str, str], Entry | None, Entry | None]:
    groups = []
    for directory in metadata_directories(layout):
        entries = _metadata_entries(directory)
        groups.append([entry for entry in entries
                       if entry.source and entry.source.parent == directory])
    return _collect_metadata(groups)


def _scan_package(root: Path, root_dir: int, max_depth: int | None = None,
                  skip_top: frozenset[str] = frozenset()) -> list[Entry]:
    entries: list[Entry] = []
    dir_ids: dict[Path, int] = {root: root_dir}
    next_dir_id = root_dir + 1

    def scan(directory: Path, relative: Path, depth: int = 1) -> None:
        nonlocal next_dir_id
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.encode("utf-8"))
        except OSError as exc:
            raise NupError(f"Cannot read package folder {directory}: {exc}") from exc
        for child in children:
            if child.name.casefold() == ".listing" or child.is_symlink():
                continue
            if directory == root and child.name.casefold() in skip_top:
                continue
            is_dir = child.is_dir()
            if not is_dir and not child.is_file():
                continue
            rel = relative / child.name
            entry = Entry(child.name, dir_ids[directory], is_dir, rel.as_posix(),
                          source=None if is_dir else child,
                          dir_id=next_dir_id if is_dir else -1)
            if not is_dir:
                try:
                    entry.size = child.stat().st_size
                except OSError as exc:
                    raise NupError(f"Cannot inspect file {child}: {exc}") from exc
            entries.append(entry)
            if is_dir:
                dir_ids[child] = next_dir_id
                next_dir_id += 1
                if max_depth is None or depth < max_depth:
                    scan(child, rel, depth + 1)

    scan(root, Path())
    return entries


def _make_build_packages(
    source: Path,
    title_id: str = "",
    title: str = "",
    publisher: str = "",
    display_version: str = "",
    icon_path: str = "",
    base_version: str = "",
    update_version: str = "",
) -> list[Package]:
    scanned_entries: dict[Path, list[Entry]] = {}
    roots = _find_packages(source, scanned_entries=scanned_entries)
    layout = resolve_layout(source)
    packages: list[Package] = []
    base_title_id = 0
    for index, root in enumerate(roots):
        kind, dlc_id, version = _classify_package(root)
        entries = scanned_entries[root]
        for entry in entries:
            entry.parent += index
            if entry.is_dir:
                entry.dir_id += index
        detected_id = dlc_id or _npdm_title_id(entries)
        if (layout and layout.kind in ("native", "legacy_update")
                and root == (layout.native_root.parent if layout.native_root else layout.exefs.parent)):
            nacp, selected_nacp, selected_icon = _layout_metadata(layout)
        else:
            nacp, selected_nacp, selected_icon = _collect_metadata([entries])
        if title_id and kind != 2:
            detected_id = _parse_uint(title_id, base=16, limit=0xFFFFFFFFFFFFFFFF)
        override_version = base_version if kind == 0 else update_version
        if override_version and (kind == 0 or len(roots) == 1):
            version = _parse_uint(override_version, base=0)
        if kind == 0:
            base_title_id = detected_id
        metadata = _make_metadata(
            title=title or nacp.get("title") or None,
            publisher=publisher or nacp.get("publisher") or None,
            version=display_version or nacp.get("version") or None,
            icon_path=Path(icon_path) if icon_path else (
                selected_icon.source if selected_icon else None
            ),
            source_name=root.name,
            dump_files=entries,
            selected_nacp=selected_nacp,
        )
        packages.append(Package(detected_id, version, kind, 0, entries, metadata, index))
    if not any(package.kind == 0 for package in packages):
        for package in packages:
            if package.kind == 1:
                package.flags |= 1
    for package in packages:
        if package.kind == 1 and not package.title_id:
            package.title_id = base_title_id
    return packages


def inspect_folder(source: str | Path) -> dict[str, str]:
    source_path = Path(source).resolve()
    roots = _find_packages(source_path, require_packable_files=False)
    base_root = next(
        (candidate for candidate in roots if _classify_package(candidate)[0] == 0),
        None,
    )
    root = base_root or roots[0]
    kind, dlc_id, version = _classify_package(root)
    entries_by_root = {candidate: _metadata_entries(candidate) for candidate in roots}
    entries = entries_by_root[root]
    detected_id = dlc_id or _npdm_title_id(entries)
    layout = resolve_layout(source_path)
    metadata_roots = ([base_root] if base_root else []) + [
        candidate for candidate in roots
        if candidate != base_root and _classify_package(candidate)[0] != 2
    ]
    if not metadata_roots:
        metadata_roots = roots
    nacp, _, icon = _collect_metadata(entries_by_root[candidate] for candidate in metadata_roots)
    if layout:
        if layout.exefs:
            npdm = layout.exefs / "main.npdm"
            if npdm.is_file():
                detected_id = _npdm_title_id(
                    [Entry("main.npdm", 0, False, "exefs/main.npdm", source=npdm)]
                ) or detected_id
        if layout.kind in ("native", "legacy_update"):
            nacp, _, icon = _layout_metadata(layout)
    update_versions = [
        _classify_package(candidate)[2]
        for candidate in roots
        if _classify_package(candidate)[0] == 1
    ]
    result = {
        "title_id": f"{detected_id:016X}" if detected_id else "",
        "title": "",
        "publisher": "",
        "display_version": "",
        "package_count": str(len(roots)),
        "package_kinds": ",".join(str(_classify_package(candidate)[0]) for candidate in roots),
        "base_version": str(version) if kind == 0 else "",
        "update_version": (
            str(max(update_versions)) if update_versions
            else str(version) if kind == 1 else ""
        ),
    }
    result["title"] = nacp.get("title", "")
    result["publisher"] = nacp.get("publisher", "")
    result["display_version"] = nacp.get("version", "")
    result["icon_file"] = str(icon.source) if icon and icon.source else ""
    return result


def _prepare_entries(packages: list[Package], compression_level: int,
                     progress: Progress | None, temp_dir: Path) -> None:
    all_files = [entry for package in packages for entry in package.entries if not entry.is_dir]
    total = sum(max(entry.size, 1) for entry in all_files)
    complete = 0
    seen: dict[tuple[int, bytes], Entry] = {}
    compressor = zstandard.ZstdCompressor(level=compression_level) if compression_level else None
    for entry in all_files:
        entry.digest = hashlib.sha256()
        duplicate_key = None
        if entry.source is None:
            raise NupError("A package file has no source path.")
        compress_entry = compressor is not None and entry.size < MAX_ZSTD_FILE_SIZE
        with entry.source.open("rb") as source:
            stage_path = temp_dir / f"extent-{len(seen)}.zst"
            staged = stage_path.open("wb") if compress_entry else None
            try:
                writer = (
                    compressor.stream_writer(staged, size=entry.size, closefd=False)
                    if compress_entry and staged
                    else None
                )
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    if writer:
                        writer.write(chunk)
                    _progress(progress, min(5, (complete + size) * 5 // max(total, 1)),
                              "Preparing package files")
                if writer:
                    writer.close()
                if compress_entry and size != entry.size:
                    raise NupError(f"Source file changed while preparing: {entry.rel}")
            finally:
                if staged:
                    staged.close()
            entry.size = size
            entry.digest = digest.digest()
        duplicate_key = (entry.size, entry.digest)
        duplicate = seen.get(duplicate_key) if entry.size else None
        if duplicate:
            entry.codec = duplicate.codec
            entry.stored = duplicate.stored
            entry.offset = duplicate.offset
            entry.staged = duplicate.staged
            if compress_entry:
                stage_path.unlink(missing_ok=True)
        else:
            compressed_size = stage_path.stat().st_size if compress_entry and entry.size else entry.size
            if compress_entry and compressed_size < entry.size:
                entry.codec = 1
                entry.stored = compressed_size
                entry.staged = stage_path
            else:
                entry.codec = 0
                entry.stored = entry.size
                if compress_entry:
                    stage_path.unlink(missing_ok=True)
            if entry.size:
                seen[duplicate_key] = entry
        complete += max(entry.size, 1)
    _progress(progress, 5, "Prepared package files")


def _build_toc(packages: list[Package]) -> bytes:
    package_count = len(packages)
    dir_records: list[dict[str, int | bytes]] = [
        {"name": b"", "parent": -1} for _ in range(package_count)
    ]
    file_entries: list[tuple[Package, Entry]] = []
    local_to_global: list[dict[int, int]] = []
    global_dir_entry: dict[int, Entry] = {}
    file_indices: dict[int, int] = {}
    for package_index, package in enumerate(packages):
        local_ids = {package.root_dir: package_index}
        local_to_global.append(local_ids)
        for entry in package.entries:
            parent_id = local_ids.get(entry.parent)
            if parent_id is None:
                raise NupError(f"Invalid parent directory for {entry.rel}.")
            if entry.is_dir:
                new_id = len(dir_records)
                if entry.dir_id < 0 or entry.dir_id in local_ids:
                    raise NupError(f"Invalid directory index for {entry.rel}.")
                local_ids[entry.dir_id] = new_id
                dir_records.append({"name": entry.name.encode("utf-8"), "parent": parent_id})
                global_dir_entry[new_id] = entry
            else:
                file_indices[id(entry)] = len(file_entries)
                file_entries.append((package, entry))

    dir_children: list[list[tuple[bytes, int]]] = [[] for _ in dir_records]
    file_children: list[list[tuple[bytes, int]]] = [[] for _ in dir_records]
    for package_index, package in enumerate(packages):
        local_ids = local_to_global[package_index]
        for entry in package.entries:
            parent_id = local_ids.get(entry.parent)
            if parent_id is None:
                raise NupError(f"Invalid directory tree at {entry.rel}.")
            if entry.is_dir:
                child_id = local_ids.get(entry.dir_id)
                if child_id is None:
                    raise NupError(f"Invalid directory tree at {entry.rel}.")
                dir_children[parent_id].append((entry.name.encode("utf-8"), child_id))
            else:
                file_children[parent_id].append(
                    (entry.name.encode("utf-8"), len(dir_records) + file_indices[id(entry)])
                )

    all_dirs = len(dir_records)
    child_offsets: list[int] = []
    child_ids: list[int] = []
    for directory_id in range(all_dirs):
        children = sorted(dir_children[directory_id] + file_children[directory_id],
                          key=lambda item: item[0])
        child_offsets.append(len(child_ids))
        child_ids.extend(child_id for _, child_id in children)

    package_offset = 48
    dir_offset = package_offset + package_count * PACKAGE_SIZE
    file_offset = dir_offset + all_dirs * DIRECTORY_SIZE
    children_offset = file_offset + len(file_entries) * FILE_SIZE
    strings_offset = children_offset + len(child_ids) * 4
    if (all_dirs > 0xFFFFFFFF or len(file_entries) > 0xFFFFFFFF
            or len(child_ids) > 0xFFFFFFFF or strings_offset > 0xFFFFFFFF):
        raise NupError("Package tables exceed the NUP format limits.")
    string_pool = bytearray()
    name_ranges: dict[int, tuple[int, int]] = {}
    for package_index, package in enumerate(packages):
        for entry in package.entries:
            name = entry.name.encode("utf-8")
            if len(name) > 65535:
                raise NupError(f"Package entry name is too long: {entry.rel}")
            name_ranges[id(entry)] = (len(string_pool), len(name))
            string_pool.extend(name)
    if len(string_pool) > 0xFFFFFFFF - strings_offset:
        raise NupError("Package name table exceeds the NUP format limits.")
    toc = bytearray(strings_offset + len(string_pool))
    toc[:4] = b"TOC\x01"
    struct.pack_into("<IQQQQQ", toc, 4, package_count, all_dirs, len(file_entries),
                     strings_offset, len(string_pool), 0)
    for index, package in enumerate(packages):
        offset = package_offset + index * PACKAGE_SIZE
        exefs = romfs = 0xFFFFFFFF
        program1 = None
        for entry in package.entries:
            if entry.is_dir and entry.parent == package.root_dir:
                if entry.name.casefold() == "exefs":
                    exefs = local_to_global[index][entry.dir_id]
                elif entry.name.casefold() == "romfs":
                    romfs = local_to_global[index][entry.dir_id]
                elif entry.name.casefold() == "program1":
                    program1 = entry.dir_id
        # A consolidated cross-generation package keeps its executable in program1/exefs;
        # without recording it the loader would boot the legacy base program instead.
        if program1 is not None and 0xFFFFFFFF in (exefs, romfs):
            for entry in package.entries:
                if entry.is_dir and entry.parent == program1:
                    if entry.name.casefold() == "exefs" and exefs == 0xFFFFFFFF:
                        exefs = local_to_global[index][entry.dir_id]
                    elif entry.name.casefold() == "romfs" and romfs == 0xFFFFFFFF:
                        romfs = local_to_global[index][entry.dir_id]
        struct.pack_into("<QIIIIBB", toc, offset, package.title_id, index, exefs, romfs,
                         package.version, package.kind, package.flags)
        struct.pack_into("<Q I", toc, offset + 32, package.meta_offset, len(package.metadata))

    for directory_id, record in enumerate(dir_records):
        offset = dir_offset + directory_id * DIRECTORY_SIZE
        first = child_offsets[directory_id]
        count = len(dir_children[directory_id]) + len(file_children[directory_id])
        if directory_id < package_count:
            struct.pack_into("<II", toc, offset + 8, first, count)
        else:
            name_off, name_len = name_ranges[id(global_dir_entry[directory_id])]
            struct.pack_into("<IIII", toc, offset, name_off, name_len, first, count)

    for index, (package, entry) in enumerate(file_entries):
        name_off, name_len = name_ranges[id(entry)]
        offset = file_offset + index * FILE_SIZE
        struct.pack_into("<IIQQQ", toc, offset, name_off, name_len, entry.size,
                         entry.offset if entry.size else 0, entry.stored)
        toc[offset + 32 : offset + 64] = entry.digest
        toc[offset + 64] = entry.codec

    for index, child_id in enumerate(child_ids):
        struct.pack_into("<I", toc, children_offset + index * 4, child_id)
    toc[strings_offset:] = string_pool
    return bytes(toc)


def _write_container(
    packages: list[Package],
    output: Path,
    source_archive: Path | None = None,
    progress: Progress | None = None,
) -> None:
    if not packages:
        raise NupError("No packages are available to write.")
    if len(packages) > 100_000:
        raise NupError("The container has too many packages.")
    toc = _build_toc(packages)
    metadata_pos = _align(HEADER_SIZE + len(toc), 8)
    for package in packages:
        package.meta_offset = metadata_pos if package.metadata else 0
        metadata_pos += len(package.metadata)
        metadata_pos = _align(metadata_pos, 8)
    blob_offset = _align(metadata_pos, 16)
    file_pos = blob_offset
    extents: dict[tuple[int, bytes, int, int], int] = {}
    for package in packages:
        for entry in package.entries:
            if entry.is_dir:
                continue
            if not entry.size:
                entry.offset = 0
                continue
            key = (entry.size, entry.digest, entry.stored, entry.codec)
            if key in extents:
                entry.offset = extents[key]
            else:
                entry.offset = _align(file_pos, 16)
                file_pos = entry.offset + entry.stored
                extents[key] = entry.offset
    toc = _build_toc(packages)
    header = bytearray(HEADER_SIZE)
    header[:4] = b"NUP\x01"
    struct.pack_into("<HHQQ", header, 4, HEADER_SIZE, 1, HEADER_SIZE, len(toc))
    header[24:56] = hashlib.sha256(toc).digest()
    struct.pack_into("<QQII", header, 56, blob_offset, file_pos, len(packages), 0)
    uuid_data = b"".join(
        struct.pack("<QI B", package.title_id, package.version, package.kind)
        for package in packages
    )
    header[80:96] = hashlib.sha256(uuid_data).digest()[:16]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".nupstudio.tmp")
    try:
        with temporary.open("wb") as destination:
            destination.write(header)
            destination.write(toc)
            destination.seek(_align(HEADER_SIZE + len(toc), 8))
            for package in packages:
                if package.metadata:
                    destination.seek(package.meta_offset)
                    destination.write(package.metadata)
            total_work = sum(max(e.size, 1) for p in packages for e in p.entries
                             if not e.is_dir)
            completed_work = 0
            written_extents: set[tuple[int, bytes, int, int]] = set()
            input_archive = source_archive.open("rb") if source_archive else None
            try:
                for package in packages:
                    for entry in package.entries:
                        if entry.is_dir:
                            continue
                        work_size = max(entry.size, 1)
                        if not entry.size:
                            completed_work += work_size
                            _progress(progress, 5 + completed_work * 95 // max(total_work, 1),
                                      "Writing NUP container")
                            continue
                        key = (entry.size, entry.digest, entry.stored, entry.codec)
                        if key in written_extents:
                            completed_work += work_size
                            _progress(progress, 5 + completed_work * 95 // max(total_work, 1),
                                      "Writing NUP container")
                            continue
                        destination.seek(entry.offset)

                        def update_progress(copied: int) -> None:
                            logical = copied if entry.codec == 0 else copied * entry.size // entry.stored
                            percent = 5 + (completed_work + logical) * 95 // max(total_work, 1)
                            _progress(progress, percent, "Writing NUP container")

                        if source_archive:
                            input_archive.seek(entry.source_offset)
                            _copy_exact(input_archive, destination, entry.stored, update_progress)
                        elif entry.codec == 1 and entry.staged:
                            with entry.staged.open("rb") as staged:
                                _copy_exact(staged, destination, entry.stored, update_progress)
                        elif entry.source:
                            _copy_source_checked(entry, destination, update_progress)
                        else:
                            raise NupError(f"Missing file data for {entry.rel}.")
                        written_extents.add(key)
                        completed_work += work_size
            finally:
                if input_archive:
                    input_archive.close()
            destination.truncate(file_pos)
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _copy_exact(source, destination, size: int,
                progress: Callable[[int], None] | None = None) -> None:
    remaining = size
    copied = 0
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise NupError("Unexpected end of package data.")
        destination.write(chunk)
        remaining -= len(chunk)
        copied += len(chunk)
        if progress:
            progress(copied)


def _copy_source_checked(entry: Entry, destination,
                         progress: Callable[[int], None] | None = None) -> None:
    if not entry.source:
        raise NupError(f"Missing source file for {entry.rel}.")
    digest = hashlib.sha256()
    size = 0
    with entry.source.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            if progress:
                progress(size)
    if size != entry.size or digest.digest() != entry.digest:
        raise NupError(f"Source file changed while packaging: {entry.rel}")


def pack_folder(
    source: str | Path,
    output: str | Path,
    *,
    title_id: str = "",
    title: str = "",
    publisher: str = "",
    display_version: str = "",
    icon_path: str = "",
    base_version: str = "",
    update_version: str = "",
    compression_level: int = 0,
    progress: Progress | None = None,
) -> None:
    if compression_level not in (0, 1, 3, 5, 10, 19):
        raise NupError("Choose raw or one of the supported Zstandard levels.")
    source_path, output_path = Path(source).resolve(), Path(output).resolve()
    packages = _make_build_packages(source_path, title_id, title, publisher,
                                    display_version, icon_path, base_version,
                                    update_version)
    try:
        ensure_no_legacy_overwrite(packages)
    except LayoutError as exc:
        raise NupError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="nupstudio-stage-") as temp_name:
        _prepare_entries(packages, compression_level, progress, Path(temp_name))
        _write_container(packages, output_path, progress=progress)
    _progress(progress, 100, "Package complete")


def _read_container(path: str | Path) -> Container:
    archive_path = Path(path).resolve()
    try:
        size = archive_path.stat().st_size
        with archive_path.open("rb") as archive:
            header = archive.read(HEADER_SIZE)
            if len(header) != HEADER_SIZE or header[:4] != b"NUP\x01":
                raise NupError("Invalid NUP header.")
            header_size, version = struct.unpack_from("<HH", header, 4)
            toc_offset, toc_size = struct.unpack_from("<QQ", header, 8)
            blob_offset, declared_size, package_count, reserved = struct.unpack_from(
                "<QQII", header, 56
            )
            if (header_size < HEADER_SIZE or version < 1 or toc_offset != header_size
                    or toc_size > 512 * 1024 * 1024 or declared_size != size
                    or reserved or any(header[96:128]) or not package_count
                    or package_count > 100_000 or toc_offset > size
                    or toc_size > size - toc_offset or blob_offset % 16
                    or blob_offset < toc_offset + toc_size or blob_offset > size):
                raise NupError("NUP size fields or reserved bytes are inconsistent.")
            archive.seek(toc_offset)
            toc = archive.read(toc_size)
            if len(toc) < 48:
                raise NupError("The NUP table is truncated.")
            if hashlib.sha256(toc).digest() != header[24:56] or toc[:4] != b"TOC\x01":
                raise NupError("TOC signature or SHA-256 is invalid.")
            toc_packages, dir_count, file_count, strings_offset, strings_size, toc_reserved = (
                struct.unpack_from("<IQQQQQ", toc, 4)
            )
            if (toc_packages != package_count or toc_reserved or not dir_count
                    or dir_count > 1_000_000 or file_count > 10_000_000):
                raise NupError("Malformed package and node tables.")
            package_offset = 48
            dir_offset = package_offset + package_count * PACKAGE_SIZE
            file_offset = dir_offset + dir_count * DIRECTORY_SIZE
            child_offset = file_offset + file_count * FILE_SIZE
            if (child_offset > strings_offset or strings_offset > len(toc)
                    or strings_size > len(toc) - strings_offset):
                raise NupError("TOC table boundaries are invalid.")
            child_count, _child_padding_size = divmod(strings_offset - child_offset, 4)
            if (any(toc[child_offset + child_count * 4:strings_offset])
                    or any(toc[strings_offset + strings_size:])):
                raise NupError("TOC table boundaries contain unsupported non-zero padding.")
            packages: list[Package] = []
            package_uuid = bytearray()
            metadata_ranges: list[tuple[int, int]] = []
            base_count = update_count = 0
            for index in range(package_count):
                offset = package_offset + index * PACKAGE_SIZE
                title_id, root_dir, exefs, romfs, package_version = struct.unpack_from(
                    "<QIIII", toc, offset
                )
                kind, flags = toc[offset + 24], toc[offset + 25]
                meta_offset, meta_size = struct.unpack_from("<QI", toc, offset + 32)
                if (kind > 2 or flags & ~1 or any(toc[offset + 26:offset + 32])
                        or any(toc[offset + 44:offset + PACKAGE_SIZE])
                        or root_dir >= dir_count or (exefs != 0xFFFFFFFF and exefs >= dir_count)
                        or (romfs != 0xFFFFFFFF and romfs >= dir_count)):
                    raise NupError("Package table contains invalid or reserved fields.")
                if meta_size > 64 * 1024 * 1024 or (not meta_size and meta_offset) or (meta_size and (
                        not meta_offset or meta_offset < toc_offset + toc_size
                        or meta_offset > size or meta_size > size - meta_offset)):
                    raise NupError("Package metadata range is invalid.")
                if meta_size:
                    metadata_ranges.append((meta_offset, meta_offset + meta_size))
                    archive.seek(meta_offset)
                    metadata = archive.read(meta_size)
                    if len(metadata) != meta_size:
                        raise NupError("Cannot read package metadata.")
                    _parse_metadata(metadata)
                else:
                    metadata = b""
                packages.append(Package(title_id, package_version, kind, flags,
                                         metadata=metadata, root_dir=root_dir))
                package_uuid.extend(struct.pack("<QI B", title_id, package_version, kind))
                if kind == 0:
                    base_count += 1
                if kind == 1:
                    update_count += 1
            if base_count > 1 or hashlib.sha256(package_uuid).digest()[:16] != header[80:96]:
                raise NupError("NUP package count or UUID is invalid.")
            if any(
                packages[index].kind == 1
                and bool(packages[index].flags & 1) != (base_count == 0 and update_count > 0)
                for index in range(package_count)
            ):
                raise NupError("Update standalone flags do not match the container contents.")

            dirs = []
            for index in range(dir_count):
                offset = dir_offset + index * DIRECTORY_SIZE
                name_offset, name_size, first, count = struct.unpack_from("<IIII", toc, offset)
                if (any(toc[offset + 16:offset + DIRECTORY_SIZE]) or first > child_count
                        or count > child_count - first):
                    raise NupError("Directory table contains an invalid child range.")
                dirs.append((name_offset, name_size, first, count))
            child_ranges = sorted((first, count) for _, _, first, count in dirs)
            if sum(count for _, count in child_ranges) != child_count:
                raise NupError("Directory child ranges do not cover the child table.")
            cursor = 0
            for first, count in child_ranges:
                if first < cursor:
                    raise NupError("Directory child ranges overlap.")
                cursor = first + count

            files = []
            for index in range(file_count):
                offset = file_offset + index * FILE_SIZE
                name_offset, name_size, file_size, extent, stored = struct.unpack_from(
                    "<IIQQQ", toc, offset
                )
                digest = toc[offset + 32:offset + 64]
                codec = toc[offset + 64]
                if any(toc[offset + 65:offset + FILE_SIZE]) or codec > 1:
                    raise NupError("File table contains unsupported codec or reserved fields.")
                if ((codec == 0 and stored != file_size)
                        or (codec == 1 and (not file_size or not stored))
                        or ((file_size or stored) and (extent < blob_offset
                            or extent > size or stored > size - extent))
                        or (not file_size and (extent or digest != hashlib.sha256(b"").digest()))):
                    raise NupError("File extent size or range is invalid.")
                if (file_size or stored) and extent % 16:
                    raise NupError("File extent is not 16-byte aligned.")
                files.append((name_offset, name_size, file_size, extent, stored, digest, codec))

            def decode_name(offset: int, length: int) -> tuple[str, bytes]:
                if not length or length > 65535 or offset > strings_size or length > strings_size - offset:
                    raise NupError("Node name range is invalid.")
                data = toc[strings_offset + offset:strings_offset + offset + length]
                if (b"\0" in data or b"/" in data or b"\\" in data or b":" in data
                        or data in (b".", b"..")):
                    raise NupError("Package tree contains an unsafe path name.")
                try:
                    return data.decode("utf-8"), data
                except UnicodeDecodeError as exc:
                    raise NupError("Package tree contains an invalid UTF-8 name.") from exc

            children = [struct.unpack_from("<I", toc, child_offset + i * 4)[0]
                        for i in range(child_count)]
            dir_refs = [0] * dir_count
            file_refs = [0] * file_count
            dir_names: list[str | None] = [None] * dir_count
            file_names: list[str | None] = [None] * file_count
            for dir_id, (_, _, first, count) in enumerate(dirs):
                previous = None
                for child in children[first:first + count]:
                    if child >= dir_count + file_count:
                        raise NupError("Child table references a missing node.")
                    is_dir = child < dir_count
                    item_index = child if is_dir else child - dir_count
                    item = dirs[item_index] if is_dir else files[item_index]
                    name, raw_name = decode_name(item[0], item[1])
                    if previous is not None and raw_name <= previous:
                        raise NupError("Child names are duplicated or unsorted.")
                    previous = raw_name
                    if is_dir:
                        dir_names[item_index] = name
                        dir_refs[item_index] += 1
                    else:
                        file_names[item_index] = name
                        file_refs[item_index] += 1

            roots = {package.root_dir for package in packages}
            if len(roots) != package_count or any(dir_refs[root] for root in roots):
                raise NupError("Package roots are duplicated or referenced as child nodes.")
            for root in roots:
                root_name_offset, root_name_size, _, _ = dirs[root]
                if root_name_size:
                    dir_names[root], _ = decode_name(root_name_offset, root_name_size)
                elif root_name_offset:
                    raise NupError("Unnamed package roots must have a zero name offset.")
            if any(ref != 1 for index, ref in enumerate(dir_refs) if index not in roots):
                raise NupError("A directory node is missing or referenced more than once.")
            if any(ref != 1 for ref in file_refs):
                raise NupError("A file node is missing or referenced more than once.")

            all_extents: list[tuple[int, int, int, int, bytes]] = []
            all_visited_dirs: set[int] = set()
            for package in packages:
                next_local_id = package.root_dir + 1
                stack = [(package.root_dir, "", package.root_dir)]
                while stack:
                    dir_id, prefix, local_parent = stack.pop()
                    if dir_id in all_visited_dirs:
                        raise NupError("Package trees share a directory or contain a cycle.")
                    all_visited_dirs.add(dir_id)
                    _, _, first, count = dirs[dir_id]
                    directory_children = []
                    for child in children[first:first + count]:
                        is_dir = child < dir_count
                        item_index = child if is_dir else child - dir_count
                        item = dirs[item_index] if is_dir else files[item_index]
                        name = dir_names[item_index] if is_dir else file_names[item_index]
                        if name is None:
                            raise NupError("Package tree references a node without a name.")
                        relative = f"{prefix}/{name}" if prefix else name
                        if is_dir:
                            local_id = next_local_id
                            next_local_id += 1
                            package.entries.append(Entry(
                                name, local_parent, True, relative,
                                dir_id=local_id,
                            ))
                            directory_children.append((item_index, relative, local_id))
                        else:
                            _, _, file_size, extent, stored, digest, codec = item
                            package.entries.append(Entry(
                                name, local_parent, False, relative, None,
                                file_size, stored, extent, digest, codec,
                                source_offset=extent,
                            ))
                            if stored:
                                all_extents.append((extent, extent + stored, file_size, codec, digest))
                    stack.extend(reversed(directory_children))
            if len(all_visited_dirs) != dir_count:
                raise NupError("Package trees contain unreachable directories.")
            metadata_ranges.sort()
            for previous, current in zip(metadata_ranges, metadata_ranges[1:]):
                if current[0] < previous[1]:
                    raise NupError("Package metadata ranges overlap.")
            sorted_extents = sorted(all_extents)
            unique_extents: list[tuple[int, int]] = []
            previous_extent = None
            for start, end, file_size, codec, digest in sorted_extents:
                extent = (start, end)
                if previous_extent and start < previous_extent[1]:
                    previous_record = previous_extent[2]
                    if (start, end, file_size, codec, digest) != previous_record:
                        raise NupError("File extents overlap inconsistently.")
                    continue
                unique_extents.append(extent)
                previous_extent = (start, end, (start, end, file_size, codec, digest))
            meta_index = 0
            for first, second in unique_extents:
                while meta_index < len(metadata_ranges) and metadata_ranges[meta_index][1] <= first:
                    meta_index += 1
                if meta_index < len(metadata_ranges):
                    meta_start, meta_end = metadata_ranges[meta_index]
                    if first < meta_end and second > meta_start:
                        raise NupError("File extent overlaps package metadata.")
            return Container(archive_path, packages, size)
    except struct.error as exc:
        raise NupError("NUP file contains a malformed binary structure.") from exc
    except OSError as exc:
        raise NupError(f"Cannot read NUP file: {exc}") from exc


def inspect_nup(path: str | Path) -> dict:
    container = _read_container(path)
    summaries = []
    for package in container.packages:
        files = [entry for entry in package.entries if not entry.is_dir]
        metadata = _metadata_values(package.metadata)
        metadata_fields = _parse_metadata(package.metadata)
        summaries.append({
            "title_id": f"{package.title_id:016X}",
            "version": package.version,
            "kind": package.kind,
            "metadata": metadata,
            "icon_data": metadata_fields.get(3, b""),
            "raw_files": sum(entry.codec == 0 for entry in files),
            "zstd_files": sum(entry.codec == 1 for entry in files),
            "original_bytes": sum(entry.size for entry in files),
            "stored_bytes": sum(entry.stored for entry in files),
        })
    return {"path": str(container.path), "packages": summaries}


def _output_root(output: Path, package: Package) -> Path:
    if package.kind == 0:
        return output / "base"
    if package.kind == 1:
        return output / "updates" / str(package.version)
    if package.kind == 2:
        return output / "dlc" / f"{package.title_id:016X}" / str(package.version)
    raise NupError(f"Unsupported package kind: {package.kind}.")


def extract_nup(path: str | Path, output: str | Path,
                progress: Progress | None = None) -> None:
    container = _read_container(path)
    destination = Path(output).resolve()
    try:
        with container.path.open("rb") as archive:
            total = sum(max(entry.size, 1) for package in container.packages
                        for entry in package.entries if not entry.is_dir)
            completed = 0
            for package in container.packages:
                root = _output_root(destination, package)
                root.mkdir(parents=True, exist_ok=True)
                root = root.resolve()
                for entry in package.entries:
                    target = (root / Path(entry.rel)).resolve()
                    if not target.is_relative_to(root):
                        raise NupError("Archive contains an unsafe extraction path.")
                    if entry.is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    archive.seek(entry.offset)
                    digest = hashlib.sha256()
                    written = 0

                    def report_file_progress() -> None:
                        progress_value = (completed + min(written, max(entry.size, 1))) * 100 // max(total, 1)
                        _progress(progress, progress_value, f"Extracting {entry.rel}")

                    with target.open("wb") as extracted:
                        if entry.codec == 0:
                            reader = _LimitedReader(archive, entry.stored)
                            while chunk := reader.read(1024 * 1024):
                                extracted.write(chunk)
                                digest.update(chunk)
                                written += len(chunk)
                                report_file_progress()
                        else:
                            reader = _LimitedReader(archive, entry.stored)
                            try:
                                with zstandard.ZstdDecompressor().stream_reader(reader) as decoded:
                                    while chunk := decoded.read(
                                        min(1024 * 1024, entry.size - written + 1)
                                    ):
                                        if written + len(chunk) > entry.size:
                                            raise NupError(
                                                f"Decompressed file exceeds its declared size: {entry.rel}"
                                            )
                                        extracted.write(chunk)
                                        digest.update(chunk)
                                        written += len(chunk)
                                        report_file_progress()
                            except zstandard.ZstdError as exc:
                                raise NupError(f"Cannot decompress {entry.rel}: {exc}") from exc
                    if written != entry.size or digest.digest() != entry.digest:
                        raise NupError(f"Extracted file failed SHA-256 verification: {entry.rel}")
                    completed += max(entry.size, 1)
                    _progress(progress, completed * 100 // max(total, 1),
                              f"Extracting {entry.rel}")
    except OSError as exc:
        raise NupError(f"Cannot extract package: {exc}") from exc
    _progress(progress, 100, "Extraction complete")


class _LimitedReader:
    def __init__(self, source, length: int):
        self.source = source
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        size = self.remaining if size < 0 else min(size, self.remaining)
        chunk = self.source.read(size)
        self.remaining -= len(chunk)
        return chunk

    def readable(self) -> bool:
        return True


def _update_metadata(old: bytes, *, title: str, publisher: str, version: str,
                     icon_path: str) -> bytes:
    values = _parse_metadata(old)
    icon = Path(icon_path) if icon_path.strip() else None
    additions = {
        5: title.encode("utf-8") if title else None,
        6: publisher.encode("utf-8") if publisher else None,
        4: version.encode("utf-8") if version else None,
        3: icon.read_bytes() if icon else values.get(3),
    }
    nacp = values.get(2)
    if nacp and (not title or not publisher or not version):
        edited_nacp = bytearray(nacp)
        if not title:
            for index in range(16):
                start = index * 0x300
                if start + 0x200 <= len(edited_nacp):
                    edited_nacp[start : start + 0x200] = bytes(0x200)
        if not publisher:
            for index in range(16):
                start = index * 0x300 + 0x200
                if start + 0x100 <= len(edited_nacp):
                    edited_nacp[start : start + 0x100] = bytes(0x100)
        if not version and len(edited_nacp) >= 0x3070:
            edited_nacp[0x3060:0x3070] = bytes(0x10)
        additions[2] = bytes(edited_nacp)
    if icon and len(additions[3] or b"") > 64 * 1024 * 1024:
        raise NupError("Icon files larger than 64 MiB are not supported.")
    output = []
    seen = set()
    offset = 0
    while offset < len(old):
        kind, reserved, size = struct.unpack_from("<HHI", old, offset)
        end = offset + 8 + size
        if reserved or end > len(old):
            raise NupError("Package metadata contains an invalid field.")
        if kind not in additions:
            output.append(old[offset:end])
        elif kind not in seen and additions[kind] is not None:
            output.append(_tlv(kind, additions[kind]))
            seen.add(kind)
        offset = end
    for kind, value in additions.items():
        if kind not in seen and value is not None:
            output.append(_tlv(kind, value))
    return b"".join(output)


def edit_metadata(path: str | Path, output: str | Path | None = None, *,
                  title_id: str = "", title: str = "", publisher: str = "",
                  base_version: str = "", update_version: str = "",
                  display_version: str = "", icon_path: str = "",
                  package_index: int = 0,
                  progress: Progress | None = None) -> None:
    container = _read_container(path)
    if (not isinstance(package_index, int) or isinstance(package_index, bool)
            or not 0 <= package_index < len(container.packages)):
        raise NupError(f"Package index is outside the container: {package_index}.")
    package = container.packages[package_index]
    if title_id and package.kind != 2:
        package.title_id = _parse_uint(title_id, base=16, limit=0xFFFFFFFFFFFFFFFF)
    version_text = base_version if package.kind == 0 else update_version
    if version_text:
        package.version = _parse_uint(version_text, base=0)
    package.metadata = _update_metadata(
        package.metadata, title=title, publisher=publisher,
        version=display_version, icon_path=icon_path,
    )
    destination = Path(output).resolve() if output else container.path
    for existing_package in container.packages:
        for entry in existing_package.entries:
            if entry.is_dir:
                continue
            entry.source_offset = entry.offset
    _write_container(container.packages, destination, source_archive=container.path,
                     progress=progress)
    _progress(progress, 100, "Metadata saved")
