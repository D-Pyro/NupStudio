from __future__ import annotations

import sys
import struct
import tempfile
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import zstandard

from .core import (
    NupError,
    edit_metadata,
    extract_nup,
    inspect_folder,
    inspect_nup,
    pack_folder,
)
from .lookup import LookupPanel


class OperationWorker(QThread):
    progress = Signal(int, str)
    completed = Signal()
    failed = Signal(str)

    def __init__(self, operation: Callable, arguments: dict):
        super().__init__()
        self.operation = operation
        self.arguments = arguments

    def run(self) -> None:
        try:
            self.operation(
                **self.arguments,
                progress=lambda percent, message: self.progress.emit(percent, message),
            )
        except (NupError, OSError, ValueError, struct.error, zstandard.ZstdError) as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit()


class FolderInspectionWorker(QThread):
    result = Signal(str, object)
    failed = Signal(str, str)
    active_workers: set[FolderInspectionWorker] = set()

    def __init__(self, source: str):
        super().__init__()
        self.source = source
        self.active_workers.add(self)
        self.finished.connect(lambda worker=self: self.active_workers.discard(worker))

    def run(self) -> None:
        try:
            result = inspect_folder(self.source)
        except (NupError, OSError, ValueError) as exc:
            self.failed.emit(self.source, str(exc))
        else:
            self.result.emit(self.source, result)


class PathRow(QWidget):
    def __init__(self, caption: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.edit = QLineEdit()
        self.button = QPushButton(caption)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)


