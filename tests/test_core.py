import tempfile
import unittest
from pathlib import Path

from nupstudio import core


class CoreTests(unittest.TestCase):
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
