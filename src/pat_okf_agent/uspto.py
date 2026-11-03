"""Shared access layer for USPTO patent data (Open Data Portal API).

``USPTO Open Data Portal (ODP)``
    Free REST API for US patent applications and related data (Patent File
    Wrapper): search, bibliographic metadata, continuity, transactions,
    documents, assignments, attorney, foreign priority, term adjustment and
    status codes. Authentication is a free API key sent as ``X-API-KEY``
    (create one at https://data.uspto.gov/apikey with a USPTO.gov account).

The key is read from ``USPTO_API_KEY`` or ``.secrets/uspto_credentials.json``
and never written to tracked files. Without a key the API answers HTTP 401.

PatentsView (``search.patentsview.org``) was retired in 2026; its data was
moved to ODP, so this module deliberately targets ODP only.

Fair-use defaults: cache-first, >= 1 s between requests (shared across
processes), HTTP 429/5xx backoff honouring ``Retry-After``, and a conservative
result window of 25 records per request.

Library use::

    from pat_okf_agent.uspto import UsptoClient

    client = UsptoClient()
    res = client.search("applicationMetaData.inventionTitle:graphene", limit=10)
    print(res["count"])

Command line::

    python -m pat_okf_agent.uspto check
    python -m pat_okf_agent.uspto search 'graphene' --limit 10
    python -m pat_okf_agent.uspto app 18999999
    python -m pat_okf_agent.uspto continuity 18999999
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__
from .epo import RateLimiter, _atomic_write, _safe_name
from .uspto_bulk import BulkError

API_ROOT = "https://api.uspto.gov"
DEFAULT_ENDPOINT = API_ROOT + "/api/v1"
DEFAULT_TIMEOUT = 60.0
DEFAULT_SLEEP_S = 1.0
MAX_RESULTS = 25  # conservative window for a single request

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CREDENTIALS = ROOT / ".secrets" / "uspto_credentials.json"
DEFAULT_CACHE = ROOT / "data" / "external" / "uspto_raw"
DEFAULT_RATE_STATE = Path(
    os.environ.get("USPTO_RATE_STATE")
    or ROOT / "data" / "external" / "uspto_rate_state.json"
)
DEFAULT_USER_AGENT = f"pat-okf-agent/{__version__} (research; cache-first)"


class UsptoError(Exception):
    """Base class for ODP client errors."""


class UsptoCredentialsError(UsptoError):
    """The ODP API key is missing or unusable."""


class UsptoOfflineError(UsptoError):
    """A network request was required but the client is in offline mode."""


class UsptoApiError(UsptoError):
    """The ODP API returned an error or an unparsable response."""

    def __init__(self, message: str, *, status: int | None = None,
                 url: str | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


@dataclass(frozen=True)
class UsptoCredentials:
    api_key: str
    endpoint: str = DEFAULT_ENDPOINT
    source: str = "env"

    @classmethod
    def load(cls, path: str | Path | None = None, *,
             env: Mapping[str, str] | None = None) -> "UsptoCredentials":
        env = os.environ if env is None else env
        key = (env.get("USPTO_API_KEY") or "").strip()
        endpoint = (env.get("USPTO_API_ENDPOINT") or DEFAULT_ENDPOINT).strip()
        if key:
            return cls(api_key=key, endpoint=endpoint, source="env")
        candidate = Path(path or env.get("USPTO_CREDENTIALS") or DEFAULT_CREDENTIALS)
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise UsptoCredentialsError(
                    f"cannot read credentials file {candidate}: {exc}") from exc
            key = str(data.get("api_key") or data.get("USPTO_API_KEY") or "").strip()
            endpoint = str(data.get("endpoint") or endpoint).strip()
            if key:
                return cls(api_key=key, endpoint=endpoint, source=str(candidate))
        raise UsptoCredentialsError(
            "USPTO API key is missing. Create a free key at "
            "https://data.uspto.gov/apikey and set USPTO_API_KEY or "
            f"write {DEFAULT_CREDENTIALS}. PatentsView is retired; ODP needs a key."
        )


def _application_digits(number: str) -> str:
    digits = re.sub(r"[^0-9]", "", str(number))
    if not digits:
        raise ValueError(f"cannot parse application number: {number!r}")
    return digits


def parse_data_bag(payload: Any) -> list[dict[str, Any]]:
    """Return the ``patentFileWrapperDataBag`` records of an ODP response."""
    if not isinstance(payload, dict):
        return []
    bag = payload.get("patentFileWrapperDataBag")
    return bag if isinstance(bag, list) else []


class UsptoClient:
    """Cache-first USPTO ODP client with API-key auth and fair-use spacing."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        credentials_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
        endpoint: str | None = None,
        sleep_s: float = DEFAULT_SLEEP_S,
        timeout: float = DEFAULT_TIMEOUT,
        offline: bool = False,
        transports: Callable[[urllib.request.Request, float], Any] | None = None,
        limiter: RateLimiter | None = None,
        state_path: str | Path | bool | None = DEFAULT_RATE_STATE,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        user_agent: str | None = None,
    ):
        if api_key:
            credentials = UsptoCredentials(
                api_key=api_key.strip(),
                endpoint=endpoint or DEFAULT_ENDPOINT,
                source="argument")
        else:
            credentials = UsptoCredentials.load(credentials_path)
            if endpoint:
                credentials = UsptoCredentials(
                    api_key=credentials.api_key,
                    endpoint=endpoint,
                    source=credentials.source)
        if not credentials.api_key:
            raise UsptoCredentialsError("USPTO API key is required")

        self.credentials = credentials
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE)
        self.timeout = float(timeout)
        self.offline = bool(offline)
        self.user_agent = user_agent or os.environ.get(
            "USPTO_USER_AGENT") or DEFAULT_USER_AGENT
        self._transport = transports or (
            lambda request, timeout: urllib.request.urlopen(
                request, timeout=timeout))
        self._limiter = limiter or RateLimiter(
            sleep_s, clock=time.monotonic, sleeper=sleeper,
            state_path=state_path)
        self._sleep = sleeper
        self._clock = clock

    # -- requests ----------------------------------------------------------

    def _cache_path(self, name: str) -> Path:
        return self.cache_dir / f"{_safe_name(name)}.json"

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        name: str | None = None,
        refresh: bool = False,
        retries: int = 2,
    ) -> Any:
        """GET an ODP endpoint, using the on-disk cache unless ``refresh``."""
        endpoint = self.credentials.endpoint.rstrip("/")
        url = endpoint + "/" + path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                    clean)
        cache_name = name or (
            "uspto_" + hashlib.sha1(url.encode()).hexdigest()[:20])
        cache_path = self._cache_path(cache_name)

        if not refresh and cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass  # unparsable legacy entry -> treat as a miss

        if self.offline:
            raise UsptoOfflineError(
                f"no cached response for {cache_name}; offline mode is active")

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "X-API-KEY": self.credentials.api_key,
            }
            request = urllib.request.Request(url, headers=headers)
            self._limiter.wait()
            try:
                with self._transport(request, self.timeout) as response:
                    body = response.read()
                    status = getattr(response, "status", 200)
            except urllib.error.HTTPError as exc:
                with closing(exc):
                    body = exc.read()
                    status = exc.code
                    text = body.decode("utf-8", errors="replace")
                    if status in (401, 403):
                        raise UsptoApiError(
                            f"USPTO ODP HTTP {status}: API key missing, invalid "
                            "or deactivated, or access is not permitted. Get a "
                            "free key at https://data.uspto.gov/apikey and set "
                            "USPTO_API_KEY.",
                            status=status, url=url, body=text[:1000]) from exc
                    if (status in (429, 500, 502, 503, 504)
                            and attempt < retries):
                        retry_after = 0.0
                        try:
                            retry_after = float(
                                (exc.headers or {}).get("Retry-After", 0))
                        except (TypeError, ValueError):
                            pass
                        self._sleep(max(retry_after, 2.0 * (attempt + 1)))
                        last_error = UsptoApiError(
                            f"HTTP {status}", status=status, url=url,
                            body=text[:1000])
                        continue
                    self._write_error(cache_name, url, status, text)
                    raise UsptoApiError(
                        f"USPTO ODP HTTP {status} for {url}",
                        status=status, url=url, body=text[:1000]) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                raise UsptoApiError(
                    f"USPTO ODP request failed for {url}: {exc}",
                    url=url) from exc

            if status >= 400:
                text = body.decode("utf-8", errors="replace")
                self._write_error(cache_name, url, status, text)
                raise UsptoApiError(
                    f"USPTO ODP HTTP {status} for {url}",
                    status=status, url=url, body=text[:1000])
            _atomic_write(cache_path, body.decode("utf-8", errors="replace"))
            try:
                return json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise UsptoApiError(
                    "USPTO ODP returned non-JSON", status=200, url=url,
                    body=body.decode("utf-8", errors="replace")[:1000]) from exc

        raise UsptoApiError(
            f"USPTO ODP request failed for {url}: {last_error}", url=url)

    def _write_error(self, name: str, url: str, status: int, body: str) -> None:
        try:
            _atomic_write(
                self.cache_dir / f"{_safe_name(name)}.error.txt",
                f"URL: {url}\nSTATUS: {status}\n\n{body}")
        except OSError:
            pass

    # -- convenience methods ----------------------------------------------

    def search(
        self,
        q: str,
        *,
        start: int = 0,
        limit: int = MAX_RESULTS,
        fields: Sequence[str] | None = None,
        sort: Sequence[str] | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Search patent applications (default top 25 per request)."""
        if start < 0:
            raise ValueError("start must be >= 0")
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(
                f"limit must be 1..{MAX_RESULTS} (conservative window)")
        params: dict[str, Any] = {
            "q": q,
            "start": start,
            "limit": limit,
            "fields": ",".join(fields) if fields else None,
            "sort": ",".join(sort) if sort else None,
            "dateRangeData.startDate": date_start,
            "dateRangeData.endDate": date_end,
        }
        return self.get_json("/patent/applications/search", params=params,
                             refresh=refresh)

    def application(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        app = _application_digits(number)
        return self.get_json(f"/patent/applications/{app}", refresh=refresh)

    def metadata(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "meta-data", refresh=refresh)

    def continuity(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "continuity", refresh=refresh)

    def transactions(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "transactions", refresh=refresh)

    def documents(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "documents", refresh=refresh)

    def assignment(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "assignment", refresh=refresh)

    def attorney(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "attorney", refresh=refresh)

    def foreign_priority(self, number: str, *,
                        refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "foreign-priority", refresh=refresh)

    def adjustment(self, number: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._sub(number, "adjustment", refresh=refresh)

    def status_codes(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.get_json("/patent/status-codes", refresh=refresh)

    def _sub(self, number: str, section: str, *, refresh: bool) -> dict[str, Any]:
        app = _application_digits(number)
        return self.get_json(f"/patent/applications/{app}/{section}",
                             refresh=refresh)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _make_client(args) -> UsptoClient:
    kwargs: dict[str, Any] = {}
    if getattr(args, "no_rate_state", False):
        kwargs["state_path"] = False
    return UsptoClient(
        credentials_path=args.credentials,
        cache_dir=args.cache_dir,
        sleep_s=args.sleep,
        offline=args.offline,
        **kwargs,
    )


def _download_api_key(credentials_path: str | None) -> str | None:
    """Best-effort key lookup for file downloads (public URLs work without)."""
    try:
        return UsptoCredentials.load(credentials_path).api_key
    except UsptoCredentialsError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m pat_okf_agent.uspto",
        description="USPTO Open Data Portal (Patent File Wrapper) client")
    parser.add_argument("--credentials", default=None,
                        help="ODP credentials JSON (default: .secrets/uspto_credentials.json)")
    parser.add_argument("--cache-dir", default=None,
                        help="raw ODP response cache directory")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S,
                        help="minimum seconds between requests (default 1)")
    parser.add_argument("--offline", action="store_true",
                        help="use only cached responses")
    parser.add_argument("--no-rate-state", action="store_true",
                        help="disable cross-process spacing (single-user only)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="resolve the API key (no network)")
    p_search = sub.add_parser("search", help="search applications")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=MAX_RESULTS)
    p_search.add_argument("--start", type=int, default=0)
    p_search.add_argument("--refresh", action="store_true")
    for name in ("app", "metadata", "continuity", "transactions", "documents",
                 "assignment", "attorney", "foreign-priority", "adjustment"):
        sub.add_parser(name, help=f"fetch {name}").add_argument(
            "publication")
    p_status = sub.add_parser("status-codes", help="application status codes")
    p_status.add_argument("--refresh", action="store_true")

    p_dl = sub.add_parser("download", help="download an ODP/bulk file")
    p_dl.add_argument("url")
    p_dl.add_argument("-o", "--dest", required=True,
                      help="target file or existing directory")
    p_dl.add_argument("--no-key", action="store_true",
                      help="do not send the ODP API key header")

    p_parse = sub.add_parser("parse", help="parse a USPTO grant XML file")
    p_parse.add_argument("file")
    p_parse.add_argument("--claims", action="store_true",
                         help="include claim texts in the output")
    p_parse.add_argument("--stream", action="store_true",
                         help="iterate documents in a large multi-doc file")

    p_unzip = sub.add_parser("extract", help="safely extract a ZIP archive")
    p_unzip.add_argument("archive")
    p_unzip.add_argument("-o", "--dest", required=True)

    p_prods = sub.add_parser("products", help="search the bulk dataset directory")
    p_prods.add_argument("query", nargs="?", default="")
    p_prods.add_argument("--limit", type=int, default=25)
    p_prods.add_argument("--offset", type=int, default=0)

    p_prod = sub.add_parser("product", help="list files of a bulk product")
    p_prod.add_argument("product")
    p_prod.add_argument("--year", type=int, default=None)
    p_prod.add_argument("--from", dest="date_from", default=None)
    p_prod.add_argument("--to", dest="date_to", default=None)
    p_prod.add_argument("--limit", type=int, default=50)

    p_year = sub.add_parser("year",
                            help="download every file of a product for a year")
    p_year.add_argument("product")
    p_year.add_argument("year", type=int)
    p_year.add_argument("-o", "--dest", required=True)
    p_year.add_argument("--dry-run", action="store_true")
    p_year.add_argument("--no-key", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            creds = UsptoCredentials.load(args.credentials)
            print(f"credentials: {creds.source}")
            print(f"endpoint: {creds.endpoint}")
            print(f"api_key: {creds.api_key[:4]}… ({len(creds.api_key)} chars)")
            return 0
        if args.command == "download":
            from pathlib import Path as _Path
            from .uspto_bulk import download as _download
            dest = _Path(args.dest)
            if dest.is_dir():
                name = args.url.rstrip("/").rsplit("/", 1)[-1] or "download.bin"
                dest = dest / name
            key = None if args.no_key else _download_api_key(args.credentials)
            size = _download(args.url, dest, api_key=key)
            print(json.dumps({"path": str(dest), "bytes": size,
                              "api_key_sent": bool(key)},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "parse":
            from .uspto_bulk import (iter_grant_documents,
                                     iter_grant_documents_zip,
                                     parse_grant_xml)
            if str(args.file).lower().endswith(".zip") or args.stream:
                source = (iter_grant_documents_zip(args.file)
                          if str(args.file).lower().endswith(".zip")
                          else iter_grant_documents(args.file))
                for doc in source:
                    if args.claims:
                        print(json.dumps(doc, ensure_ascii=False))
                    else:
                        print(json.dumps(
                            {k: doc[k] for k in ("publication_number", "title",
                                                 "claim_count")},
                            ensure_ascii=False))
                return 0
            doc = parse_grant_xml(args.file)
            if not args.claims:
                doc.pop("claims", None)
            _json_print(doc)
            return 0
        if args.command == "extract":
            from .uspto_bulk import extract_zip
            files = extract_zip(args.archive, args.dest)
            _json_print({"extracted": len(files),
                         "files": [str(p) for p in files[:50]]})
            return 0

        client = _make_client(args)
        if args.command == "search":
            _json_print(client.search(args.query, start=args.start,
                                      limit=args.limit,
                                      refresh=args.refresh))
        elif args.command == "app":
            _json_print(client.application(args.publication))
        elif args.command == "metadata":
            _json_print(client.metadata(args.publication))
        elif args.command == "continuity":
            _json_print(client.continuity(args.publication))
        elif args.command == "transactions":
            _json_print(client.transactions(args.publication))
        elif args.command == "documents":
            _json_print(client.documents(args.publication))
        elif args.command == "assignment":
            _json_print(client.assignment(args.publication))
        elif args.command == "attorney":
            _json_print(client.attorney(args.publication))
        elif args.command == "foreign-priority":
            _json_print(client.foreign_priority(args.publication))
        elif args.command == "adjustment":
            _json_print(client.adjustment(args.publication))
        elif args.command == "status-codes":
            _json_print(client.status_codes(refresh=args.refresh))
        elif args.command == "products":
            from .uspto_bulk import search_products
            _json_print(search_products(client, args.query, offset=args.offset,
                                        limit=args.limit))
        elif args.command == "product":
            from .uspto_bulk import product_files
            files = product_files(client, args.product, year=args.year,
                                  date_from=args.date_from,
                                  date_to=args.date_to, limit=args.limit)
            _json_print({"product": args.product, "year": args.year,
                         "count": len(files),
                         "files": [{k: f.get(k) for k in (
                             "fileName", "fileSize", "fileDataFromDate",
                             "fileDataToDate", "fileDownloadURI")}
                             for f in files]})
        elif args.command == "year":
            from .uspto_bulk import download_year
            key = None if args.no_key else _download_api_key(args.credentials)
            _json_print(download_year(client, args.product, args.year,
                                      args.dest, api_key=key,
                                      dry_run=args.dry_run))
        return 0
    except UsptoError as exc:
        print(f"error: {exc}")
        return 2
    except BulkError as exc:
        print(f"error: {exc}")
        return 2
    except ValueError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
