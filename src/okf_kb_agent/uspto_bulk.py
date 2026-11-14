"""USPTO bulk data download and grant-XML parsing.

Two independent building blocks:

``download`` / ``extract_zip``
    Stream any file (typically an ODP dataset product file or a weekly grant
    archive) to disk with size verification, retries and safe ZIP extraction.
    ODP dataset files require the free ODP API key (``X-API-KEY``); bulk data
    pages on data.uspto.gov can be browsed without a key, but the API file
    endpoint enforces authentication in practice (401/403 without a key).

``parse_grant_xml`` / ``iter_grant_documents``
    Parse USPTO ``us-patent-grant`` XML (DTD v4.5 style) into a compact dict:
    publication number, kind, title, abstract, inventors, assignees, claims.
    ``iter_grant_documents`` streams large weekly files document by document.

This module is stdlib-only and does not perform network I/O unless you call
``download``.
"""
from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__

API_ROOT = "https://api.uspto.gov"
FILES_ROOT = API_ROOT + "/api/v1/datasets/products/files"
DEFAULT_TIMEOUT = 180.0
DEFAULT_USER_AGENT = f"okf-kb-agent/{__version__} (research; cache-first)"


class BulkError(Exception):
    """A download, extraction or parse step failed."""


def product_file_url(product: str, *segments: str | int) -> str:
    """Build an ODP dataset product file URL.

    ``product_file_url("PTGRXML-SPLT", 2024, "ipg240604", "18999999.xml")``
    """
    product = str(product).strip("/")
    if not product:
        raise ValueError("product is required")
    tail = [str(part).strip("/") for part in segments if str(part).strip("/")]
    url = f"{FILES_ROOT}/{product}"
    if tail:
        url += "/" + "/".join(tail)
    return url


def _format_error(status: int, url: str, body: bytes) -> BulkError:
    text = body.decode("utf-8", errors="replace")[:300]
    if status in (401, 403):
        return BulkError(
            f"USPTO download HTTP {status}: API key missing, invalid or "
            "deactivated. Get a free key at https://data.uspto.gov/apikey "
            f"and pass api_key=. {text}"
        )
    return BulkError(f"USPTO download HTTP {status} for {url}. {text}")


def search_products(client: Any, q: str = "", *, offset: int = 0,
                    limit: int = 25) -> dict[str, Any]:
    """Search the ODP bulk dataset directory (requires the API key)."""
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    params = {"q": q or None, "offset": offset, "limit": limit}
    return client.get_json("/datasets/products/search", params=params,
                           name=f"products_{offset}_{limit}")


