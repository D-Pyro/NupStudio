# NupStudio

NupStudio is a cross-platform Python/PySide6 app for creating, inspecting, editing, and extracting NUP v1 containers.

## Features

- Pack a package folder or recursively discovered `base/`, `updates/<version>/`, and `dlc/<title-id>/<index>/` packages into one NUP.
- Inspect package metadata and compression details; edit a selected package while preserving the others.
- Extract package contents to their corresponding folder structure and verify file hashes.
- Prefill title ID from `exefs/main.npdm`, title, publisher, and display version from `romfs/control.nacp`, and base/update versions from folder names.
- Run folder inspection asynchronously so large dumps do not block the interface.
- Search NS2DB by title ID or game name and review matching release metadata before applying it.

Title, publisher, and display version cannot be detected without `control.nacp`; enter them manually when needed.

## NS2DB lookup

The Pack and Metadata tabs automatically search [NS2DB](https://ns2db.ddnsfree.com/) when a loaded folder or selected package has a nonzero, valid title ID. This sends the title ID to NS2DB over HTTPS. You can also search manually by exact title ID or by a substring of the game name. Searches run in the background; when several regional releases match, select the appropriate release.

Choose **Review selected release** to compare available values with the form.
Compare current values with the NS2DB values, check the ones you want, and choose
**Apply selected**. Existing values are unchecked by default; nothing is applied automatically. NS2DB's regional publisher is used when available.
Base and update versions are offered only if they are valid NUP unsigned
32-bit integers and the current package or pack layout accepts that override.
Display version is text; firmware is not a package version. An existing DLC
title ID cannot be changed in the metadata editor. Pack form overrides may
affect multiple packages; metadata editing affects only the selected package.

You may optionally download a release's cover artwork and use it as an icon.
This can contact the image host as well as NS2DB; artwork is not downloaded
unless selected. Downloaded images remain available during the app session and
are removed when it closes. Lookup failures or no matches do not prevent
manual editing, packing, or saving.

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

The app uses PySide6 and `zstandard`. On Ubuntu, install the `libegl1` system
package if `libEGL.so.1` is missing.

## Build a standalone app

Build on each target operating system; PyInstaller outputs are platform-specific.

```sh
python -m pip install -e ".[build]"
python -m PyInstaller --clean --noconfirm --distpath build/dist --workpath build nupstudio.spec
```

The executable or app bundle is written to `build/dist/`. The build omits unused Qt PDF, QML/Quick, Virtual Keyboard, and OpenGL components; PNG, JPEG, and WebP icon previews remain supported. GitHub Actions builds Windows, Linux, and macOS artifacts. Push a version tag such as `v0.5.0` to create a GitHub Release with downloadable packages. macOS signing and notarization are not configured.

## Notes

- A container supports at most one base package. Update-only containers are marked standalone.
- Zstandard compression levels can be selected when packing. Files 256 MiB or larger are stored raw because NUP v1 has no chunked Zstandard codec. Metadata edits preserve file data and reuse storage for identical files.
- Blank text fields clear their metadata. Leave the icon path blank to preserve the existing icon.
- Wait for active background tasks to finish before closing the app.
- The reader accepts zero alignment padding around the TOC string pool, metadata after file extents, and optional package-root names, while validating metadata bounds and overlaps.

References: [NUP format specification](https://github.com/a55uka/nup/blob/meow/SPEC.md) · [NUPPacker folder-layout reference](https://github.com/PastaLaPate/NUPPacker)
