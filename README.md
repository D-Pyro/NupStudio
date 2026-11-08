# NupStudio

NupStudio is a cross-platform Python/PySide6 app for creating, inspecting, editing, and extracting NUP v1 containers.

## Features

- Pack a package folder or recursively discovered `base/`, `updates/<version>/`, and `dlc/<title-id>/<index>/` packages into one NUP.
- Inspect package metadata and compression details; edit a selected package while preserving the others.
- Extract package contents to their corresponding folder structure and verify file hashes.
- Prefill title ID from `exefs/main.npdm`, title, publisher, and display version from `romfs/control.nacp`, and base/update versions from folder names.
- Run folder inspection asynchronously so large dumps do not block the interface.

Title, publisher, and display version cannot be detected without `control.nacp`; enter them manually when needed.

## Install and run

Requires Python 3.10 or newer. From the project root, create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

Install and launch:

```sh
python -m pip install -e .
nupstudio
```

The app uses PySide6 and `zstandard`.

## Build a standalone app

Build on each target operating system; PyInstaller outputs are platform-specific.

```sh
python -m pip install -e ".[build]"
python -m PyInstaller --clean --noconfirm --distpath build/dist --workpath build nupstudio.spec
```

The executable or app bundle is written to `build/dist/`. The build omits unused Qt PDF, QML/Quick, Virtual Keyboard, and OpenGL components; PNG, JPEG, and WebP icon previews remain supported. GitHub Actions builds Windows, Linux, and macOS artifacts. Push a version tag such as `v0.2.0` to create a GitHub Release with downloadable packages. macOS signing and notarization are not configured.

## Notes

- A container supports at most one base package. Update-only containers are marked standalone.
- Zstandard compression levels can be selected when packing. Files 256 MiB or larger are stored raw because NUP v1 has no chunked Zstandard codec. Metadata edits preserve existing file extents.
- Blank text fields clear their metadata. Leave the icon path blank to preserve the existing icon.
- Wait for active background tasks to finish before closing the app.
- The reader accepts zero alignment padding around the TOC string pool, metadata after file extents, and optional package-root names, while validating metadata bounds and overlaps.

References: [NUP format specification](https://github.com/a55uka/nup/blob/meow/SPEC.md) · [NUPPacker folder-layout reference](https://github.com/PastaLaPate/NUPPacker)
