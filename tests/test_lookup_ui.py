import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel

from nupstudio import core, ns2db
from nupstudio.app import MainWindow
from nupstudio.lookup import LookupPanel, ReviewDialog


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    if not predicate():
        raise AssertionError("Timed out waiting for Qt background work")


def png_bytes():
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0xFF336699)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise AssertionError("Could not encode test image")
    return bytes(buffer.data())


class FakeReview:
    selection = set()

    def __init__(self, *args):
        pass

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected(self):
        return self.selection


class LookupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        wait_for(lambda: not self.window.pack_lookup.workers and
                 not self.window.edit_lookup.workers and
                 not self.window._pack_inspect_workers)
        self.window.close()

    def test_async_inspection_prompts_for_missing_nacp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "romfs").mkdir()
            self.window.pack_source.edit.setText(str(root))
            self.window._load_pack_fields()
            wait_for(lambda: not self.window._pack_inspect_workers)
            self.assertIn("enter title, publisher, and display version manually",
                          self.window.statusBar().currentMessage())
            self.assertEqual(self.window.pack_title.text(), "")

    def test_multiple_results_require_selection_and_review_is_opt_in(self):
        panel = self.window.pack_lookup
        panel._show_results([
            {"title": "Game", "titleid": "0123456789ABCDEF", "region": "US"},
            {"title": "Game", "titleid": "0123456789ABCDEF", "region": "EU"},
        ])
        self.assertFalse(panel.review_button.isEnabled())
        self.assertEqual(self.window.pack_title.text(), "")
        panel.results.setCurrentIndex(2)
        self.assertTrue(panel.review_button.isEnabled())
        FakeReview.selection = {"title"}
        with patch("nupstudio.lookup.ReviewDialog", FakeReview):
            panel.review()
        self.assertEqual(self.window.pack_title.text(), "Game")

    def test_review_defaults_preserve_existing_fields_and_icon(self):
        dialog = ReviewDialog({"title": "Existing"}, {"title": "New", "publisher": "Publisher"},
                              [], "https://example.com/image", "Scope", self.window)
        self.assertFalse(dialog.checks["title"].isChecked())
        self.assertTrue(dialog.checks["publisher"].isChecked())
        self.assertFalse(dialog.checks["icon_path"].isChecked())
        self.assertEqual(dialog.checks["title"].text(), "Use NS2DB value: New")
        self.assertEqual(dialog.checks["title"].toolTip(), "Current value: Existing")
        self.assertEqual(dialog.checks["publisher"].toolTip(), "Current value: Not set")
        self.assertEqual(dialog.selected(), {"publisher"})
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("Title\nCurrent value: Existing", labels)
        self.assertIn("Publisher\nCurrent value: Not set", labels)
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertEqual(buttons.button(QDialogButtonBox.StandardButton.Ok).text(),
                         "Apply selected")

    def test_automatic_lookup_and_stale_results(self):
        panel = self.window.pack_lookup
        started = threading.Event()
        release = threading.Event()

        def delayed(mode, query):
            started.set()
            release.wait(5)
            return [{"title": "Old", "titleid": query}]

        with patch.object(ns2db, "search", side_effect=delayed):
            panel.reset("0123456789ABCDEF")
            self.assertTrue(started.wait(2))
            panel.reset()
            release.set()
            wait_for(lambda: not panel.workers)
        self.assertEqual(panel.results.count(), 0)
        self.assertEqual(self.window.pack_title.text(), "")

    def test_cover_is_optional_preserved_and_available_until_close(self):
        panel = self.window.pack_lookup
        self.window.pack_title.setText("Old")
        panel._show_results([{"title": "New", "cover_url": "https://example.com/image"}])
        FakeReview.selection = {"title", "icon_path"}
        with patch("nupstudio.lookup.ReviewDialog", FakeReview), \
                patch.object(ns2db, "download_cover", return_value=png_bytes()):
            panel.review()
            wait_for(lambda: not panel.workers)
        self.assertEqual(self.window.pack_title.text(), "New")
        icon = Path(self.window.pack_icon.edit.text())
        self.assertTrue(icon.is_file())
        self.assertEqual(icon.read_bytes(), png_bytes())
        self.assertFalse(self.window.pack_icon_preview._source.isNull())
        self.assertTrue(self.window.close())
        self.assertFalse(icon.exists())

    def test_cover_failure_and_changed_metadata_do_not_apply_partial_values(self):
        panel = self.window.pack_lookup
        panel._show_results([{"title": "New", "cover_url": "https://example.com/image"}])
        FakeReview.selection = {"title", "icon_path"}
        with patch("nupstudio.lookup.ReviewDialog", FakeReview), \
                patch.object(ns2db, "download_cover", return_value=b"not an image"):
            panel.review()
            wait_for(lambda: not panel.workers)
        self.assertEqual(self.window.pack_title.text(), "")
        self.assertIn("not a supported image", panel.status.text())
        panel._apply_cover(png_bytes(), {"title": "New"},
                           {"title": "outdated"}, panel.generation)
        self.assertEqual(self.window.pack_title.text(), "")
        self.assertIn("changed", panel.status.text())

    def test_inflight_cover_is_discarded_after_source_change(self):
        panel = self.window.pack_lookup
        panel._show_results([{"title": "New", "cover_url": "https://example.com/image"}])
        started = threading.Event()
        release = threading.Event()

        def delayed_cover(_url):
            started.set()
            release.wait(5)
            return png_bytes()

        FakeReview.selection = {"title", "icon_path"}
        with patch("nupstudio.lookup.ReviewDialog", FakeReview), \
                patch.object(ns2db, "download_cover", side_effect=delayed_cover):
            panel.review()
            self.assertTrue(started.wait(2))
            self.window.pack_source.edit.setText("another source")
            release.set()
            wait_for(lambda: not panel.workers)
        self.assertEqual(self.window.pack_title.text(), "")
        self.assertEqual(self.window.pack_icon.edit.text(), "")

    def test_dlc_edit_does_not_offer_title_id_or_base_version(self):
        self.window._edit_packages = [{"kind": 2}]
        self.window._active_edit_package = 0
        allowed = self.window._allowed_edit_lookup_fields()
        self.assertNotIn("title_id", allowed)
        self.assertNotIn("base_version", allowed)
        self.assertIn("update_version", allowed)
        self.window._active_edit_package = None

    def test_manual_name_search_and_automatic_folder_lookup(self):
        panel = self.window.pack_lookup
        results = [{"title": "Game", "titleid": "0123456789ABCDEF",
                    "publisher": "Publisher", "base_version": "3"}]
        with patch.object(ns2db, "search", return_value=results) as search:
            panel.mode.setCurrentIndex(1)
            panel.query.setText("Game")
            panel.search()
            wait_for(lambda: not panel.workers)
            search.assert_called_with("q", "Game")
            self.window.pack_source.edit.setText("example folder")
            self.window._last_pack_source = "example folder"
            self.window._apply_pack_fields("example folder", {
                "title_id": "0123456789ABCDEF", "package_kinds": "0,1",
                "package_count": "2",
            })
            wait_for(lambda: not panel.workers)
            search.assert_called_with("titleid", "0123456789ABCDEF")
            self.assertIn("base_version", self.window._pack_lookup_fields)
            self.assertNotIn("update_version", self.window._pack_lookup_fields)
            self.assertEqual(self.window.pack_title.text(), "")
            FakeReview.selection = {"title", "publisher", "base_version"}
            with patch("nupstudio.lookup.ReviewDialog", FakeReview), \
                    patch.object(ns2db, "download_cover") as download:
                panel.review()
                download.assert_not_called()
            self.assertEqual(self.window.pack_title.text(), "Game")
            self.assertEqual(self.window.pack_base_version.text(), "3")

    def test_pack_applied_values_are_written(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "base" / "romfs"
            folder.mkdir(parents=True)
            (folder / "file.bin").write_bytes(b"data")
            self.window._apply_lookup_pack_values({
                "title_id": "0123456789ABCDEF", "title": "Reviewed",
                "publisher": "Publisher", "base_version": "9",
                "display_version": "1.2",
            })
            values = self.window._current_pack_values()
            nup = root / "packed.nup"
            core.pack_folder(root / "base", nup, **values)
            package = core.inspect_nup(nup)["packages"][0]
            self.assertEqual(package["title_id"], "0123456789ABCDEF")
            self.assertEqual(package["version"], 9)
            self.assertEqual(package["metadata"]["title"], "Reviewed")
            self.assertEqual(package["metadata"]["publisher"], "Publisher")
            self.assertEqual(package["metadata"]["version"], "1.2")

    def test_pack_and_edit_apply_reviewed_values_without_losing_other_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in (root / "dump" / "base" / "romfs",
                         root / "dump" / "updates" / "7" / "romfs"):
                path.mkdir(parents=True)
                (path / "file.bin").write_bytes(b"data")
            icon = root / "original.png"
            icon.write_bytes(png_bytes())
            nup = root / "test.nup"
            core.pack_folder(root / "dump", nup, title_id="0123456789ABCDEF",
                             title="Old", publisher="Original", icon_path=str(icon))
            with patch.object(ns2db, "search", return_value=[]):
                self.window.edit_source.edit.setText(str(nup))
                self.window._load_edit_fields()
                wait_for(lambda: not self.window.edit_lookup.workers)
                self.assertEqual(len(self.window._edit_packages), 2)
                self.window.edit_title.setText("Base draft")
                self.window.edit_package.setCurrentIndex(1)
                wait_for(lambda: not self.window.edit_lookup.workers)
                self.window._apply_lookup_edit_values({"publisher": "Reviewed", "update_version": "9"})
                self.window.edit_package.setCurrentIndex(0)
                wait_for(lambda: not self.window.edit_lookup.workers)
                self.assertEqual(self.window.edit_title.text(), "Base draft")
                self.window.edit_package.setCurrentIndex(1)
                wait_for(lambda: not self.window.edit_lookup.workers)
                self.assertEqual(self.window.edit_publisher.text(), "Reviewed")
                values = self.window._current_edit_values()
                core.edit_metadata(nup, title_id=values["title_id"], title=values["title"],
                                   publisher=values["publisher"],
                                   display_version=values["display_version"],
                                   update_version=values["update_version"],
                                   icon_path=values["icon_path"], package_index=1)
            packages = core.inspect_nup(nup)["packages"]
            self.assertEqual(packages[0]["metadata"]["publisher"], "Original")
            self.assertEqual(packages[1]["metadata"]["publisher"], "Reviewed")
            self.assertEqual(packages[1]["version"], 9)
            self.assertEqual(packages[1]["icon_data"], png_bytes())


if __name__ == "__main__":
    unittest.main()
