import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from nupstudio import core
from nupstudio.layout import LayoutError, ensure_no_legacy_overwrite, resolve_layout
from test_core import nacp_bytes


def touch(root: Path, rel: str, data: bytes = b"x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def cross_gen(self):
        touch(self.root, "base/exefs/main")
        touch(self.root, "base/control/control.nacp", nacp_bytes("Legacy", "Pub", "1.0"))
        touch(self.root, "updates/5/exefs/main")
        touch(self.root, "updates/5/program1/exefs/main")
        touch(self.root, "updates/5/program1/control/Acd_1/control.nacp",
              nacp_bytes("Native", "NPub", "2.0"))
        touch(self.root, "dlc/0100000000001001/romfs/a.bin")

    def test_priority_order(self):
        touch(self.root, "exefs/main")
        self.assertEqual(resolve_layout(self.root).kind, "root")
        touch(self.root, "base/exefs/main")
        self.assertEqual(resolve_layout(self.root).kind, "legacy_base")
        touch(self.root, "updates/3/exefs/main")
        self.assertEqual(resolve_layout(self.root).kind, "legacy_update")
        touch(self.root, "updates/2/program1/exefs/main")
        layout = resolve_layout(self.root)
        self.assertEqual((layout.kind, layout.version), ("native", 2))

    def test_native_metadata_and_dlc(self):
        self.cross_gen()
        layout = resolve_layout(self.root)
        self.assertTrue(layout.is_native)
        self.assertEqual(layout.metadata_dir.name, "Acd_1")
        self.assertEqual([d.name for d in layout.dlc_dirs], ["0100000000001001"])
        info = core.inspect_folder(self.root)
        self.assertEqual(info["title"], "Native")

    def test_legacy_update_metadata_fallback(self):
        touch(self.root, "base/control/control.nacp", nacp_bytes("Base", "P", "1"))
        touch(self.root, "updates/1/exefs/main")
        layout = resolve_layout(self.root)
        self.assertEqual(layout.metadata_dir, self.root / "base" / "control")

    def test_native_falls_back_per_field_in_inspection_and_pack(self):
        touch(self.root, "base/exefs/main")
        touch(self.root, "base/control/control.nacp", nacp_bytes("Base", "Base publisher", "1"))
        touch(self.root, "updates/5/program1/exefs/main")
        touch(self.root, "updates/5/program1/control/Acd_1/control.nacp",
              nacp_bytes("Native", "", ""))
        touch(self.root, "updates/5/program1/control/control.nacp",
              nacp_bytes("Other title", "Native publisher", ""))
        touch(self.root, "updates/5/control/Acd_1/control.nacp",
              nacp_bytes("Legacy update", "Other publisher", "2"))
        touch(self.root, "updates/5/control/Acd_1/icon_AmericanEnglish.dat", b"legacy icon")
        touch(self.root, "dlc/0100000000001001/control/control.nacp",
              nacp_bytes("DLC", "DLC publisher", "9"))

        info = core.inspect_folder(self.root)
        self.assertEqual((info["title"], info["publisher"], info["display_version"]),
                         ("Native", "Native publisher", "2"))
        self.assertEqual(Path(info["icon_file"]),
                         self.root / "updates/5/control/Acd_1/icon_AmericanEnglish.dat")
        archive = self.root.parent / (self.root.name + ".nup")
        self.addCleanup(archive.unlink, missing_ok=True)
        core.pack_folder(self.root, archive)
        update = next(p for p in core._read_container(archive).packages if p.kind == 1)
        values = core._metadata_values(update.metadata)
        self.assertEqual((values["title"], values["publisher"], values["version"]),
                         ("Native", "Native publisher", "2"))
        self.assertEqual(core._parse_metadata(update.metadata)[3], b"legacy icon")

    def test_missing_native_metadata_uses_legacy_not_dlc(self):
        touch(self.root, "base/exefs/main")
        touch(self.root, "base/control/control.nacp", nacp_bytes("Base", "Base publisher", "1"))
        touch(self.root, "updates/5/program1/exefs/main")
        touch(self.root, "updates/5/control/control.nacp", nacp_bytes("Update", "", "2"))
        touch(self.root, "dlc/0100000000001001/control/control.nacp",
              nacp_bytes("DLC", "DLC publisher", "9"))
        info = core.inspect_folder(self.root)
        self.assertEqual((info["title"], info["publisher"], info["display_version"]),
                         ("Update", "Base publisher", "2"))

    def test_native_values_and_icon_win_and_pack_overrides_remain(self):
        self.cross_gen()
        touch(self.root, "updates/5/program1/control/Acd_1/icon_AmericanEnglish.dat", b"native")
        touch(self.root, "updates/5/control/control.nacp", nacp_bytes("Legacy", "Old", "9"))
        touch(self.root, "updates/5/control/icon_AmericanEnglish.dat", b"legacy")
        info = core.inspect_folder(self.root)
        self.assertEqual((info["title"], info["publisher"], info["display_version"]),
                         ("Native", "NPub", "2.0"))
        self.assertEqual(Path(info["icon_file"]).read_bytes(), b"native")
        update = next(p for p in core._make_build_packages(
            self.root, title="Manual", display_version="3") if p.kind == 1)
        fields = core._metadata_values(update.metadata)
        self.assertEqual((fields["title"], fields["publisher"], fields["version"]),
                         ("Manual", "NPub", "3"))
        self.assertEqual(core._parse_metadata(update.metadata)[3], b"native")

    def test_legacy_update_unreadable_version_falls_back_to_base(self):
        touch(self.root, "base/exefs/main")
        touch(self.root, "base/control/control.nacp", nacp_bytes("Base", "Publisher", "1"))
        touch(self.root, "updates/5/exefs/main")
        invalid = bytearray(nacp_bytes("Update", "", ""))
        invalid[0x3060:0x3062] = b"\xff\x01"
        touch(self.root, "updates/5/control/Acd_1/control.nacp", bytes(invalid))
        touch(self.root, "updates/5/control/control.nacp", b"short")
        info = core.inspect_folder(self.root)
        self.assertEqual((info["title"], info["publisher"], info["display_version"]),
                         ("Update", "Publisher", "1"))
        update = next(p for p in core._make_build_packages(self.root) if p.kind == 1)
        values = core._metadata_values(update.metadata)
        self.assertEqual((values["title"], values["publisher"], values["version"]),
                         ("Update", "Publisher", "1"))

    def test_missing_metadata_does_not_fall_back_to_dlc(self):
        touch(self.root, "updates/5/program1/exefs/main")
        touch(self.root, "dlc/0100000000001001/control/control.nacp",
              nacp_bytes("DLC", "DLC publisher", "9"))
        info = core.inspect_folder(self.root)
        self.assertEqual((info["title"], info["publisher"], info["display_version"]),
                         ("", "", ""))
        update = next(p for p in core._make_build_packages(self.root) if p.kind == 1)
        self.assertNotIn(2, core._parse_metadata(update.metadata))

    def test_legacy_siblings_not_packed_with_native(self):
        self.cross_gen()
        packages = core._make_build_packages(self.root)
        update = next(p for p in packages if p.kind == 1)
        rels = {e.rel for e in update.entries}
        self.assertIn("program1/exefs/main", rels)
        self.assertFalse(any(r.split("/")[0] in ("exefs", "control") for r in rels))
        self.assertEqual(sum(p.kind == 2 for p in packages), 1)
        ensure_no_legacy_overwrite(packages)

    def test_native_program1_exefs_is_recorded_in_the_container(self):
        self.cross_gen()
        archive = self.root / "out.nup"
        core.pack_folder(self.root, archive)
        data = archive.read_bytes()
        package_count = struct.unpack_from("<I", data, 72)[0]
        toc_offset = struct.unpack_from("<Q", data, 8)[0]
        records = [struct.unpack_from("<QIIIIBB", data, toc_offset + 48 + i * 64)
                   for i in range(package_count)]
        update = next(r for r in records if r[5] == 1)
        base = next(r for r in records if r[5] == 0)
        self.assertNotEqual(base[2], 0xFFFFFFFF)  # legacy base keeps its own exefs
        self.assertNotEqual(update[2], 0xFFFFFFFF)  # update boots program1/exefs

    def test_encrypted_title_fields_are_ignored(self):
        data = bytearray(nacp_bytes("x", "y", "1.2.1"))
        data[0:0x300] = bytes([0x2A, 0x01, 0xED, 0xD6, 0x41, 0x4A, 0xC3, 0x40]) * 96
        fields = core._decode_nacp(bytes(data))
        self.assertNotIn("title", fields)
        self.assertEqual(fields["version"], "1.2.1")

    def test_overwrite_block(self):
        entry = SimpleNamespace(
            is_dir=False, rel="program1/exefs/main",
            source=self.root / "base" / "exefs" / "main",
        )
        with self.assertRaises(LayoutError):
            ensure_no_legacy_overwrite([SimpleNamespace(entries=[entry])])


if __name__ == "__main__":
    unittest.main()