class IconPreview(QLabel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(104, 104)
        self.setMaximumSize(144, 144)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setText("No icon")
        self._source = QPixmap()

    def set_image_data(self, data: bytes) -> None:
        image = QPixmap()
        if data and image.loadFromData(data):
            self._source = image
            self._refresh()
        else:
            self._source = QPixmap()
            self.setText("Preview unavailable" if data else "No icon")

    def _refresh(self) -> None:
        if self._source.isNull():
            return
        self.setPixmap(self._source.scaled(
            self.contentsRect().size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NupStudio")
        self.resize(760, 660)
        self.setMinimumSize(620, 440)
        self.worker: OperationWorker | None = None
        self._embedded_icon = b""
        self._pack_inspect_workers: set[FolderInspectionWorker] = set()
        self._lookup_temp = tempfile.TemporaryDirectory(prefix="nupstudio-ns2db-")
        self._pack_lookup_fields = {"title", "publisher", "display_version"}
        self._pack_lookup_icon = ""

        self.tabs = QTabWidget()
        self.pack_page, self.edit_page, self.extract_page = (
            self._make_pack_page(),
            self._make_edit_page(),
            self._make_extract_page(),
        )
        self.tabs.addTab(self._scroll_page(self.pack_page), "Pack")
        self.tabs.addTab(self._scroll_page(self.edit_page), "Metadata")
        self.tabs.addTab(self._scroll_page(self.extract_page), "Extract")
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("Ready")

    @staticmethod
    def _scroll_page(page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _configure_form(layout: QFormLayout) -> None:
        layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

    @staticmethod
    def _field(layout: QFormLayout, label: str, widget: QWidget) -> None:
        layout.addRow(label, widget)

    def _make_pack_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.pack_instruction = QLabel(
            "Select a package folder, or a dump root containing base, updates, or DLC packages."
        )
        self.pack_instruction.setWordWrap(True)
        layout.addWidget(self.pack_instruction)
        form = QFormLayout()
        self._configure_form(form)
        self.pack_source = PathRow("Browse…")
        self.pack_source.button.clicked.connect(self._choose_pack_source)
        self.pack_source.edit.editingFinished.connect(self._load_pack_fields)
        self._autofilled_pack_fields: dict[str, str] = {}
        self._last_pack_source = ""
        self.pack_output = PathRow("Save as…")
        self.pack_output.button.clicked.connect(
            lambda: self._choose_save_file(self.pack_output.edit)
        )
        self._field(form, "Input folder", self.pack_source)
        self._field(form, "Output NUP", self.pack_output)

        self.pack_title_id = QLineEdit()
        self.pack_title = QLineEdit()
        self.pack_publisher = QLineEdit()
        self.pack_base_version = QLineEdit()
        self.pack_update_version = QLineEdit()
        self.pack_display_version = QLineEdit()
        self.pack_icon = PathRow("Browse…")
        self.pack_icon.button.clicked.connect(lambda: self._choose_image(self.pack_icon.edit))
        self.pack_icon.edit.textChanged.connect(self._preview_pack_icon)
        self.pack_icon_preview = IconPreview()
        self.pack_compression = QComboBox()
        self.pack_compression.addItem("Off (raw)", 0)
        for level in (1, 3, 5, 10, 19):
            self.pack_compression.addItem(f"Zstandard {level}", level)
        self.pack_compression.setToolTip(
            "Files 256 MiB or larger are stored raw; NUP v1 does not support chunked Zstandard."
        )
        self._field(form, "Title ID (hex)", self.pack_title_id)
        self._field(form, "Title", self.pack_title)
        self._field(form, "Publisher", self.pack_publisher)
        self._field(form, "Base version", self.pack_base_version)
        self._field(form, "Update version", self.pack_update_version)
        self._field(form, "Display version", self.pack_display_version)
        self._field(form, "Icon (optional)", self.pack_icon)
        self._field(form, "Compression", self.pack_compression)
        layout.addLayout(form)
        self.pack_lookup = LookupPanel(
            self._current_pack_values, lambda: self._pack_lookup_fields,
            self._apply_lookup_pack_values,
            lambda: ("These details can affect more than one package in the selected folder.\n"
                     "Version changes depend on the packages being packed."),
            self._lookup_temp.name, self,
        )
        self.pack_source.edit.textChanged.connect(self._invalidate_pack_lookup)
        layout.addWidget(self.pack_lookup)
        layout.addWidget(self.pack_icon_preview, 0, Qt.AlignmentFlag.AlignHCenter)
        self.pack_action = QPushButton("Create NUP")
        self.pack_action.clicked.connect(self._start_pack)
        layout.addWidget(self.pack_action)
        layout.addStretch(1)
        return page

    def _make_edit_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._configure_form(form)
        self.edit_source = PathRow("Open…")
        self.edit_source.button.clicked.connect(self._choose_edit_source)
        self.edit_source.edit.editingFinished.connect(self._load_edit_fields)
        self.edit_output = PathRow("Save as…")
        self.edit_output.button.clicked.connect(lambda: self._choose_save_file(self.edit_output.edit))
        self.edit_package = QComboBox()
        self.edit_package.currentIndexChanged.connect(self._load_selected_edit_package)
        self.edit_package_label = QLabel("Package")
        self._edit_packages: list[dict] = []
        self._edit_package_drafts: dict[int, dict[str, str]] = {}
        self._active_edit_package: int | None = None
        self.edit_title_id = QLineEdit()
        self.edit_title = QLineEdit()
        self.edit_publisher = QLineEdit()
        self.edit_base_version = QLineEdit()
        self.edit_update_version = QLineEdit()
        self.edit_display_version = QLineEdit()
        self.edit_icon = PathRow("Browse…")
        self.edit_icon.button.clicked.connect(lambda: self._choose_image(self.edit_icon.edit))
        self.edit_icon.edit.textChanged.connect(self._preview_edit_icon)
        self.edit_icon_preview = IconPreview()
        self.edit_icon_preview.setFixedSize(104, 104)
        edit_icon_row = QWidget()
        edit_icon_layout = QHBoxLayout(edit_icon_row)
        edit_icon_layout.setContentsMargins(0, 0, 0, 0)
        edit_icon_layout.addWidget(self.edit_icon, 1)
        edit_icon_layout.addWidget(self.edit_icon_preview)
        self._field(form, "NUP file", self.edit_source)
        self._field(form, "Output file (optional)", self.edit_output)
        form.addRow(self.edit_package_label, self.edit_package)
        self._field(form, "Title ID (hex)", self.edit_title_id)
        self._field(form, "Title", self.edit_title)
        self._field(form, "Publisher", self.edit_publisher)
        self._field(form, "Base version", self.edit_base_version)
        self._field(form, "Update version", self.edit_update_version)
        self._field(form, "Display version", self.edit_display_version)
        self._field(form, "Replace icon (blank keeps current)", edit_icon_row)
        layout.addLayout(form)
        self.edit_lookup = LookupPanel(
            self._current_edit_values, self._allowed_edit_lookup_fields,
            self._apply_lookup_edit_values,
            lambda: "These details will only affect the selected package.",
            self._lookup_temp.name, self,
        )
        self.edit_source.edit.textChanged.connect(self._invalidate_edit_lookup)
        layout.addWidget(self.edit_lookup)
        self.edit_summary = QLabel("Select a NUP file to view package and compression details.")
        self.edit_summary.setWordWrap(True)
        layout.addWidget(self.edit_summary)
        self.edit_action = QPushButton("Save metadata changes")
        self.edit_action.clicked.connect(self._start_edit)
        layout.addWidget(self.edit_action)
        layout.addStretch(1)
        return page

    def _make_extract_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.extract_summary = QLabel("Select a NUP file to view package and compression details.")
        self.extract_summary.setWordWrap(True)
        layout.addWidget(self.extract_summary)
        form = QFormLayout()
        self._configure_form(form)
        self.extract_source = PathRow("Open…")
        self.extract_source.button.clicked.connect(self._choose_extract_source)
        self.extract_source.edit.editingFinished.connect(
            lambda: self._load_extract_summary(self.extract_source.edit.text())
        )
        self.extract_output = PathRow("Browse…")
        self.extract_output.button.clicked.connect(
            lambda: self._choose_folder(self.extract_output.edit, "Select extraction destination")
        )
        self._field(form, "NUP file", self.extract_source)
        self._field(form, "Output folder", self.extract_output)
        layout.addLayout(form)
        self.extract_instruction = QLabel(
            "All packages are extracted, preserving base/, updates/, and dlc/ folder structure."
        )
        self.extract_instruction.setWordWrap(True)
        layout.addWidget(self.extract_instruction)
        self.extract_action = QPushButton("Extract files")
        self.extract_action.clicked.connect(self._start_extract)
        layout.addWidget(self.extract_action)
        layout.addStretch(1)
        return page

    @staticmethod
    def _choose_folder(target: QLineEdit, caption: str) -> None:
        folder = QFileDialog.getExistingDirectory(None, caption, target.text())
        if folder:
            target.setText(folder)

    @staticmethod
    def _choose_nup(target: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            None, "Select NUP file", target.text(), "NUP containers (*.nup);;All files (*)"
        )
        if path:
            target.setText(path)

    @staticmethod
    def _choose_save_file(target: QLineEdit) -> None:
        path, _ = QFileDialog.getSaveFileName(
            None, "Save NUP file", target.text() or "package.nup",
            "NUP containers (*.nup);;All files (*)",
        )
        if path:
            target.setText(path)

    @staticmethod
    def _choose_image(target: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            None, "Select icon", target.text(),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*)",
        )
        if path:
            target.setText(path)

    def _choose_edit_source(self) -> None:
        self._choose_nup(self.edit_source.edit)
        if self.edit_source.edit.text():
            self._load_edit_fields()

    def _choose_extract_source(self) -> None:
        self._choose_nup(self.extract_source.edit)
        if self.extract_source.edit.text():
            self._load_extract_summary(self.extract_source.edit.text())

    def _preview_pack_icon(self, path: str) -> None:
        data = self._image_as_png(path)
        self.pack_icon_preview.set_image_data(data if data else (b"invalid" if path else b""))

    def _preview_edit_icon(self, path: str) -> None:
        if path.strip():
            data = self._image_as_png(path)
            self.edit_icon_preview.set_image_data(data if data else b"invalid")
            return
        self.edit_icon_preview.set_image_data(self._embedded_icon)

    def _load_extract_summary(self, path: str) -> None:
        if not path.strip():
            self.extract_summary.setText("Select a NUP file to view package and compression details.")
            return
        try:
            details = inspect_nup(path)
        except (NupError, OSError) as exc:
            self.extract_summary.setText(f"Package details unavailable: {exc}")
            return
        self.extract_summary.setText(self._format_package_summary(details["packages"]))

    @staticmethod
    def _format_size(value: int) -> str:
        size = float(value)
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        unit = 0
        while size >= 1024 and unit < len(units) - 1:
            size /= 1024
            unit += 1
        return f"{size:.1f} {units[unit]}" if unit else f"{value} B"

    @classmethod
    def _format_package_summary(cls, packages: list[dict]) -> str:
        lines = []
        for package in packages:
            names = ("Base", "Update", "DLC")
            kind = package["kind"]
            title = package["metadata"].get("title", "")
            label = names[kind] if kind < len(names) else f"Package {kind}"
            if kind == 1:
                label += f" {package['version']}"
            elif kind == 2:
                label += f" {package['title_id']}"
            if title:
                label += f": {title}"
            lines.append(
                f"{label} | {package['zstd_files']} Zstd + {package['raw_files']} raw | "
                f"{cls._format_size(package['stored_bytes'])} / "
                f"{cls._format_size(package['original_bytes'])} stored/original"
            )
        return "\n".join(lines)

    @staticmethod
    def _image_as_png(path: str) -> bytes:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return b""
        from PySide6.QtCore import QBuffer, QIODevice

        output = QBuffer()
        output.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(output, "PNG")
        data = bytes(output.data().data())
        output.close()
        return data

    def _choose_pack_source(self) -> None:
        self._choose_folder(self.pack_source.edit, "Select a package folder or dump root")
        self._load_pack_fields()

    def _invalidate_pack_lookup(self, _text: str) -> None:
        self.pack_lookup.reset()
        self._pack_lookup_fields = {"title", "publisher", "display_version"}

    def _invalidate_edit_lookup(self, _text: str) -> None:
        self.edit_lookup.reset()

    def _load_pack_fields(self) -> None:
        source = self.pack_source.edit.text().strip()
        if not source or source == self._last_pack_source:
            return
        self._last_pack_source = source
        fields = {
            "title_id": self.pack_title_id,
            "title": self.pack_title,
            "publisher": self.pack_publisher,
            "base_version": self.pack_base_version,
            "update_version": self.pack_update_version,
            "display_version": self.pack_display_version,
        }
        for key, previous in self._autofilled_pack_fields.items():
            widget = fields[key]
            if widget.text() == previous:
                widget.clear()
        self._autofilled_pack_fields.clear()
        if self._pack_lookup_icon and self.pack_icon.edit.text() == self._pack_lookup_icon:
            self.pack_icon.edit.clear()
        self._pack_lookup_icon = ""
        self.statusBar().showMessage("Scanning folder for package metadata…")
        worker = FolderInspectionWorker(source)
        self._pack_inspect_workers.add(worker)
        worker.result.connect(self._apply_pack_fields)
        worker.failed.connect(self._pack_inspection_failed)
        worker.finished.connect(lambda task=worker: self._pack_inspect_workers.discard(task))
        worker.start()

    @Slot(str, object)
    def _apply_pack_fields(self, source: str, detected: dict[str, str]) -> None:
        if source != self.pack_source.edit.text().strip() or source != self._last_pack_source:
            return
        fields = {
            "title_id": self.pack_title_id,
            "title": self.pack_title,
            "publisher": self.pack_publisher,
            "base_version": self.pack_base_version,
            "update_version": self.pack_update_version,
            "display_version": self.pack_display_version,
        }
        for key, widget in fields.items():
            value = detected.get(key, "")
            if value and not widget.text():
                widget.setText(value)
                self._autofilled_pack_fields[key] = value
        icon_file = detected.get("icon_file", "")
        if icon_file and not self.pack_icon.edit.text().strip():
            self.pack_icon.edit.setText(icon_file)
            self._pack_lookup_icon = icon_file
        kinds = detected.get("package_kinds", "").split(",")
        self._pack_lookup_fields = {"title", "publisher", "display_version"}
        if any(kind in ("0", "1") for kind in kinds):
            self._pack_lookup_fields.add("title_id")
        if "0" in kinds:
            self._pack_lookup_fields.add("base_version")
        if detected.get("package_count") == "1" and kinds[0] in ("1", "2"):
            self._pack_lookup_fields.add("update_version")
        self.pack_lookup.reset(self.pack_title_id.text().strip())
        if not any(detected.get(key) for key in ("title", "publisher", "display_version")):
            self.statusBar().showMessage(
                "No control.nacp metadata found; enter title, publisher, and display version manually."
            )
        else:
            self.statusBar().showMessage("Folder metadata loaded.")

    @Slot(str, str)
    def _pack_inspection_failed(self, source: str, message: str) -> None:
        if source == self.pack_source.edit.text().strip() and source == self._last_pack_source:
            self.statusBar().showMessage(f"Could not detect package metadata: {message}")

    def _load_edit_fields(self) -> None:
        self.edit_lookup.reset()
        self._embedded_icon = b""
        self._edit_packages = []
        self._edit_package_drafts.clear()
        self._active_edit_package = None
        self.edit_package.blockSignals(True)
        self.edit_package.clear()
        self.edit_package_label.setVisible(False)
        self.edit_package.setVisible(False)
        self.edit_icon.edit.clear()
        self.edit_title_id.clear()
        self.edit_title.clear()
        self.edit_publisher.clear()
        self.edit_base_version.clear()
        self.edit_update_version.clear()
        self.edit_display_version.clear()
        self.edit_icon_preview.set_image_data(b"")
        path = self.edit_source.edit.text().strip()
        if not path:
            self.edit_summary.setText("Select a NUP file to view package and compression details.")
            self.edit_package.blockSignals(False)
            return
        try:
            summary = inspect_nup(path)
        except (NupError, OSError) as exc:
            self.edit_summary.setText(f"Could not read package: {exc}")
            self.edit_package.blockSignals(False)
            return

        self._edit_packages = summary["packages"]
        for package in self._edit_packages:
            self.edit_package.addItem(self._edit_package_label(package))
        multiple_packages = len(self._edit_packages) > 1
        self.edit_package_label.setVisible(multiple_packages)
        self.edit_package.setVisible(multiple_packages)
        self.edit_package.blockSignals(False)
        self.edit_summary.setText(self._format_package_summary(self._edit_packages))
        if self._edit_packages:
            self._load_selected_edit_package(0)

    @staticmethod
    def _edit_package_label(package: dict) -> str:
        names = ("Base", "Update", "DLC")
        kind = package["kind"]
        label = names[kind] if kind < len(names) else f"Package {kind}"
        if kind == 1:
            label += f" {package['version']}"
        elif kind == 2:
            label += f" {package['title_id']}"
        title = package["metadata"].get("title", "")
        return f"{label}: {title}" if title else label

    def _current_edit_values(self) -> dict[str, str]:
        return {
            "title_id": self.edit_title_id.text(),
            "title": self.edit_title.text(),
            "publisher": self.edit_publisher.text(),
            "base_version": self.edit_base_version.text(),
            "update_version": self.edit_update_version.text(),
            "display_version": self.edit_display_version.text(),
            "icon_path": self.edit_icon.edit.text(),
        }

    def _current_pack_values(self) -> dict[str, str]:
        return {
            "title_id": self.pack_title_id.text(),
            "title": self.pack_title.text(),
            "publisher": self.pack_publisher.text(),
            "base_version": self.pack_base_version.text(),
            "update_version": self.pack_update_version.text(),
            "display_version": self.pack_display_version.text(),
            "icon_path": self.pack_icon.edit.text(),
        }

    def _apply_lookup_pack_values(self, values: dict[str, str]) -> None:
        widgets = {
            "title_id": self.pack_title_id, "title": self.pack_title,
            "publisher": self.pack_publisher, "base_version": self.pack_base_version,
            "update_version": self.pack_update_version,
            "display_version": self.pack_display_version, "icon_path": self.pack_icon.edit,
        }
        for key, value in values.items():
            widgets[key].setText(value)
            if key == "icon_path":
                self._pack_lookup_icon = value
            else:
                self._autofilled_pack_fields[key] = value

    def _allowed_edit_lookup_fields(self) -> set[str]:
        fields = {"title", "publisher", "display_version"}
        index = self._active_edit_package
        if index is None or not 0 <= index < len(self._edit_packages):
            return set()
        kind = self._edit_packages[index]["kind"]
        if kind != 2:
            fields.add("title_id")
        fields.add("base_version" if kind == 0 else "update_version")
        return fields

    def _apply_lookup_edit_values(self, values: dict[str, str]) -> None:
        widgets = {
            "title_id": self.edit_title_id, "title": self.edit_title,
            "publisher": self.edit_publisher, "base_version": self.edit_base_version,
            "update_version": self.edit_update_version,
            "display_version": self.edit_display_version, "icon_path": self.edit_icon.edit,
        }
        for key, value in values.items():
            widgets[key].setText(value)

    def _load_selected_edit_package(self, index: int) -> None:
        self.edit_lookup.reset()
        if self._active_edit_package is not None:
            self._edit_package_drafts[self._active_edit_package] = self._current_edit_values()
        if not 0 <= index < len(self._edit_packages):
            self._active_edit_package = None
            return

        self._active_edit_package = index
        package = self._edit_packages[index]
        metadata = package["metadata"]
        values = self._edit_package_drafts.get(index, {})
        self._embedded_icon = package["icon_data"]
        self.edit_title_id.setText(values.get("title_id", package["title_id"]))
        self.edit_title.setText(values.get("title", metadata.get("title", "")))
        self.edit_publisher.setText(values.get("publisher", metadata.get("publisher", "")))
        self.edit_display_version.setText(
            values.get("display_version", metadata.get("version", ""))
        )
        self.edit_base_version.setText(
            values.get("base_version", str(package["version"]) if package["kind"] == 0 else "")
        )
        self.edit_update_version.setText(
            values.get("update_version", str(package["version"]) if package["kind"] == 1 else "")
        )
        self.edit_icon.edit.setText(values.get("icon_path", ""))
        self._preview_edit_icon(self.edit_icon.edit.text())
        self.edit_lookup.reset(self.edit_title_id.text().strip())

    def _start_pack(self) -> None:
        self._run(self.pack_action, pack_folder, {
            "source": self.pack_source.edit.text(),
            "output": self.pack_output.edit.text(),
            "title_id": self.pack_title_id.text().strip(),
            "title": self.pack_title.text(),
            "publisher": self.pack_publisher.text(),
            "base_version": self.pack_base_version.text().strip(),
            "update_version": self.pack_update_version.text().strip(),
            "display_version": self.pack_display_version.text(),
            "icon_path": self.pack_icon.edit.text().strip(),
            "compression_level": self.pack_compression.currentData(),
        }, required_paths=("source", "output"))

    def _start_edit(self) -> None:
        self._run(self.edit_action, edit_metadata, {
            "path": self.edit_source.edit.text(),
            "output": self.edit_output.edit.text() or None,
            "title_id": self.edit_title_id.text().strip(),
            "title": self.edit_title.text(),
            "publisher": self.edit_publisher.text(),
            "base_version": self.edit_base_version.text().strip(),
            "update_version": self.edit_update_version.text().strip(),
            "display_version": self.edit_display_version.text(),
            "icon_path": self.edit_icon.edit.text().strip(),
            "package_index": self.edit_package.currentIndex(),
        }, required_paths=("path",))

    def _start_extract(self) -> None:
        self._run(self.extract_action, extract_nup, {
            "path": self.extract_source.edit.text(),
            "output": self.extract_output.edit.text(),
        }, required_paths=("path", "output"))

    def _run(self, button: QPushButton, operation: Callable, arguments: dict,
             *, required_paths: tuple[str, ...]) -> None:
        if self.worker and self.worker.isRunning():
            return
        if not all(str(arguments.get(key) or "").strip() for key in required_paths):
            QMessageBox.warning(self, "Missing path", "Choose the required input and output paths.")
            return
        button.setEnabled(False)
        self.statusBar().showMessage("Starting operation…")
        worker = OperationWorker(operation, arguments)
        self.worker = worker
        worker.progress.connect(
            lambda percent, message: self.statusBar().showMessage(f"{percent}%  {message}")
        )
        worker.completed.connect(lambda: self._operation_finished(button, None))
        worker.failed.connect(lambda message: self._operation_finished(button, message))
        worker.start()

    def _operation_finished(self, button: QPushButton, error: str | None) -> None:
        button.setEnabled(True)
        if error:
            self.statusBar().showMessage("Operation failed")
            QMessageBox.critical(self, "NUP operation failed", error)
        else:
            self.statusBar().showMessage("Operation complete")
            if button is self.edit_action and self.worker:
                arguments = self.worker.arguments
                saved_path = arguments.get("output") or arguments["path"]
                self.edit_source.edit.setText(saved_path)
                self._load_edit_fields()
            QMessageBox.information(self, "NupStudio", "Operation completed successfully.")

    def closeEvent(self, event) -> None:
        operation_running = self.worker is not None and self.worker.isRunning()
        inspection_running = any(worker.isRunning() for worker in self._pack_inspect_workers)
        lookup_running = any(
            worker.isRunning() for panel in (self.pack_lookup, self.edit_lookup)
            for worker in panel.workers
        )
        if operation_running or inspection_running or lookup_running:
            event.ignore()
            self.statusBar().showMessage(
                "A background task is still running. Wait for it to finish before closing."
            )
            return
        self._lookup_temp.cleanup()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NupStudio")
    window = MainWindow()
    window.show()
    return app.exec()
