#!/usr/bin/env python3
"""Read JAR archives for jar.yazi without exposing host filesystem paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import sys
import tempfile
from typing import NoReturn
from urllib.parse import quote
import zipfile


PROTOCOL_VERSION = 1
COPY_CHUNK_SIZE = 1024 * 1024
MAX_READ_SIZE = 16 * 1024 * 1024


class ArchiveFailure(Exception):
    """A safe, expected failure that the Lua provider can show to the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Limits:
    max_entries: int = 100_000
    max_entry_size: int = 256 * 1024 * 1024
    max_path_bytes: int = 1024
    max_path_depth: int = 64

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if value <= 0:
                raise ArchiveFailure("invalid_limit", f"{name} must be positive")


@dataclass(frozen=True)
class Fingerprint:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    digest: str


@dataclass(frozen=True)
class Node:
    kind: str
    size: int
    ordinal: int | None


@dataclass(frozen=True)
class ArchiveIndex:
    fingerprint: Fingerprint
    nodes: dict[str, Node]

    def node(self, inner_path: str) -> Node:
        normalized = normalize_lookup(inner_path)
        try:
            return self.nodes[normalized]
        except KeyError as error:
            raise ArchiveFailure("not_found", f"No such archive entry: {inner_path}") from error

    def children(self, inner_path: str) -> list[tuple[str, Node]]:
        normalized = normalize_lookup(inner_path)
        parent = self.node(normalized)
        if parent.kind != "dir":
            raise ArchiveFailure("not_directory", f"Archive entry is not a directory: {inner_path}")

        prefix = f"{normalized}/" if normalized else ""
        children: list[tuple[str, Node]] = []
        for path, node in self.nodes.items():
            if path == normalized or not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            if "/" not in remainder:
                children.append((remainder, node))
        return sorted(children, key=lambda item: (item[1].kind != "dir", item[0].casefold(), item[0]))


def fail(code: str, message: str) -> NoReturn:
    raise ArchiveFailure(code, message)


