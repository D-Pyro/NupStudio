import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nupstudio import core


def nacp_bytes(title="", publisher="", version=""):
    data = bytearray(0x3070)
    for offset, value in ((0, title), (0x200, publisher), (0x3060, version)):
        encoded = value.encode("utf-8")
        data[offset:offset + len(encoded)] = encoded
    return bytes(data)


class CoreTests(unittest.TestCase):
    def test_nacp_rank_preferences_and_depth(self):
        def rank(path: str) -> tuple[int, int, str]:
            result = core._nacp_rank(path)
            self.assertIsNotNone(result)
            assert result is not None
            return result

        paths = [
            "other/control.nacp",
            "control.nacp",
            "updates/65536/other/control.nacp",
            "romfs/control.nacp",
            "updates/65536/program1/control/control.nacp",
        ]
        self.assertEqual(sorted(paths, key=rank), list(reversed(paths)))
        self.assertLess(
            rank("control/control.nacp"),
            rank("updates/65536/program1/control/control.nacp"),
        )
        self.assertEqual(rank("a/b/c/d/e/control.nacp")[1], 6)
        self.assertIsNone(core._nacp_rank("a/b/c/d/e/f/control.nacp"))
        self.assertIsNone(core._nacp_rank("control/other.nacp"))
        self.assertLess(rank("program1/x/control.nacp"), rank("control.nacp"))

    def test_nested_packages_metadata_and_packing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "base"
            (base / "exefs").mkdir(parents=True)
            (base / "exefs" / "main.npdm").write_bytes(
                b"ACI0" + bytes(12) + struct.pack("<Q", 0x0100123456789000)
            )
            update = root / "updates" / "65536"
            control = update / "program1" / "control"
            control.mkdir(parents=True)
            (control / "control.nacp").write_bytes(nacp_bytes("Update", "Publisher", "1.2"))
            (control / "icon_americanenglish.dat").write_bytes(b"icon")
            dlc = root / "dlc" / "0100123456789001" / "2" / "assets"
            dlc.mkdir(parents=True)
            (dlc / "file.bin").write_bytes(b"dlc")

            detected = core.inspect_folder(root)
            self.assertEqual(detected["title_id"], "0100123456789000")
            self.assertEqual((detected["title"], detected["publisher"],
                              detected["display_version"]), ("Update", "Publisher", "1.2"))
            self.assertEqual(detected["icon_file"], str(control / "icon_americanenglish.dat"))
            self.assertEqual((detected["package_kinds"], detected["update_version"]), ("0,1,2", "65536"))

            archive = root / "test.nup"
            core.pack_folder(root, archive)
            container = core._read_container(archive)
            self.assertEqual([(p.kind, p.version, p.title_id) for p in container.packages], [
                (0, 0, 0x0100123456789000),
                (1, 65536, 0x0100123456789000),
                (2, 2, 0x0100123456789001),
            ])
            self.assertIn("program1/control/control.nacp",
                          [entry.rel for entry in container.packages[1].entries])
            self.assertEqual(core._parse_metadata(container.packages[1].metadata)[3], b"icon")

    def test_nacp_missing_short_disappeared_and_empty_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "exefs").mkdir()
            (root / "romfs").mkdir()
            (root / "main.npdm").write_bytes(
                b"ACI0" + bytes(12) + struct.pack("<Q", 0x0100123456789000)
            )
            nacp = root / "romfs" / "control.nacp"
            for content in (None, b"short"):
                if content is not None:
                    nacp.write_bytes(content)
                detected = core.inspect_folder(root)
                self.assertEqual([detected[key] for key in
                                  ("title_id", "title", "publisher", "display_version", "icon_file")],
                                 ["", "", "", "", ""])
            stale = core.Entry("control.nacp", 0, False, "control.nacp",
                               source=root / "missing.nacp", size=0x3070)
            truncated = core.Entry("control.nacp", 0, False, "romfs/control.nacp",
                                   source=nacp, size=0x3070)
            self.assertIsNone(core._find_nacp([stale, truncated]))
            nacp.write_bytes(nacp_bytes(publisher="Publisher"))
            self.assertEqual(core.inspect_folder(root)["publisher"], "Publisher")

    def test_candidate_selection_depth_and_adjacent_icon(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "exefs").mkdir()
            preferred = root / "a" / "b" / "c" / "d" / "control"
            preferred.mkdir(parents=True)
            (preferred / "control.nacp").write_bytes(b"short")
            (root / "control.nacp").write_bytes(nacp_bytes("Root"))
            (root / "icon_other.dat").write_bytes(b"root icon")
            (preferred / "icon_americanenglish.dat").write_bytes(b"preferred icon")
            detected = core.inspect_folder(root)
            self.assertEqual(detected["title"], "Root")
            self.assertEqual(detected["icon_file"], str(root / "icon_other.dat"))

            (preferred / "control.nacp").write_bytes(nacp_bytes("Six components"))
            detected = core.inspect_folder(root)
            self.assertEqual(detected["title"], "Six components")
            self.assertEqual(detected["icon_file"], str(preferred / "icon_americanenglish.dat"))
            too_deep = root / "a" / "b" / "c" / "d" / "e" / "control"
            too_deep.mkdir(parents=True)
            (too_deep / "control.nacp").write_bytes(nacp_bytes("Too deep"))
            (preferred / "control.nacp").unlink()
            self.assertEqual(core.inspect_folder(root)["title"], "Root")

    def test_metadata_crawl_prunes_assets_but_packing_keeps_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "romfs" / "textures" / "large"
            assets.mkdir(parents=True)
            (assets / "file.bin").write_bytes(b"asset")
            scanned = []
            scandir = os.scandir

            def record_scandir(path):
                scanned.append(Path(path))
                return scandir(path)

            with patch.object(core.os, "scandir", side_effect=record_scandir):
                self.assertEqual(core.inspect_folder(root)["title"], "")
            self.assertNotIn(assets.parent, scanned)
            archive = root.parent / (root.name + ".nup")
            try:
                core.pack_folder(root, archive)
                package = core._read_container(archive).packages[0]
                self.assertIn("romfs/textures/large/file.bin",
                              [entry.rel for entry in package.entries])
            finally:
                archive.unlink(missing_ok=True)

    def test_romfs_control_directory_is_not_pruned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "romfs" / "control"
            control.mkdir(parents=True)
            (control / "control.nacp").write_bytes(nacp_bytes("Nested"))
            self.assertEqual(core.inspect_folder(root)["title"], "Nested")

    def test_metadata_edit_preserves_deduplicated_file_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "base"
            romfs = package / "romfs"
            romfs.mkdir(parents=True)
            content = bytes(range(256)) * 256
            (romfs / "first.bin").write_bytes(content)
            (romfs / "second.bin").write_bytes(content)
            archive = root / "test.nup"

            core.pack_folder(package, archive, title="A")
            packed_size = archive.stat().st_size
            core.edit_metadata(archive, title="B")

            edited = core._read_container(archive)
            file_entries = [
                entry for entry in edited.packages[0].entries if not entry.is_dir
            ]
            self.assertEqual(len(file_entries), 2)
            self.assertEqual(file_entries[0].offset, file_entries[1].offset)
            self.assertEqual(archive.stat().st_size, packed_size)


if __name__ == "__main__":
    unittest.main()