def _safe_relpath(name: str) -> Path:
    member = str(name).replace("\\", "/").strip()
    if not member or member.startswith("/") or re.match(r"^[A-Za-z]:", member):
        raise BulkError(f"unsafe product file name: {name!r}")
    parts = [part for part in member.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise BulkError(f"unsafe product file name: {name!r}")
    return Path(*parts)


def _year_overlaps(entry: dict[str, Any], year: int) -> bool:
    start = str(entry.get("fileDataFromDate") or "")
    end = str(entry.get("fileDataToDate") or "")
    if not start and not end:
        return True  # no date metadata: trust the server-side filter
    start = start[:10] or end[:10]
    end = end[:10] or start
    return start[:4] <= str(year) <= end[:4]


def product_files(client: Any, product: str, *, year: int | None = None,
                  date_from: str | None = None, date_to: str | None = None,
                  limit: int = 50, max_pages: int = 50) -> list[dict[str, Any]]:
    """List files of a bulk product, optionally restricted to a year/range.

    Uses ``GET /datasets/products/{productIdentifier}`` with
    ``includeFiles=true`` and ``fileDataFromDate``/``fileDataToDate``.
    """
    if year is not None:
        date_from, date_to = f"{year}-01-01", f"{year}-12-31"
    entries: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        payload = client.get_json(
            f"/datasets/products/{product}",
            params={"includeFiles": "true",
                    "fileDataFromDate": date_from,
                    "fileDataToDate": date_to,
                    "offset": offset,
                    "limit": limit},
            name=f"product_{product}_{year or date_from or 'all'}_{offset}",
        )
        bags = payload.get("bulkDataProductBag") or []
        if not bags:
            break
        file_bag = bags[0].get("productFileBag") or {}
        page = file_bag.get("fileDataBag") or []
        entries.extend(page)
        total = int(file_bag.get("count") or 0)
        offset += len(page)
        if not page or offset >= total:
            break
    if year is not None:
        entries = [e for e in entries if _year_overlaps(e, year)]
    return entries


def download_year(
    client: Any,
    product: str,
    year: int,
    dest_dir: str | Path,
    *,
    api_key: str | None = None,
    dry_run: bool = False,
    opener: Callable[[urllib.request.Request, float], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    sleep_s: float = 1.0,
    progress: Callable[[int, int | None], None] | None = None,
) -> dict[str, Any]:
    """Download every product file covering ``year`` into ``dest_dir``.

    Files already present with the expected size are skipped, so the command
    is safely re-runnable after an interruption.
    """
    entries = product_files(client, product, year=year)
    root = Path(dest_dir)
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for entry in entries:
        relative = _safe_relpath(entry.get("fileName") or "")
        target = root / relative
        expected = entry.get("fileSize")
        uri = str(entry.get("fileDownloadURI") or
                  product_file_url(product, relative.as_posix()))
        row: dict[str, Any] = {"file": relative.as_posix(), "url": uri}
        if dry_run:
            row["status"] = "planned"
            rows.append(row)
            continue
        try:
            expected_size = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected_size = None
        if (target.exists() and expected_size is not None
                and target.stat().st_size == expected_size):
            row["status"] = "skipped"
            row["bytes"] = expected_size
            rows.append(row)
            continue
        size = download(uri, target, api_key=api_key, opener=opener,
                        sleeper=sleeper, sleep_s=sleep_s, progress=progress)
        row["status"] = "ok"
        row["bytes"] = size
        total_bytes += size
        rows.append(row)
    return {"product": product, "year": year, "dest": str(root),
            "total_files": len(rows), "total_bytes": total_bytes,
            "files": rows}


def download(
    url: str,
    dest: str | Path,
    *,
    api_key: str | None = None,
    opener: Callable[[urllib.request.Request, float], Any] | None = None,
    chunk_size: int = 1 << 20,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    sleep_s: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    user_agent: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> int:
    """Stream ``url`` to ``dest`` and return the number of bytes written.

    The file is written to ``dest.part`` and renamed only after the body
    matches ``Content-Length`` (when the server sends it).
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    opener = opener or (lambda request, to: urllib.request.urlopen(request, timeout=to))
    agent = user_agent or os.environ.get("USPTO_USER_AGENT") or DEFAULT_USER_AGENT

    last: Exception | None = None
    for attempt in range(retries + 1):
        headers = {"User-Agent": agent, "Accept": "*/*"}
        if api_key:
            headers["X-API-KEY"] = api_key
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener(request, timeout) as response:
                expected = None
                try:
                    raw_len = (response.headers or {}).get("Content-Length")
                    expected = int(raw_len) if raw_len is not None else None
                except (TypeError, ValueError):
                    expected = None
                written = 0
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                        if progress is not None:
                            progress(written, expected)
                if expected is not None and written != expected:
                    raise BulkError(
                        f"truncated download: got {written} of {expected} bytes")
            part.replace(target)
            return written
        except BulkError as exc:
            part.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            with closing(exc):
                body = exc.read() or b""
                if (exc.code in (429, 500, 502, 503, 504)
                        and attempt < retries):
                    retry_after = 0.0
                    try:
                        retry_after = float(
                            (exc.headers or {}).get("Retry-After", 0))
                    except (TypeError, ValueError):
                        pass
                    last = exc
                    sleeper(max(retry_after, 2.0 ** attempt * sleep_s))
                    continue
                part.unlink(missing_ok=True)
                raise _format_error(exc.code, url, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries:
                sleeper(2.0 ** attempt * sleep_s)
                continue
            part.unlink(missing_ok=True)
            raise BulkError(f"USPTO download failed for {url}: {exc}") from exc

    part.unlink(missing_ok=True)
    raise BulkError(f"USPTO download failed for {url}: {last}")


def extract_zip(archive: str | Path, dest_dir: str | Path) -> list[Path]:
    """Extract a ZIP archive, rejecting absolute paths, ``..`` and symlinks."""
    archive_path = Path(archive)
    root = Path(dest_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        zf = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BulkError(f"cannot open zip {archive_path}: {exc}") from exc
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            member = info.filename.replace("\\", "/")
            if member.startswith("/") or re.match(r"^[A-Za-z]:", member):
                raise BulkError(f"unsafe absolute path in zip: {member}")
            target = (root / member).resolve()
            if target != root and root not in target.parents:
                raise BulkError(f"unsafe path traversal in zip: {member}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise BulkError(f"symlink entry in zip: {member}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    sink.write(chunk)
            written.append(target)
    return written


# ---------------------------------------------------------------------------
# Grant XML parsing
# ---------------------------------------------------------------------------


def _local(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _clean(text: str) -> str:
    return " ".join(text.split())


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _clean("".join(element.itertext()))


def _first(root: ET.Element, name: str) -> ET.Element | None:
    for element in root.iter():
        if _local(element.tag) == name:
            return element
    return None


def _all(root: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in root.iter() if _local(element.tag) == name]


def _grant_record(root: ET.Element) -> dict[str, Any]:
    number = kind = country = ""
    for doc_id in _all(root, "document-id"):
        fields = {_local(child.tag): _text(child) for child in doc_id}
        if fields.get("doc-number"):
            number = fields.get("doc-number", "")
            kind = fields.get("kind", "")
            country = fields.get("country", "")
            break
    if not number:
        number = str(root.get("id") or "")
        match = re.match(r"([A-Z]{2})([0-9]+)", number)
        if match:
            country, number = match.group(1), match.group(2)
    publication = f"{country}{number}{kind}" if country and number else number

    inventors: list[str] = []
    for inventor in _all(root, "inventor"):
        book = _first(inventor, "addressbook")
        if book is not None:
            last = _text(_first(book, "last-name"))
            first = _text(_first(book, "first-name"))
            name = _clean(f"{first} {last}")
            if name:
                inventors.append(name)
        elif _text(inventor):
            inventors.append(_text(inventor))

    assignees: list[str] = []
    for assignee in _all(root, "assignee"):
        org = _first(assignee, "orgname")
        name = _text(org) if org is not None else _text(assignee)
        if name:
            assignees.append(name)

    claims = [_text(claim) for claim in _all(root, "claim")]
    claims = [claim for claim in claims if claim]

    return {
        "publication_number": publication,
        "country": country,
        "number": number,
        "kind": kind,
        "title": _text(_first(root, "invention-title")),
        "abstract": _text(_first(root, "abstract")),
        "inventors": inventors,
        "assignees": assignees,
        "claims": claims,
        "claim_count": len(claims),
    }


def _load_root(source: str | Path | bytes) -> ET.Element:
    try:
        if isinstance(source, (str, Path)) and not (
                isinstance(source, bytes)):
            return ET.parse(str(source)).getroot()
        if isinstance(source, str):
            return ET.fromstring(source)
        return ET.fromstring(source)
    except (ET.ParseError, OSError) as exc:
        raise BulkError(f"cannot parse grant XML: {exc}") from exc


def parse_grant_xml(source: str | Path | bytes) -> dict[str, Any]:
    """Parse one USPTO ``us-patent-grant`` document (path or bytes)."""
    root = _load_root(source)
    if _local(root.tag) != "us-patent-grant":
        raise BulkError(
            f"not a us-patent-grant document (root={_local(root.tag)})")
    return _grant_record(root)


def iter_grant_documents(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream ``us-patent-grant`` documents from a (possibly large) XML file."""
    found = 0
    try:
        for event, element in ET.iterparse(str(path), events=("end",)):
            if _local(element.tag) != "us-patent-grant":
                continue
            record = _grant_record(element)
            element.clear()
            found += 1
            yield record
    except ET.ParseError as exc:
        raise BulkError(f"cannot parse grant XML {path}: {exc}") from exc
    if not found:
        raise BulkError(f"no us-patent-grant elements found in {path}")


def iter_grant_documents_zip(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream grant documents from every XML member of a ZIP archive.

    This works without an API key, so weekly archives downloaded manually
    through the ODP website can be analysed directly.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BulkError(f"cannot open zip {path}: {exc}") from exc
    with archive:
        members = [info for info in archive.infolist()
                   if not info.is_dir()
                   and info.filename.lower().endswith(".xml")]
        if not members:
            raise BulkError(f"no XML members in {path}")
        for info in members:
            _safe_relpath(info.filename)  # reject absolute/.. names
            found = 0
            try:
                with archive.open(info) as handle:
                    for _event, element in ET.iterparse(handle,
                                                       events=("end",)):
                        if _local(element.tag) != "us-patent-grant":
                            continue
                        record = _grant_record(element)
                        element.clear()
                        found += 1
                        yield record
            except ET.ParseError as exc:
                raise BulkError(
                    f"cannot parse {info.filename} in {path}: {exc}") from exc
            if not found:
                raise BulkError(
                    f"no us-patent-grant elements in {info.filename}")
