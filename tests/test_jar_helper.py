from __future__ import annotations

import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

import jar_helper


class JarHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.limits = jar_helper.Limits()

    def jar(self, entries: list[tuple[str, bytes]], name: str = "fixture.jar") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry_name, content in entries:
                archive.writestr(entry_name, content)
        return path

    def failure_code(self, callable_) -> str:
        with self.assertRaises(jar_helper.ArchiveFailure) as raised:
            callable_()
        return raised.exception.code

    def test_synthesizes_directories_and_lists_immediate_children(self) -> None:
        archive = self.jar(
            [
                ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n"),
                ("dev/gabber/App.class", b"class bytes"),
                ("read me.txt", b"hello"),
            ]
        )

        index = jar_helper.build_index(archive, self.limits)

        self.assertEqual(index.node("META-INF").kind, "dir")
        self.assertEqual(
            [(name, node.kind) for name, node in index.children("")],
            [("dev", "dir"), ("META-INF", "dir"), ("read me.txt", "file")],
        )
        self.assertEqual(
            [(name, node.kind) for name, node in index.children("dev/gabber")],
            [("App.class", "file")],
        )

    def test_accepts_one_explicit_directory_entry(self) -> None:
        archive = self.jar([("empty/", b"")])

        index = jar_helper.build_index(archive, self.limits)

        self.assertEqual(index.node("empty").kind, "dir")
        self.assertEqual(index.children("empty"), [])

    def test_materializes_only_requested_entry(self) -> None:
        archive = self.jar([("a.txt", b"alpha"), ("b.txt", b"beta")])

        extracted = jar_helper.materialize(archive, "b.txt", self.limits, jar_helper.cache_root(self.cache))

        self.assertEqual(extracted.read_bytes(), b"beta")
        entry_files = [path for path in (self.cache / "entries").rglob("*") if path.is_file()]
        self.assertEqual(entry_files, [extracted])

    def test_materialized_path_preserves_only_safe_extensions(self) -> None:
        archive = self.jar([("Thing.class", b"class"), ("odd.bad:name", b"odd")])
        root = jar_helper.cache_root(self.cache)

        class_file = jar_helper.materialize(archive, "Thing.class", self.limits, root)
        odd_file = jar_helper.materialize(archive, "odd.bad:name", self.limits, root)

        self.assertEqual(class_file.suffix, ".class")
        self.assertEqual(odd_file.suffix, "")

    def test_reuses_materialized_entry_for_unchanged_archive(self) -> None:
        archive = self.jar([("item.txt", b"content")])
        root = jar_helper.cache_root(self.cache)

        first = jar_helper.materialize(archive, "item.txt", self.limits, root)
        first_stat = first.stat()
        second = jar_helper.materialize(archive, "item.txt", self.limits, root)

        self.assertEqual(second, first)
        self.assertEqual(second.stat().st_mtime_ns, first_stat.st_mtime_ns)

    def test_source_change_uses_new_cache_identity(self) -> None:
        archive = self.jar([("item.txt", b"old")])
        root = jar_helper.cache_root(self.cache)
        old_path = jar_helper.materialize(archive, "item.txt", self.limits, root)

        archive = self.jar([("item.txt", b"new content")])
        os.utime(archive, ns=(archive.stat().st_atime_ns, archive.stat().st_mtime_ns + 1_000_000))
        new_path = jar_helper.materialize(archive, "item.txt", self.limits, root)

        self.assertNotEqual(new_path, old_path)
        self.assertEqual(new_path.read_bytes(), b"new content")

    def test_rejects_non_jar_even_when_it_is_zip_compatible(self) -> None:
        archive = self.jar([("item.txt", b"content")], name="fixture.zip")

        self.assertEqual(self.failure_code(lambda: jar_helper.build_index(archive, self.limits)), "not_jar")

    def test_rejects_invalid_jar(self) -> None:
        archive = self.root / "invalid.jar"
        archive.write_bytes(b"not a zip archive")

        self.assertEqual(self.failure_code(lambda: jar_helper.build_index(archive, self.limits)), "invalid_jar")

    def test_rejects_traversal_absolute_and_empty_segments(self) -> None:
        for entry in ("../escape", "/absolute", "a//b"):
            with self.subTest(entry=entry):
                archive = self.jar([(entry, b"content")], name=hash_name(entry))
                self.assertEqual(self.failure_code(lambda: jar_helper.build_index(archive, self.limits)), "unsafe_path")

    def test_rejects_backslash_path(self) -> None:
        self.assertEqual(
            self.failure_code(lambda: jar_helper.normalize_path("dir\\file", is_directory=False)),
            "unsafe_path",
        )

    def test_rejects_duplicate_file_entries(self) -> None:
        archive = self.root / "duplicate.jar"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w") as jar:
                jar.writestr("same.txt", b"first")
                jar.writestr("same.txt", b"second")

        self.assertEqual(self.failure_code(lambda: jar_helper.build_index(archive, self.limits)), "duplicate_entry")

    def test_rejects_file_and_directory_collision(self) -> None:
        archive = self.jar([("blocked", b"file"), ("blocked/child", b"child")])

        self.assertEqual(self.failure_code(lambda: jar_helper.build_index(archive, self.limits)), "path_collision")

    def test_rejects_entry_and_expanded_tree_limits(self) -> None:
        archive = self.jar([("a/b/c.txt", b"content")])

        self.assertEqual(
            self.failure_code(lambda: jar_helper.build_index(archive, jar_helper.Limits(max_entries=2))),
            "entry_limit",
        )

    def test_rejects_entry_size_path_size_and_depth_limits(self) -> None:
        archive = self.jar([("a/big.txt", b"12345")])

        self.assertEqual(
            self.failure_code(lambda: jar_helper.build_index(archive, jar_helper.Limits(max_entry_size=4))),
            "entry_size_limit",
        )
        self.assertEqual(
            self.failure_code(lambda: jar_helper.build_index(archive, jar_helper.Limits(max_path_bytes=3))),
            "path_limit",
        )
        self.assertEqual(
            self.failure_code(lambda: jar_helper.build_index(archive, jar_helper.Limits(max_path_depth=1))),
            "depth_limit",
        )

    def test_symlink_metadata_is_not_followed(self) -> None:
        archive = self.root / "symlink.jar"
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(archive, "w") as jar:
            jar.writestr(info, "../../outside")

        extracted = jar_helper.materialize(archive, "link", self.limits, jar_helper.cache_root(self.cache))

        self.assertFalse(extracted.is_symlink())
        self.assertEqual(extracted.read_text(), "../../outside")

    def test_cli_protocol_encodes_names_without_control_delimiters(self) -> None:
        archive = self.jar([("line\nbreak.txt", b"content")])
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = jar_helper.main(["--cache-dir", os.fspath(self.cache), "list", os.fspath(archive), ""])

        self.assertEqual(exit_code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0].split("\t")[:2], ["INDEX", "1"])
        self.assertIn("line%0Abreak.txt", lines[1])

    def test_corrupt_index_cache_is_rebuilt(self) -> None:
        archive = self.jar([("item.txt", b"content")])
        root = jar_helper.cache_root(self.cache)
        current = jar_helper.fingerprint(archive)
        cache_path = jar_helper.index_cache_path(root, current)
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text("not json")

        index = jar_helper.load_index(archive, self.limits, root)

        self.assertEqual(index.node("item.txt").size, 7)

    def test_cli_read_returns_the_requested_range(self) -> None:
        archive = self.jar([("item.txt", b"0123456789")])
        stdout = io.BytesIO()
        text = io.TextIOWrapper(stdout, encoding="utf-8")

        with contextlib.redirect_stdout(text):
            exit_code = jar_helper.main(
                ["--cache-dir", os.fspath(self.cache), "read", os.fspath(archive), "item.txt", "3", "4"]
            )
            text.flush()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), b"3456")


def hash_name(value: str) -> str:
    return f"{hashlib.sha256(value.encode()).hexdigest()}.jar"


if __name__ == "__main__":
    unittest.main()
