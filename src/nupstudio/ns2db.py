from __future__ import annotations

import json
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .core import NupError, _parse_uint

API_URL = "https://ns2db.ddnsfree.com/api.php"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
TIMEOUT = 12


class LookupError(Exception):
    """Raised when an NS2DB lookup cannot be completed."""


def _https_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError as exc:
        raise LookupError("The URL is malformed.") from exc
    if (parts.scheme.lower() != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        raise LookupError("Only HTTPS URLs without credentials are supported.")
    return url


def _resolve_url(base: str, url: str) -> str:
    try:
        return _https_url(urljoin(base, url))
    except ValueError as exc:
        raise LookupError("The URL is malformed.") from exc


class _SafeRedirect(HTTPRedirectHandler):
    def __init__(self, origin: str | None):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = _resolve_url(req.full_url, newurl)
        if self.origin is not None and urlsplit(target).netloc.lower() != self.origin:
            raise LookupError("The API redirected to a different host.")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _download(url: str, limit: int, *, same_host: bool) -> bytes:
    url = _https_url(url)
    origin = urlsplit(url).netloc.lower() if same_host else None
    opener = build_opener(_SafeRedirect(origin))
    request = Request(url, headers={"User-Agent": f"NupStudio/{__version__} NS2DB client"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            content = response.read(limit + 1)
    except HTTPError as exc:
        raise LookupError(f"Server returned HTTP {exc.code}.") from exc
    except (URLError, OSError, HTTPException, UnicodeError, ValueError) as exc:
        raise LookupError(f"Network request failed: {exc}") from exc
    if len(content) > limit:
        raise LookupError("The server response exceeds the supported size.")
    return content


def search(mode: str, query: str) -> list[dict]:
    query = query.strip()
    if mode == "titleid":
        if not re.fullmatch(r"[0-9a-fA-F]{16}", query) or int(query, 16) == 0:
            raise LookupError("Enter a nonzero, 16-digit hexadecimal title ID.")
        query = query.upper()
    elif mode == "q":
        if not query or len(query.encode("utf-8")) > 1020:
            raise LookupError("Enter a game name of at most 1,020 UTF-8 bytes.")
    else:
        raise LookupError("Choose a valid search mode.")
    raw = _download(f"{API_URL}?{urlencode({mode: query})}", MAX_JSON_BYTES, same_host=True)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LookupError("The API returned invalid JSON.") from exc
    if (not isinstance(result, dict) or result.get("status") != "success"
            or not isinstance(result.get("data"), list)
            or not isinstance(result.get("count"), int)
            or isinstance(result.get("count"), bool)
            or result["count"] != len(result["data"])):
        raise LookupError("The API returned an invalid response.")
    text_fields = (
        "title", "titleid", "publisher", "region", "serial", "base_version",
        "update_version", "display_version", "cover_url",
    )
    for row in result["data"]:
        if not isinstance(row, dict) or any(
            row.get(key) is not None and not isinstance(row[key], str)
            for key in text_fields
        ):
            raise LookupError("The API returned an invalid release.")
    return result["data"]


def download_cover(url: str) -> bytes:
    return _download(_resolve_url(API_URL, url), MAX_IMAGE_BYTES, same_host=False)


def valid_version(value: str) -> bool:
    try:
        _parse_uint(value, base=0)
    except NupError:
        return False
    return True
