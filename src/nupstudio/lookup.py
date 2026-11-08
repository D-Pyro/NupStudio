from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import ns2db


class LookupWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable, arguments: tuple):
        super().__init__()
        self.operation = operation
        self.arguments = arguments

    def run(self) -> None:
        try:
            result = self.operation(*self.arguments)
        except ns2db.LookupError as exc:
            self.failed.emit(str(exc))
        else:
            self.result.emit(result)


FIELD_LABELS = {
    "title_id": "Title ID",
    "title": "Title",
    "publisher": "Publisher",
    "base_version": "Base version",
    "update_version": "Update version",
    "display_version": "Display version",
}


def proposed_fields(release: dict, allowed: set[str]) -> tuple[dict[str, str], list[str]]:
    fields = {}
    notes = []
    for key in FIELD_LABELS:
        value = release.get("titleid" if key == "title_id" else key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if key not in allowed:
            notes.append(f"{FIELD_LABELS[key]} cannot be applied to this package.")
        elif key == "title_id" and (
            not re.fullmatch(r"[0-9a-fA-F]{16}", value) or int(value, 16) == 0
        ):
            notes.append("The title ID from NS2DB is not valid.")
        elif key in ("base_version", "update_version") and not ns2db.valid_version(value):
            notes.append(
                f"{FIELD_LABELS[key]} cannot be used: {value}. "
                "It must be a whole number between 0 and 4,294,967,295."
            )
        else:
            fields[key] = value.upper() if key == "title_id" else value
    return fields, notes


class ReviewDialog(QDialog):
    def __init__(self, current: dict[str, str], proposed: dict[str, str],
                 notes: list[str], cover_url: str, scope: str, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Review details from NS2DB")
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            f"{scope}\n\n"
            "Compare your current values with the details from NS2DB.\n"
            "Check the details you want to use; unchecked values will stay as they are."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        contents = QWidget()
        form = QFormLayout(contents)
        self.checks: dict[str, QCheckBox] = {}
        for key, value in proposed.items():
            check = QCheckBox(f"Use NS2DB value: {value}")
            check.setChecked(not current.get(key))
            check.setToolTip(f"Current value: {current.get(key) or 'Not set'}")
            form.addRow(
                f"{FIELD_LABELS[key]}\nCurrent value: {current.get(key) or 'Not set'}", check
            )
            self.checks[key] = check
        if cover_url:
            cover = QCheckBox("Download the cover and use it as the package icon")
            cover.setToolTip(cover_url)
            form.addRow("Cover artwork", cover)
            self.checks["icon_path"] = cover
        if notes:
            note = QLabel("\n".join(notes))
            note.setWordWrap(True)
            form.addRow("Details that can't be used", note)
        if not self.checks:
            form.addRow(QLabel("There are no details you can use from this release."))
        scroll.setWidget(contents)
        layout.addWidget(scroll)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply selected")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self) -> set[str]:
        return {key for key, check in self.checks.items() if check.isChecked()}


class LookupPanel(QWidget):
    def __init__(
        self,
        current: Callable[[], dict[str, str]],
        allowed: Callable[[], set[str]],
        apply: Callable[[dict[str, str]], None],
        scope: Callable[[], str],
        temp_dir: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.current = current
        self.allowed = allowed
        self.apply_values = apply
        self.scope = scope
        self.temp_dir = temp_dir
        self.generation = 0
        self.workers: set[LookupWorker] = set()
        self.releases: list[dict] = []
        self.mode = QComboBox()
        self.mode.addItem("Title ID", "titleid")
        self.mode.addItem("Game name", "q")
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search NS2DB")
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.search)
        self.query.returnPressed.connect(self.search)
        self.mode.currentIndexChanged.connect(self._query_changed)
        self.query.textEdited.connect(self._query_changed)
        self.results = QComboBox()
        self.results.currentIndexChanged.connect(self._update_review_button)
        self.review_button = QPushButton("Review selected release")
        self.review_button.clicked.connect(self.review)
        self.review_button.setEnabled(False)
        self.status = QLabel("Search NS2DB by title ID or game name.")
        self.status.setWordWrap(True)
        search_row = QHBoxLayout()
        search_row.addWidget(self.mode)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_button)
        result_row = QHBoxLayout()
        result_row.addWidget(self.results, 1)
        result_row.addWidget(self.review_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(search_row)
        layout.addLayout(result_row)
        layout.addWidget(self.status)

    def _query_changed(self, *_args) -> None:
        self.invalidate()
        self.status.setText("Search NS2DB by title ID or game name.")

    def invalidate(self) -> None:
        self.generation += 1
        self.releases = []
        self.results.clear()
        self.review_button.setEnabled(False)

    def reset(self, title_id: str = "") -> None:
        self.invalidate()
        self.mode.setCurrentIndex(0)
        self.query.setText(title_id if title_id != "0000000000000000" else "")
        self.status.setText("Search NS2DB by title ID or game name.")
        if re.fullmatch(r"[0-9a-fA-F]{16}", title_id) and int(title_id, 16):
            self.search()

    def _start(self, operation: Callable, arguments: tuple, success: Callable,
               generation: int) -> None:
        worker = LookupWorker(operation, arguments)
        self.workers.add(worker)
        worker.result.connect(lambda value: success(value) if generation == self.generation else None)
        worker.failed.connect(
            lambda message: self.status.setText(f"NS2DB lookup failed: {message}")
            if generation == self.generation else None
        )
        worker.finished.connect(lambda: self.workers.discard(worker))
        worker.start()

    @Slot()
    def search(self) -> None:
        self.invalidate()
        generation = self.generation
        self.status.setText("Searching NS2DB…")
        self._start(ns2db.search, (self.mode.currentData(), self.query.text()),
                    self._show_results, generation)

    def _show_results(self, releases: list[dict]) -> None:
        self.releases = releases
        self.results.clear()
        if not releases:
            self.status.setText("No matching NS2DB releases.")
            return
        if len(releases) > 1:
            self.results.addItem("Select a release…", None)
        for index, row in enumerate(releases):
            label = " | ".join(
                str(row.get(key) or "unknown") for key in
                ("title", "titleid", "region", "publisher", "serial")
            )
            self.results.addItem(label, index)
        self.status.setText(f"{len(releases)} release(s) found. Select one to review.")
        self._update_review_button()

    def _update_review_button(self, *_args) -> None:
        self.review_button.setEnabled(self.results.currentData() is not None)

    @Slot()
    def review(self) -> None:
        index = self.results.currentData()
        if not isinstance(index, int) or not 0 <= index < len(self.releases):
            return
        release = self.releases[index]
        current = self.current()
        generation = self.generation
        proposed, notes = proposed_fields(release, self.allowed())
        cover_url = release.get("cover_url") or ""
        dialog = ReviewDialog(current, proposed, notes, cover_url, self.scope(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if generation != self.generation or self.current() != current:
            self.status.setText("The package or metadata changed; review the release again.")
            return
        selected = dialog.selected()
        values = {key: value for key, value in proposed.items() if key in selected}
        if "icon_path" in selected:
            self.status.setText("Downloading cover artwork…")
            self._start(
                ns2db.download_cover, (cover_url,),
                lambda data: self._apply_cover(data, values, current, generation), generation,
            )
        else:
            self.apply_values(values)
            self.status.setText("Selected metadata applied to the form.")

    def _apply_cover(self, data: bytes, values: dict[str, str], current: dict[str, str],
                     generation: int) -> None:
        if generation != self.generation or self.current() != current:
            self.status.setText("The package or metadata changed; review the release again.")
            return
        image = QImage.fromData(data)
        if image.isNull():
            self.status.setText("Cover download failed: the response is not a supported image.")
            return
        try:
            with tempfile.NamedTemporaryFile(dir=self.temp_dir, suffix=".image", delete=False) as file:
                file.write(data)
                path = Path(file.name)
        except OSError as exc:
            self.status.setText(f"Could not save downloaded cover: {exc}")
            return
        values["icon_path"] = str(path)
        self.apply_values(values)
        self.status.setText("Selected metadata and cover applied to the form.")