def fingerprint(archive: Path) -> Fingerprint:
    try:
        resolved = archive.expanduser().resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        raise ArchiveFailure("archive_unavailable", f"Cannot access JAR: {error}") from error

    if not resolved.is_file():
        fail("not_file", f"JAR path is not a regular file: {resolved}")
    if resolved.suffix.casefold() != ".jar":
        fail("not_jar", f"Expected a .jar file: {resolved}")

    identity = "\0".join(
        (
            os.fspath(resolved),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(stat.st_dev),
            str(stat.st_ino),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8", "surrogateescape")).hexdigest()
    return Fingerprint(
        path=os.fspath(resolved),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
        digest=digest,
    )


def normalize_lookup(value: str) -> str:
    if value in ("", "."):
        return ""
    return normalize_path(value, is_directory=value.endswith("/"))


def normalize_path(value: str, *, is_directory: bool) -> str:
    if "\0" in value:
        fail("unsafe_path", "Archive entry contains NUL")
    if "\\" in value:
        fail("unsafe_path", f"Archive entry contains a backslash: {value!r}")
    if value.startswith("/"):
        fail("unsafe_path", f"Archive entry is absolute: {value!r}")

    normalized = value[:-1] if is_directory and value.endswith("/") else value
    parts = normalized.split("/")
    if not normalized or any(part in ("", ".", "..") for part in parts):
        fail("unsafe_path", f"Archive entry has an unsafe path: {value!r}")

    return normalized


def validate_normalized_path(path: str, limits: Limits) -> None:
    try:
        byte_length = len(path.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ArchiveFailure("invalid_name", "Archive entry name is not valid Unicode") from error

    if byte_length > limits.max_path_bytes:
        fail("path_limit", f"Archive entry path exceeds {limits.max_path_bytes} bytes")
    if len(path.split("/")) > limits.max_path_depth:
        fail("depth_limit", f"Archive entry path exceeds {limits.max_path_depth} segments")


def build_index(archive: Path, limits: Limits) -> ArchiveIndex:
    limits.validate()
    before = fingerprint(archive)
    nodes: dict[str, Node] = {"": Node(kind="dir", size=0, ordinal=None)}
    explicit_directories: set[str] = set()

    try:
        with zipfile.ZipFile(before.path, "r") as jar:
            entries = jar.infolist()
            if len(entries) > limits.max_entries:
                fail("entry_limit", f"JAR contains more than {limits.max_entries} entries")

            for ordinal, info in enumerate(entries):
                is_directory = info.is_dir() or info.filename.endswith("/")
                path = normalize_path(info.filename, is_directory=is_directory)
                validate_normalized_path(path, limits)

                parts = path.split("/")
                for depth in range(1, len(parts)):
                    parent_path = "/".join(parts[:depth])
                    existing_parent = nodes.get(parent_path)
                    if existing_parent and existing_parent.kind != "dir":
                        fail("path_collision", f"File blocks directory path: {parent_path!r}")
                    nodes.setdefault(parent_path, Node(kind="dir", size=0, ordinal=None))

                existing = nodes.get(path)
                if is_directory:
                    if existing and existing.kind != "dir":
                        fail("path_collision", f"File and directory share a path: {path!r}")
                    if path in explicit_directories:
                        fail("duplicate_entry", f"Duplicate directory entry: {path!r}")
                    nodes.setdefault(path, Node(kind="dir", size=0, ordinal=None))
                    explicit_directories.add(path)
                else:
                    if existing:
                        code = "duplicate_entry" if existing.kind == "file" else "path_collision"
                        fail(code, f"Ambiguous archive entry path: {path!r}")
                    if info.file_size > limits.max_entry_size:
                        fail("entry_size_limit", f"Archive entry exceeds {limits.max_entry_size} bytes: {path!r}")
                    nodes[path] = Node(kind="file", size=info.file_size, ordinal=ordinal)

                if len(nodes) > limits.max_entries:
                    fail("entry_limit", f"Expanded JAR tree exceeds {limits.max_entries} entries")
    except zipfile.BadZipFile as error:
        raise ArchiveFailure("invalid_jar", f"Invalid or corrupt JAR: {error}") from error
    except OSError as error:
        raise ArchiveFailure("archive_unavailable", f"Cannot read JAR: {error}") from error

    after = fingerprint(Path(before.path))
    if after != before:
        fail("source_changed", "JAR changed while its directory was being read")
    return ArchiveIndex(fingerprint=before, nodes=nodes)


def cache_root(path: Path) -> Path:
    root = path.expanduser()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise ArchiveFailure("cache_unavailable", f"Cannot create cache directory: {error}") from error
    if root.is_symlink() or not root.is_dir():
        fail("unsafe_cache", f"Cache path must be a real directory: {root}")
    return root


def index_cache_path(root: Path, archive_fingerprint: Fingerprint) -> Path:
    return root / "indexes" / f"{archive_fingerprint.digest}.json"


def serialize_index(index: ArchiveIndex) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "fingerprint": asdict(index.fingerprint),
        "nodes": {path: asdict(node) for path, node in index.nodes.items()},
    }


def deserialize_index(data: object, expected: Fingerprint) -> ArchiveIndex:
    if not isinstance(data, dict) or data.get("version") != PROTOCOL_VERSION:
        fail("invalid_cache", "Cached archive index has an unsupported version")
    if data.get("fingerprint") != asdict(expected):
        fail("invalid_cache", "Cached archive index has the wrong fingerprint")

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict):
        fail("invalid_cache", "Cached archive index has no node table")

    nodes: dict[str, Node] = {}
    try:
        for path, raw in raw_nodes.items():
            if not isinstance(path, str) or not isinstance(raw, dict):
                fail("invalid_cache", "Cached archive index contains an invalid node")
            kind = raw["kind"]
            size = raw["size"]
            ordinal = raw["ordinal"]
            if kind not in ("dir", "file") or not isinstance(size, int):
                fail("invalid_cache", "Cached archive index contains invalid metadata")
            if ordinal is not None and not isinstance(ordinal, int):
                fail("invalid_cache", "Cached archive index contains an invalid ordinal")
            nodes[path] = Node(kind=kind, size=size, ordinal=ordinal)
    except KeyError as error:
        raise ArchiveFailure("invalid_cache", "Cached archive index is incomplete") from error
    return ArchiveIndex(fingerprint=expected, nodes=nodes)


def write_json_atomic(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def load_index(archive: Path, limits: Limits, root: Path) -> ArchiveIndex:
    current = fingerprint(archive)
    path = index_cache_path(root, current)
    try:
        with path.open("r", encoding="utf-8") as stream:
            return deserialize_index(json.load(stream), current)
    except (OSError, json.JSONDecodeError, ArchiveFailure):
        index = build_index(archive, limits)
        write_json_atomic(path, serialize_index(index))
        return index


def materialized_path(root: Path, index: ArchiveIndex, inner_path: str, node: Node) -> Path:
    identity = f"{node.ordinal}\0{inner_path}".encode("utf-8")
    entry_digest = hashlib.sha256(identity).hexdigest()
    suffix = Path(inner_path).suffix
    if not (2 <= len(suffix) <= 17 and suffix[1:].isalnum()):
        suffix = ""
    return root / "entries" / index.fingerprint.digest / f"{entry_digest}{suffix}"


def materialize(archive: Path, inner_path: str, limits: Limits, root: Path) -> Path:
    index = load_index(archive, limits, root)
    normalized = normalize_lookup(inner_path)
    node = index.node(normalized)
    if node.kind != "file" or node.ordinal is None:
        fail("not_file", f"Archive entry is not a file: {inner_path}")

    destination = materialized_path(root, index, normalized, node)
    try:
        if destination.stat().st_size == node.size:
            return destination
    except OSError:
        pass

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with zipfile.ZipFile(index.fingerprint.path, "r") as jar:
            entries = jar.infolist()
            if node.ordinal >= len(entries):
                fail("source_changed", "JAR entry table changed before extraction")
            info = entries[node.ordinal]
            actual_path = normalize_path(info.filename, is_directory=info.is_dir() or info.filename.endswith("/"))
            if actual_path != normalized or info.file_size != node.size:
                fail("source_changed", "JAR entry changed before extraction")
            if info.flag_bits & 0x1:
                fail("encrypted_entry", f"Encrypted JAR entry is unsupported: {inner_path}")

            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                written = 0
                with jar.open(info, "r") as source:
                    while chunk := source.read(COPY_CHUNK_SIZE):
                        written += len(chunk)
                        if written > limits.max_entry_size or written > node.size:
                            fail("entry_size_limit", f"Archive entry exceeded its declared limit: {inner_path}")
                        output.write(chunk)
                if written != node.size:
                    fail("truncated_entry", f"Archive entry ended after {written} bytes: {inner_path}")
                output.flush()
                os.fsync(output.fileno())

        if fingerprint(Path(index.fingerprint.path)) != index.fingerprint:
            fail("source_changed", "JAR changed while an entry was being extracted")
        os.replace(temporary, destination)
        temporary = None
        return destination
    except zipfile.BadZipFile as error:
        raise ArchiveFailure("invalid_jar", f"Invalid or corrupt JAR entry: {error}") from error
    except OSError as error:
        raise ArchiveFailure("archive_unavailable", f"Cannot extract JAR entry: {error}") from error
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def encoded(value: str) -> str:
    return quote(value, safe="")


def node_line(record: str, name: str, node: Node, mtime_ns: int) -> str:
    return "\t".join((record, encoded(name), node.kind, str(node.size), str(mtime_ns // 1_000_000_000)))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe JAR backend for jar.yazi")
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--max-entries", type=int, default=Limits.max_entries)
    parser.add_argument("--max-entry-size", type=int, default=Limits.max_entry_size)
    parser.add_argument("--max-path-bytes", type=int, default=Limits.max_path_bytes)
    parser.add_argument("--max-path-depth", type=int, default=Limits.max_path_depth)

    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("stat", "list", "prepare"):
        subparser = commands.add_parser(command)
        subparser.add_argument("archive", type=Path)
        subparser.add_argument("inner_path")
    read = commands.add_parser("read")
    read.add_argument("archive", type=Path)
    read.add_argument("inner_path")
    read.add_argument("offset", type=int)
    read.add_argument("length", type=int)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    limits = Limits(
        max_entries=args.max_entries,
        max_entry_size=args.max_entry_size,
        max_path_bytes=args.max_path_bytes,
        max_path_depth=args.max_path_depth,
    )
    limits.validate()
    root = cache_root(args.cache_dir)

    if args.command == "stat":
        index = load_index(args.archive, limits, root)
        node = index.node(args.inner_path)
        name = normalize_lookup(args.inner_path).rsplit("/", 1)[-1]
        print(node_line("NODE", name, node, index.fingerprint.mtime_ns))
        return

    if args.command == "list":
        index = load_index(args.archive, limits, root)
        print(f"INDEX\t{PROTOCOL_VERSION}\t{index.fingerprint.digest}")
        for name, node in index.children(args.inner_path):
            print(node_line("ENTRY", name, node, index.fingerprint.mtime_ns))
        return

    if args.command == "prepare":
        path = materialize(args.archive, args.inner_path, limits, root)
        print(f"READY\t{encoded(os.fspath(path))}")
        return

    if args.command == "read":
        if args.offset < 0 or args.length < 0 or args.length > MAX_READ_SIZE:
            fail("invalid_range", f"Read range must be within {MAX_READ_SIZE} bytes")
        path = materialize(args.archive, args.inner_path, limits, root)
        with path.open("rb") as stream:
            stream.seek(args.offset)
            sys.stdout.buffer.write(stream.read(args.length))
        return

    fail("invalid_command", f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        run(parse_args(sys.argv[1:] if argv is None else argv))
        return 0
    except ArchiveFailure as error:
        print(f"{error.code}\t{encoded(str(error))}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"internal_error\t{encoded(str(error))}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
