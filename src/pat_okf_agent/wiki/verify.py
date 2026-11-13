"""OKF source verification tool (okf verify-source).

Provides deterministic ledger verification (Crossref, JaLC, DataCite, arXiv)
and Web URL snapshot capturing with publisher extraction and SHA-256 evidence.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from pat_okf_agent.epo import RateLimiter, _atomic_write, _safe_name
from pat_okf_agent.wiki.model import KNOWN_SOURCE_KINDS, parse_source_entry

# Exceptions


class SourceVerifyError(RuntimeError):
    """Base exception for source verification errors."""


class SourceOfflineError(SourceVerifyError):
    """Raised when an operation requires network in offline mode."""


class SourceRateLimitError(SourceVerifyError):
    """Raised when rate limits or HTTP 429 retries are exceeded."""


class SourceApiError(SourceVerifyError):
    """Raised on upstream HTTP or API errors."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


# Data models


@dataclass(frozen=True)
class ClassifiedTarget:
    kind: str
    identifier: str
    identifier_type: str  # "doi" | "arxiv" | "url"
    raw_id: str


@dataclass
class SourceMetadata:
    title: str | None = None
    alternate_titles: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    year: int | None = None
    container_title: str | None = None
    work_type: str | None = None
    is_retracted: bool = False
    published_doi: str | None = None
    url: str | None = None
    abstract_available: bool = False
    abstract: str | None = None
    abstract_source: str | None = None


@dataclass
class VerificationChecks:
    title_match: bool | None = None
    author_match: bool | None = None
    year_match: bool | None = None
    claim_matches: dict[str, bool] = field(default_factory=dict)


@dataclass
class EvidenceRecord:
    cached: bool = False
    raw_path: str | None = None
    snapshot_path: str | None = None
    meta_path: str | None = None
    sha256: str | None = None


@dataclass
class VerificationResult:
    ok: bool
    status: str  # "verified" | "not_found" | "mismatch" | "unverified" | "error"
    input: str
    identifier: str
    identifier_type: str
    kind: str
    registration_agency: str | None
    via: str
    retrieved_at: str
    issued: str | None
    metadata: SourceMetadata
    checks: VerificationChecks
    evidence: EvidenceRecord
    okf_source: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def format_okf_source(self) -> str | None:
        """Format an OKF frontmatter source entry if verification succeeded."""
        if self.status != "verified":
            return None

        tokens = [f"{self.kind}:{self.identifier}"]
        if self.issued:
            tokens.append(f"issued={self.issued}")
        if self.retrieved_at:
            tokens.append(f"retrieved={self.retrieved_at}")
        if self.via:
            tokens.append(f"via={self.via}")

        source_str = " ".join(tokens)
        # Validate format against model parser
        parse_source_entry(source_str)
        return source_str

    def as_dict(self) -> dict[str, Any]:
        """Convert verification result to dictionary matching the JSON output contract."""
        return {
            "ok": self.ok,
            "status": self.status,
            "input": self.input,
            "identifier": self.identifier,
            "identifier_type": self.identifier_type,
            "kind": self.kind,
            "registration_agency": self.registration_agency,
            "via": self.via,
            "retrieved_at": self.retrieved_at,
            "issued": self.issued,
            "metadata": asdict(self.metadata),
            "checks": asdict(self.checks),
            "evidence": asdict(self.evidence),
            "okf_source": self.okf_source or self.format_okf_source(),
            "warnings": list(self.warnings),
            "error": self.error,
        }


# Target classification

_DOI_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?(10\.\d{4,9}/[^\s]+)$",
    re.IGNORECASE,
)
_ARXIV_MODERN_RE = re.compile(r"^(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(
    r"^(?:arXiv:)?([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$", re.IGNORECASE
)
_ARXIV_URL_RE = re.compile(
    r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/([^?#\s]+?)(?:\.pdf)?$",
    re.IGNORECASE,
)


def classify_target(target: str, *, kind: str | None = None) -> ClassifiedTarget:
    """Classify and normalize an input target into DOI, arXiv, or URL."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Target must be a non-empty string")

    raw = target.strip()
    prefix_kind: str | None = None

    if ":" in raw:
        head, tail = raw.split(":", 1)
        if head in KNOWN_SOURCE_KINDS and tail:
            prefix_kind = head
            raw = tail.strip()

    effective_kind = kind or prefix_kind

    # Check DOI first
    m_doi = _DOI_RE.match(raw)
    if m_doi:
        bare_doi = m_doi.group(1).rstrip(".,;").lower()
        return ClassifiedTarget(
            kind=effective_kind or "paper",
            identifier=bare_doi,
            identifier_type="doi",
            raw_id=bare_doi,
        )

    # Check arXiv
    m_arx_url = _ARXIV_URL_RE.match(raw)
    if m_arx_url:
        bare_id = m_arx_url.group(1)
        return ClassifiedTarget(
            kind=effective_kind or "preprint",
            identifier=f"arXiv:{bare_id}",
            identifier_type="arxiv",
            raw_id=bare_id,
        )

    m_arx_mod = _ARXIV_MODERN_RE.match(raw)
    if m_arx_mod:
        bare_id = m_arx_mod.group(1)
        return ClassifiedTarget(
            kind=effective_kind or "preprint",
            identifier=f"arXiv:{bare_id}",
            identifier_type="arxiv",
            raw_id=bare_id,
        )

    m_arx_old = _ARXIV_OLD_RE.match(raw)
    if m_arx_old:
        bare_id = m_arx_old.group(1)
        return ClassifiedTarget(
            kind=effective_kind or "preprint",
            identifier=f"arXiv:{bare_id}",
            identifier_type="arxiv",
            raw_id=bare_id,
        )

    # Check Web URL
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        # Strip URL fragment
        cleaned_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )
        return ClassifiedTarget(
            kind=effective_kind or "web",
            identifier=cleaned_url,
            identifier_type="url",
            raw_id=cleaned_url,
        )

    raise ValueError(f"Unrecognized or unsupported target format: {target!r}")


# ---------------------------------------------------------------------------
# Verifier implementation
# ---------------------------------------------------------------------------

DEFAULT_CACHE_DIR = Path("data/external/source_verify")
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024  # 10 MB


class _HtmlSnapshotParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._in_time = False
        self._ignore_depth = 0
        self.title_candidates: list[str] = []
        self.publisher_candidates: dict[str, str] = {}
        self.date_candidates: dict[str, str] = {}
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript"):
            self._ignore_depth += 1
            return
        if t == "title":
            self._in_title = True
            return
        if t == "time":
            self._in_time = True
            attr_dict = {k.lower(): (v or "") for k, v in attrs}
            if "datetime" in attr_dict and attr_dict["datetime"].strip():
                self.date_candidates.setdefault("time_datetime", attr_dict["datetime"].strip())
            return

        if t == "meta":
            attr_dict = {k.lower(): (v or "") for k, v in attrs}
            name = (attr_dict.get("name") or attr_dict.get("property") or "").lower()
            content = attr_dict.get("content", "").strip()
            if not content:
                return

            # Title
            if name == "citation_title":
                self.title_candidates.insert(0, content)
            elif name in ("dc.title", "dcterms.title"):
                self.title_candidates.append(content)
            elif name == "og:title":
                self.title_candidates.append(content)

            # Publisher
            if name == "citation_publisher":
                self.publisher_candidates.setdefault("citation_publisher", content)
            elif name in (
                "citation_technical_report_institution",
                "citation_dissertation_institution",
            ):
                self.publisher_candidates.setdefault("citation_institution", content)
            elif name in ("dc.publisher", "dcterms.publisher"):
                self.publisher_candidates.setdefault("dc_publisher", content)
            elif name == "og:site_name":
                self.publisher_candidates.setdefault("og_site_name", content)

            # Date
            if name in ("citation_publication_date", "citation_date"):
                self.date_candidates.setdefault("citation_date", content)
            elif name == "article:published_time":
                self.date_candidates.setdefault("article_published_time", content)
            elif name in ("dc.date", "dcterms.issued"):
                self.date_candidates.setdefault("dc_date", content)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript") and self._ignore_depth > 0:
            self._ignore_depth -= 1
        elif t == "title":
            self._in_title = False
        elif t == "time":
            self._in_time = False

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        cleaned = data.strip()
        if self._in_title and cleaned:
            self.title_candidates.append(cleaned)
        if cleaned:
            self.text_parts.append(cleaned)

    def get_title(self) -> str | None:
        return self.title_candidates[0] if self.title_candidates else None

    def get_publisher(self) -> str | None:
        for k in (
            "citation_publisher",
            "citation_institution",
            "dc_publisher",
            "og_site_name",
        ):
            if k in self.publisher_candidates:
                return self.publisher_candidates[k]
        return None

    def get_date(self) -> tuple[str | None, int | None]:
        raw_date: str | None = None
        for k in (
            "citation_date",
            "article_published_time",
            "dc_date",
            "time_datetime",
        ):
            if k in self.date_candidates:
                raw_date = self.date_candidates[k]
                break

        if not raw_date:
            return None, None

        # Look for full date YYYY-MM-DD
        m_full = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw_date)
        if m_full:
            y, m, d = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
            cand = f"{y:04d}-{m:02d}-{d:02d}"
            try:
                date.fromisoformat(cand)
                return cand, y
            except ValueError:
                pass

        # Look for year only
        m_year = re.search(r"(\d{4})", raw_date)
        if m_year:
            return None, int(m_year.group(1))

        return None, None

    def get_visible_text(self) -> str:
        return _clean_space(" ".join(self.text_parts))


def _run_pdftotext(data: bytes) -> str | None:
    """Attempt extracting text via pdftotext CLI (poppler), returning cleaned text or None."""
    if not shutil.which("pdftotext"):
        return None
    try:
        proc = subprocess.run(
            ["pdftotext", "-", "-"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            cleaned = _clean_space(text)
            return cleaned if cleaned else None
    except Exception:
        return None
    return None


def _extract_pdf_text(data: bytes) -> tuple[str, bool, str]:
    """Extract text from PDF using pypdf if available, falling back to pdftotext CLI and ascii."""
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        pypdf_text = _clean_space(" ".join(parts))
        if pypdf_text:
            return pypdf_text, True, "pypdf"
        # pypdf yielded empty or near-empty text: try pdftotext fallback
        poppler_text = _run_pdftotext(data)
        if poppler_text:
            return poppler_text, True, "pdftotext"
        return "", True, "pypdf"
    except ImportError:
        # Fallback: extract ASCII-like strings from binary
        ascii_text = re.findall(rb"[A-Za-z0-9 .,:;!?/\-]{4,}", data)
        text_str = " ".join(b.decode("ascii", errors="ignore") for b in ascii_text)
        return _clean_space(text_str), False, "ascii_fallback"
    except Exception:
        poppler_text = _run_pdftotext(data)
        if poppler_text:
            return poppler_text, True, "pdftotext"
        return "", False, "pypdf_error"


def _extract_response_url(resp: Any, default_url: str) -> str:
    if hasattr(resp, "geturl") and callable(resp.geturl):
        try:
            u = resp.geturl()
            if u:
                return str(u)
        except Exception:
            pass
    u = getattr(resp, "url", None)
    if u:
        return str(u)
    return default_url


def _atomic_write_bytes(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    tmp.replace(path)


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def snapshot_key_for_url(target_or_url: str | ClassifiedTarget) -> str:
    """Return deterministic snapshot filename stem '<netloc_safe>_<sha1[:16]>' for a URL."""
    classified = (
        target_or_url
        if isinstance(target_or_url, ClassifiedTarget)
        else classify_target(target_or_url)
    )
    if classified.identifier_type != "url":
        raise ValueError(
            f"Expected URL target, got {classified.identifier_type}: {target_or_url!r}"
        )
    url = classified.identifier  # URL fragment stripped by classify_target
    parsed = urllib.parse.urlsplit(url)
    netloc_safe = _safe_name(parsed.netloc)
    sha1_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{netloc_safe}_{sha1_hash}"


def set_snapshot_orphan_reason(
    cache_dir: Path | str, target_or_url: str | ClassifiedTarget, reason: str
) -> Path:
    """Record orphan/rejection reason into an existing snapshot's .meta.json file."""
    if not reason or not reason.strip():
        raise ValueError("orphan_reason must not be empty")
    cleaned_reason = reason.strip()
    key = snapshot_key_for_url(target_or_url)
    snapshots_dir = Path(cache_dir) / "snapshots"
    meta_path = snapshots_dir / f"{key}.meta.json"
    if not meta_path.is_file():
        raise SourceVerifyError(
            f"Snapshot metadata not found for {target_or_url}: {meta_path}"
        )
    meta_json = json.loads(meta_path.read_text("utf-8"))
    meta_json["orphan_reason"] = cleaned_reason
    _atomic_write_bytes(
        meta_path,
        json.dumps(meta_json, indent=2, ensure_ascii=False),
    )
    return meta_path


def _strip_jats_xml(raw: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", raw)
    cleaned = html.unescape(cleaned)
    return _clean_space(cleaned)


def _reconstruct_openalex_abstract(inverted_index: Any) -> str | None:
    """Reconstruct plaintext abstract from OpenAlex abstract_inverted_index."""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted_index.items():
        if not isinstance(word, str) or not isinstance(idxs, list):
            continue
        for idx in idxs:
            if isinstance(idx, int):
                positions.append((idx, word))
    if not positions:
        return None
    positions.sort(key=lambda item: item[0])
    text = " ".join(word for _, word in positions)
    cleaned = _clean_space(text)
    return cleaned if cleaned else None


def _parse_date_parts(date_obj: Any) -> tuple[str | None, int | None]:
    if not isinstance(date_obj, dict):
        return None, None
    dps = date_obj.get("date-parts")
    if not isinstance(dps, list) or not dps or not isinstance(dps[0], list):
        return None, None
    parts = dps[0]
    year: int | None = None
    issued: str | None = None
    if len(parts) >= 1:
        try:
            year = int(parts[0])
        except (ValueError, TypeError):
            year = None
    if len(parts) >= 3 and year is not None:
        try:
            m = int(parts[1])
            d = int(parts[2])
            candidate = f"{year:04d}-{m:02d}-{d:02d}"
            date.fromisoformat(candidate)
            issued = candidate
        except (ValueError, TypeError):
            issued = None
    return issued, year


def _evaluate_checks(
    meta: SourceMetadata,
    *,
    expect_title: str | None = None,
    expect_author: str | None = None,
    expect_year: int | None = None,
    check_claim: list[str] | None = None,
    all_years: set[int] | None = None,
    text_to_check: str | None = None,
) -> tuple[VerificationChecks, str, bool]:
    years = set(all_years or ())
    if meta.year is not None:
        years.add(meta.year)

    # Title check
    title_match: bool | None = None
    if expect_title:
        exp_clean = _clean_space(expect_title).casefold()
        tokens_expect = set(re.findall(r"\w+", exp_clean))
        tokens_got = set(re.findall(r"\w+", (meta.title or "").casefold()))
        for alt in meta.alternate_titles:
            tokens_got |= set(re.findall(r"\w+", alt.casefold()))

        if not tokens_expect:
            title_match = True
        else:
            overlap = len(tokens_expect & tokens_got) / len(tokens_expect)
            title_match = (
                exp_clean in (meta.title or "").casefold()
                or any(exp_clean in alt.casefold() for alt in meta.alternate_titles)
                or overlap >= 0.8
            )

    # Author check
    author_match: bool | None = None
    if expect_author:
        exp_author = _clean_space(expect_author).casefold()
        author_match = any(exp_author in a.casefold() for a in meta.authors)

    # Year check
    year_match: bool | None = None
    if expect_year is not None:
        year_match = expect_year in years

    # Claim checks
    claim_matches: dict[str, bool] = {}
    content = text_to_check or meta.abstract or ""
    norm_content = (
        _clean_space(content)
        .casefold()
        .replace("·", "")
        .replace("・", "")
        .replace("-", "")
    )
    for claim in check_claim or []:
        norm_claim = (
            _clean_space(claim)
            .casefold()
            .replace("·", "")
            .replace("・", "")
            .replace("-", "")
        )
        claim_matches[claim] = bool(norm_claim and norm_claim in norm_content)

    checks = VerificationChecks(
        title_match=title_match,
        author_match=author_match,
        year_match=year_match,
        claim_matches=claim_matches,
    )

    if meta.is_retracted:
        return checks, "mismatch", False
    if title_match is False or author_match is False or year_match is False:
        return checks, "mismatch", False
    if any(v is False for v in claim_matches.values()):
        return checks, "unverified", False

    return checks, "verified", True


class SourceVerifier:
    """Verifier for DOIs, arXiv IDs, and Web URLs with cache and politeness."""

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        transports: Callable[[urllib.request.Request, float], Any] | None = None,
        limiter: RateLimiter | None = None,
        offline: bool = False,
        refresh: bool = False,
        mailto: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        retries: int = 2,
        timeout: float = 30.0,
        sleep: float | None = None,
        no_rate_state: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.transports = transports
        self.offline = offline
        self.refresh = refresh
        self.mailto = (
            mailto
            or os.environ.get("OKF_VERIFY_MAILTO")
            or os.environ.get("CROSSREF_MAILTO")
        )
        self.sleeper = sleeper
        self.clock = clock
        self.retries = max(0, int(retries))
        self.timeout = float(timeout)
        self.default_sleep = sleep
        self._last_final_url: str = ""

        state_path = None
        if not no_rate_state:
            state_path = (
                os.environ.get("OKF_VERIFY_RATE_STATE")
                or self.cache_dir.parent / "source_verify_rate_state.json"
            )

        self.limiter = limiter or RateLimiter(
            min_interval=self.default_sleep if self.default_sleep is not None else 1.5,
            clock=clock,
            sleeper=sleeper,
            state_path=state_path,
        )

    def _get_ua(self) -> str:
        if self.mailto:
            return f"pat-okf-agent/0.2.0 (research; cache-first; mailto:{self.mailto})"
        return "pat-okf-agent/0.2.0 (research; cache-first)"

    def _http_request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        is_ledger: bool = True,
        interval: float = 1.5,
    ) -> tuple[int, dict[str, str], bytes]:
        req_headers = {"User-Agent": self._get_ua()}
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, headers=req_headers)

        effective_interval = (
            self.default_sleep if self.default_sleep is not None else interval
        )
        self.limiter.min_interval = effective_interval
        self.limiter.wait()

        for attempt in range(self.retries + 1):
            if self.offline:
                raise SourceOfflineError(f"Offline mode: cache miss for {url}")
            try:
                if self.transports is not None:
                    resp = self.transports(req, self.timeout)
                else:
                    resp = urllib.request.urlopen(req, timeout=self.timeout)

                self._last_final_url = _extract_response_url(resp, url)

                with resp:
                    body = resp.read()
                    status = getattr(resp, "status", 200)
                    resp_headers = {}
                    if hasattr(resp, "headers") and resp.headers:
                        resp_headers = {k: v for k, v in resp.headers.items()}
                    return status, resp_headers, body
            except urllib.error.HTTPError as exc:
                self._last_final_url = _extract_response_url(exc, url)
                status = exc.code
                body = exc.read() if hasattr(exc, "read") else b""
                resp_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}

                if status == 404:
                    return 404, resp_headers, body
                if status == 429:
                    retry_after_hdr = resp_headers.get("Retry-After", "0")
                    try:
                        retry_after = float(retry_after_hdr)
                    except ValueError:
                        retry_after = 0.0
                    if attempt < self.retries:
                        wait_s = max(retry_after, 2.5 * (2**attempt))
                        self.sleeper(wait_s)
                        continue
                    raise SourceRateLimitError(
                        f"Rate limit exceeded (HTTP 429) for {url}"
                    )
                if status in (500, 502, 503, 504):
                    if attempt < self.retries:
                        wait_s = 2.5 * (2**attempt)
                        self.sleeper(wait_s)
                        continue
                    raise SourceApiError(status, url, body.decode("utf-8", errors="replace"))
                if status in (401, 403):
                    if is_ledger:
                        raise SourceRateLimitError(
                            f"Access forbidden (HTTP {status}) on ledger API {url}"
                        )
                    return status, resp_headers, body
                raise SourceApiError(status, url, body.decode("utf-8", errors="replace"))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.retries:
                    self.sleeper(2.5 * (2**attempt))
                    continue
                raise SourceApiError(0, url, str(exc))

        raise SourceApiError(0, url, "Max retries exceeded")

    def verify(
        self,
        target: str,
        *,
        kind: str | None = None,
        issued: str | None = None,
        expect_title: str | None = None,
        expect_author: str | None = None,
        expect_year: int | None = None,
        check_claim: list[str] | None = None,
        verify_landing: bool = False,
    ) -> VerificationResult:
        classified = classify_target(target, kind=kind)
        if classified.identifier_type == "doi":
            return self.verify_doi(
                classified,
                issued=issued,
                expect_title=expect_title,
                expect_author=expect_author,
                expect_year=expect_year,
                check_claim=check_claim,
                verify_landing=verify_landing,
            )
        if classified.identifier_type == "arxiv":
            return self.verify_arxiv(
                classified,
                issued=issued,
                expect_title=expect_title,
                expect_author=expect_author,
                expect_year=expect_year,
                check_claim=check_claim,
            )
        if classified.identifier_type == "url":
            return self.verify_url(
                classified,
                issued=issued,
                expect_title=expect_title,
                expect_author=expect_author,
                expect_year=expect_year,
                check_claim=check_claim,
            )
        raise NotImplementedError(
            f"Identifier type {classified.identifier_type} implemented in later units"
        )

    def verify_doi(
        self,
        target: ClassifiedTarget | str,
        *,
        issued: str | None = None,
        expect_title: str | None = None,
        expect_author: str | None = None,
        expect_year: int | None = None,
        check_claim: list[str] | None = None,
        verify_landing: bool = False,
    ) -> VerificationResult:
        classified = (
            target
            if isinstance(target, ClassifiedTarget)
            else classify_target(target)
        )
        bare_doi = classified.identifier
        safe_doi = _safe_name(bare_doi)
        today = date.today().isoformat()
        retrieved_at = today

        crossref_cache = self.cache_dir / "crossref" / f"{safe_doi}.json"
        cached = False
        raw_path: str | None = None
        sha256_digest: str | None = None
        agency: str | None = None
        via: str = "crossref"
        meta = SourceMetadata()
        extracted_issued: str | None = None
        all_years: set[int] = set()

        # 1. Try Crossref Cache
        if crossref_cache.exists() and not self.refresh:
            cached = True
            raw_path = str(crossref_cache)
            data_bytes = crossref_cache.read_bytes()
            sha256_digest = f"sha256:{hashlib.sha256(data_bytes).hexdigest()}"
            payload = json.loads(data_bytes.decode("utf-8"))
            agency = "crossref"
            via = "crossref"
            meta, extracted_issued, all_years = self._parse_crossref_work(payload)
        else:
            # Step 1: Call Crossref Works API
            quoted_doi = urllib.parse.quote(bare_doi, safe="")
            url = f"https://api.crossref.org/works/{quoted_doi}"
            if self.mailto:
                quoted_mailto = urllib.parse.quote(self.mailto)
                url_with_mail = f"{url}?mailto={quoted_mailto}"
            else:
                url_with_mail = url

            status, _, body = self._http_request(url_with_mail, is_ledger=True, interval=1.5)
            if status == 200:
                agency = "crossref"
                via = "crossref"
                _atomic_write(crossref_cache, body.decode("utf-8", errors="replace"))
                raw_path = str(crossref_cache)
                sha256_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
                payload = json.loads(body.decode("utf-8"))
                meta, extracted_issued, all_years = self._parse_crossref_work(payload)
            elif status == 404:
                # Step 2: Fallback
                agency, via, meta, extracted_issued, all_years, raw_path, sha256_digest = (
                    self._doi_fallback(bare_doi, safe_doi)
                )
                if agency is None:
                    # Not found anywhere
                    checks = VerificationChecks(
                        title_match=False if expect_title else None,
                        author_match=False if expect_author else None,
                        year_match=False if expect_year else None,
                    )
                    return VerificationResult(
                        ok=False,
                        status="not_found",
                        input=target if isinstance(target, str) else target.raw_id,
                        identifier=bare_doi,
                        identifier_type="doi",
                        kind=classified.kind,
                        registration_agency=None,
                        via="crossref",
                        retrieved_at=retrieved_at,
                        issued=issued or None,
                        metadata=meta,
                        checks=checks,
                        evidence=EvidenceRecord(cached=False),
                        okf_source=None,
                    )
            else:
                raise SourceApiError(status, url, body.decode("utf-8", errors="replace"))

        # Fallback to OpenAlex for abstract if registry verification succeeded without one
        if meta.abstract is None:
            oa_abstract, oa_source = self._fetch_openalex_abstract(bare_doi, safe_doi)
            if oa_abstract:
                meta.abstract = oa_abstract
                meta.abstract_available = True
                meta.abstract_source = oa_source

        effective_issued = issued or extracted_issued
        checks, status_str, ok = _evaluate_checks(
            meta,
            expect_title=expect_title,
            expect_author=expect_author,
            expect_year=expect_year,
            check_claim=check_claim,
            all_years=all_years,
        )

        warnings: list[str] = []
        if meta.work_type == "posted-content" and classified.kind == "paper":
            warnings.append(
                "work_type is posted-content (preprint); consider kind=preprint"
            )

        evidence = EvidenceRecord(
            cached=cached,
            raw_path=raw_path,
            sha256=sha256_digest,
        )

        res = VerificationResult(
            ok=ok,
            status=status_str,
            input=target if isinstance(target, str) else target.raw_id,
            identifier=bare_doi,
            identifier_type="doi",
            kind=classified.kind,
            registration_agency=agency,
            via=via,
            retrieved_at=retrieved_at,
            issued=effective_issued,
            metadata=meta,
            checks=checks,
            evidence=evidence,
            warnings=warnings,
        )
        if ok:
            res.okf_source = res.format_okf_source()
        return res

    def _doi_fallback(
        self, bare_doi: str, safe_doi: str
    ) -> tuple[str | None, str, SourceMetadata, str | None, set[int], str | None, str | None]:
        quoted_doi = urllib.parse.quote(bare_doi, safe="")
        # Step 2a: Check DOI RA
        ra_cache = self.cache_dir / "doi_ra" / f"{safe_doi}.json"
        ra_name: str | None = None
        if ra_cache.exists() and not self.refresh:
            ra_bytes = ra_cache.read_bytes()
            ra_list = json.loads(ra_bytes.decode("utf-8"))
            if isinstance(ra_list, list) and ra_list:
                ra_name = ra_list[0].get("RA")
        else:
            ra_url = f"https://doi.org/ra/{quoted_doi}"
            status, _, body = self._http_request(ra_url, is_ledger=True, interval=1.5)
            if status == 200:
                ra_list = json.loads(body.decode("utf-8"))
                if isinstance(ra_list, list) and ra_list:
                    item = ra_list[0]
                    if item.get("status") in ("DOI does not exist", "Invalid DOI"):
                        return None, "crossref", SourceMetadata(), None, set(), None, None
                    ra_name = item.get("RA")
                    _atomic_write(ra_cache, body.decode("utf-8", errors="replace"))

        # Step 2b: JaLC API if RA == "JaLC"
        if ra_name == "JaLC":
            jalc_cache = self.cache_dir / "jalc" / f"{safe_doi}.json"
            if jalc_cache.exists() and not self.refresh:
                jalc_bytes = jalc_cache.read_bytes()
                payload = json.loads(jalc_bytes.decode("utf-8"))
                meta, issued, years = self._parse_jalc_work(payload)
                sha = f"sha256:{hashlib.sha256(jalc_bytes).hexdigest()}"
                return "jalc", "jalc", meta, issued, years, str(jalc_cache), sha
            else:
                jalc_url = f"https://api.japanlinkcenter.org/dois/{quoted_doi}"
                status, _, body = self._http_request(
                    jalc_url,
                    headers={"Accept": "application/json"},
                    is_ledger=True,
                    interval=1.5,
                )
                if status == 200:
                    _atomic_write(jalc_cache, body.decode("utf-8", errors="replace"))
                    payload = json.loads(body.decode("utf-8"))
                    meta, issued, years = self._parse_jalc_work(payload)
                    sha = f"sha256:{hashlib.sha256(body).hexdigest()}"
                    return "jalc", "jalc", meta, issued, years, str(jalc_cache), sha

        # Step 2c: doi.org content negotiation
        csl_cache = self.cache_dir / "doi_csl" / f"{safe_doi}.json"
        if csl_cache.exists() and not self.refresh:
            csl_bytes = csl_cache.read_bytes()
            payload = json.loads(csl_bytes.decode("utf-8"))
            meta, issued, years = self._parse_csl_json(payload)
            sha = f"sha256:{hashlib.sha256(csl_bytes).hexdigest()}"
            agency = ra_name.lower() if ra_name else "datacite"
            return agency, agency, meta, issued, years, str(csl_cache), sha
        else:
            doi_url = f"https://doi.org/{urllib.parse.quote(bare_doi, safe='/')}"
            status, _, body = self._http_request(
                doi_url,
                headers={"Accept": "application/vnd.citationstyles.csl+json"},
                is_ledger=True,
                interval=1.5,
            )
            if status == 200:
                _atomic_write(csl_cache, body.decode("utf-8", errors="replace"))
                payload = json.loads(body.decode("utf-8"))
                meta, issued, years = self._parse_csl_json(payload)
                sha = f"sha256:{hashlib.sha256(body).hexdigest()}"
                agency = ra_name.lower() if ra_name else "datacite"
                return agency, agency, meta, issued, years, str(csl_cache), sha

        return None, "crossref", SourceMetadata(), None, set(), None, None

    def _fetch_openalex_abstract(
        self, bare_doi: str, safe_doi: str
    ) -> tuple[str | None, str | None]:
        """Fetch abstract from OpenAlex for a DOI, returning (abstract, abstract_source).

        Caches the raw JSON at cache_dir / 'openalex' / f'{safe_doi}.json'.
        Returns (None, None) gracefully on any error or missing abstract.
        """
        openalex_cache = self.cache_dir / "openalex" / f"{safe_doi}.json"
        payload: dict[str, Any] | None = None

        if openalex_cache.exists() and not self.refresh:
            try:
                data_bytes = openalex_cache.read_bytes()
                payload = json.loads(data_bytes.decode("utf-8"))
            except Exception:
                payload = None

        if payload is None:
            quoted_doi = urllib.parse.quote(bare_doi, safe="")
            url = f"https://api.openalex.org/works/https://doi.org/{quoted_doi}"
            if self.mailto:
                quoted_mailto = urllib.parse.quote(self.mailto)
                url_with_mail = f"{url}?mailto={quoted_mailto}"
            else:
                url_with_mail = url

            try:
                status, _, body = self._http_request(
                    url_with_mail, is_ledger=True, interval=1.5
                )
                if status == 200:
                    _atomic_write(openalex_cache, body.decode("utf-8", errors="replace"))
                    payload = json.loads(body.decode("utf-8"))
            except Exception:
                return None, None

        if isinstance(payload, dict):
            inverted = payload.get("abstract_inverted_index")
            abstract = _reconstruct_openalex_abstract(inverted)
            if abstract:
                return abstract, "openalex"

        return None, None

    def _parse_crossref_work(
        self, payload: dict[str, Any]
    ) -> tuple[SourceMetadata, str | None, set[int]]:
        message = payload.get("message", payload)
        titles = message.get("title", [])
        title = _clean_space(titles[0]) if titles else ""

        authors: list[str] = []
        for a in message.get("author", []):
            given = a.get("given", "").strip()
            family = a.get("family", "").strip()
            name = a.get("name", "").strip()
            if given and family:
                authors.append(f"{given} {family}")
            elif family:
                authors.append(family)
            elif name:
                authors.append(name)

        publisher = message.get("publisher")
        ct = message.get("container-title", [])
        container_title = ct[0] if ct else None
        work_type = message.get("type")

        # Date resolution
        all_years: set[int] = set()
        issued_date: str | None = None
        primary_year: int | None = None

        for key in ("published-print", "published-online", "issued"):
            iss, y = _parse_date_parts(message.get(key))
            if y is not None:
                all_years.add(y)
                if primary_year is None:
                    primary_year = y
            if iss is not None and issued_date is None:
                issued_date = iss

        # Abstract
        raw_abstract = message.get("abstract")
        abstract: str | None = None
        if raw_abstract:
            abstract = _strip_jats_xml(raw_abstract)

        # Retraction
        update_to = message.get("update-to", [])
        is_retracted = any(
            isinstance(u, dict) and u.get("type") == "retraction" for u in update_to
        )
        if title.lower().startswith("retraction:") or title.lower().startswith("retracted:"):
            is_retracted = True

        meta = SourceMetadata(
            title=title or None,
            authors=authors,
            publisher=publisher,
            year=primary_year,
            container_title=container_title,
            work_type=work_type,
            is_retracted=is_retracted,
            url=message.get("URL"),
            abstract_available=bool(abstract),
            abstract=abstract,
        )
        return meta, issued_date, all_years

    def _parse_jalc_work(
        self, payload: dict[str, Any]
    ) -> tuple[SourceMetadata, str | None, set[int]]:
        data = payload.get("data", payload)
        title_list = data.get("title_list", [])
        titles: list[str] = []
        for t in title_list:
            if isinstance(t, dict) and t.get("title"):
                titles.append(t["title"].strip())

        primary_title = titles[0] if titles else ""
        alt_titles = titles[1:] if len(titles) > 1 else []

        authors: list[str] = []
        for c in data.get("creator_list", []):
            if isinstance(c, dict):
                name = c.get("creator") or c.get("name")
                if name:
                    authors.append(name.strip())

        publishers: list[str] = []
        for p in data.get("publisher_list", []):
            if isinstance(p, dict):
                pname = p.get("publisher") or p.get("name")
                if pname:
                    publishers.append(pname.strip())
        publisher = publishers[0] if publishers else None

        pub_date = data.get("publication_date", {})
        all_years: set[int] = set()
        issued_date: str | None = None
        year: int | None = None
        if isinstance(pub_date, dict):
            y = pub_date.get("year")
            m = pub_date.get("month")
            d = pub_date.get("day")
            if y:
                try:
                    year = int(y)
                    all_years.add(year)
                except (ValueError, TypeError):
                    pass
            if y and m and d and year is not None:
                try:
                    cand = f"{year:04d}-{int(m):02d}-{int(d):02d}"
                    date.fromisoformat(cand)
                    issued_date = cand
                except (ValueError, TypeError):
                    pass

        meta = SourceMetadata(
            title=primary_title or None,
            alternate_titles=alt_titles,
            authors=authors,
            publisher=publisher,
            year=year,
            url=data.get("url"),
        )
        return meta, issued_date, all_years

    def _parse_csl_json(
        self, payload: dict[str, Any]
    ) -> tuple[SourceMetadata, str | None, set[int]]:
        title = payload.get("title", "")
        if isinstance(title, list) and title:
            title = title[0]
        title = _clean_space(str(title)) if title else ""

        authors: list[str] = []
        for a in payload.get("author", []):
            if isinstance(a, dict):
                given = a.get("given", "").strip()
                family = a.get("family", "").strip()
                literal = a.get("literal", "").strip()
                if given and family:
                    authors.append(f"{given} {family}")
                elif family:
                    authors.append(family)
                elif literal:
                    authors.append(literal)

        publisher = payload.get("publisher")
        ct = payload.get("container-title")
        container_title = str(ct) if ct else None

        issued_date, year = _parse_date_parts(payload.get("issued"))
        all_years: set[int] = {year} if year is not None else set()

        raw_abstract = payload.get("abstract")
        abstract = _strip_jats_xml(str(raw_abstract)) if raw_abstract else None

        meta = SourceMetadata(
            title=title or None,
            authors=authors,
            publisher=publisher,
            year=year,
            container_title=container_title,
            url=payload.get("URL"),
            abstract_available=bool(abstract),
            abstract=abstract,
        )
        return meta, issued_date, all_years

    def _parse_arxiv_atom(
        self, xml_text: str
    ) -> tuple[SourceMetadata, str | None, set[int], bool]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return SourceMetadata(), None, set(), False

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        entry = root.find("atom:entry", ns)
        if entry is None:
            return SourceMetadata(), None, set(), False

        id_elem = entry.find("atom:id", ns)
        id_text = _clean_space(id_elem.text) if id_elem is not None and id_elem.text else ""
        if "/api/errors" in id_text:
            return SourceMetadata(), None, set(), False

        title_elem = entry.find("atom:title", ns)
        title_text = _clean_space(title_elem.text) if title_elem is not None and title_elem.text else ""
        if not title_text or title_text.lower() == "error":
            return SourceMetadata(), None, set(), False

        authors: list[str] = []
        for author_elem in entry.findall("atom:author", ns):
            name_elem = author_elem.find("atom:name", ns)
            if name_elem is not None and name_elem.text:
                authors.append(_clean_space(name_elem.text))

        pub_elem = entry.find("atom:published", ns)
        pub_text = pub_elem.text.strip() if pub_elem is not None and pub_elem.text else ""
        issued_date: str | None = None
        year: int | None = None
        all_years: set[int] = set()
        if pub_text:
            cand = pub_text[:10]
            try:
                date.fromisoformat(cand)
                issued_date = cand
                year = int(cand[:4])
                all_years.add(year)
            except ValueError:
                pass

        sum_elem = entry.find("atom:summary", ns)
        abstract = _clean_space(sum_elem.text) if sum_elem is not None and sum_elem.text else None

        doi_elem = entry.find("arxiv:doi", ns)
        published_doi = doi_elem.text.strip() if doi_elem is not None and doi_elem.text else None

        meta = SourceMetadata(
            title=title_text or None,
            authors=authors,
            publisher="arXiv",
            year=year,
            url=id_text or None,
            abstract_available=bool(abstract),
            abstract=abstract,
            published_doi=published_doi,
        )
        return meta, issued_date, all_years, True

    def verify_arxiv(
        self,
        target: ClassifiedTarget | str,
        *,
        issued: str | None = None,
        expect_title: str | None = None,
        expect_author: str | None = None,
        expect_year: int | None = None,
        check_claim: list[str] | None = None,
    ) -> VerificationResult:
        classified = (
            target
            if isinstance(target, ClassifiedTarget)
            else classify_target(target)
        )
        bare_id = classified.raw_id
        safe_id = _safe_name(bare_id)
        today = date.today().isoformat()
        retrieved_at = today

        cache_path = self.cache_dir / "arxiv" / f"{safe_id}.xml"
        cached = False
        raw_path: str | None = None
        sha256_digest: str | None = None
        url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(bare_id)}"

        if cache_path.exists() and not self.refresh:
            cached = True
            raw_path = str(cache_path)
            data_bytes = cache_path.read_bytes()
            sha256_digest = f"sha256:{hashlib.sha256(data_bytes).hexdigest()}"
            xml_text = data_bytes.decode("utf-8", errors="replace")
            meta, extracted_issued, all_years, found = self._parse_arxiv_atom(xml_text)
            if not found:
                checks = VerificationChecks(
                    title_match=False if expect_title else None,
                    author_match=False if expect_author else None,
                    year_match=False if expect_year else None,
                )
                return VerificationResult(
                    ok=False,
                    status="not_found",
                    input=target if isinstance(target, str) else target.raw_id,
                    identifier=classified.identifier,
                    identifier_type="arxiv",
                    kind=classified.kind,
                    registration_agency=None,
                    via="arxiv",
                    retrieved_at=retrieved_at,
                    issued=issued or None,
                    metadata=meta,
                    checks=checks,
                    evidence=EvidenceRecord(cached=True),
                    okf_source=None,
                )
        else:
            status, _, body = self._http_request(url, is_ledger=True, interval=3.0)
            if status != 200:
                raise SourceApiError(status, url, body.decode("utf-8", errors="replace"))

            xml_text = body.decode("utf-8", errors="replace")
            meta, extracted_issued, all_years, found = self._parse_arxiv_atom(xml_text)
            if not found:
                checks = VerificationChecks(
                    title_match=False if expect_title else None,
                    author_match=False if expect_author else None,
                    year_match=False if expect_year else None,
                )
                return VerificationResult(
                    ok=False,
                    status="not_found",
                    input=target if isinstance(target, str) else target.raw_id,
                    identifier=classified.identifier,
                    identifier_type="arxiv",
                    kind=classified.kind,
                    registration_agency=None,
                    via="arxiv",
                    retrieved_at=retrieved_at,
                    issued=issued or None,
                    metadata=meta,
                    checks=checks,
                    evidence=EvidenceRecord(cached=False),
                    okf_source=None,
                )

            _atomic_write(cache_path, xml_text)
            raw_path = str(cache_path)
            data_bytes = cache_path.read_bytes()
            sha256_digest = f"sha256:{hashlib.sha256(data_bytes).hexdigest()}"

        effective_issued = issued or extracted_issued

        meta_payload = {
            "identifier": f"arXiv:{bare_id}",
            "identifier_type": "arxiv",
            "kind": classified.kind,
            "url": url,
            "via": "arxiv",
            "retrieved_at": retrieved_at,
            "sha256": sha256_digest,
            "title": meta.title,
            "issued": effective_issued,
        }
        meta_path = cache_path.parent / f"{safe_id}.meta.json"
        _atomic_write(
            meta_path,
            json.dumps(meta_payload, indent=2, ensure_ascii=False),
        )
        checks, status_str, ok = _evaluate_checks(
            meta,
            expect_title=expect_title,
            expect_author=expect_author,
            expect_year=expect_year,
            check_claim=check_claim,
            all_years=all_years,
        )

        warnings: list[str] = []
        if meta.published_doi:
            warnings.append(
                f"published DOI {meta.published_doi} exists; policy prefers Version of Record (paper:{meta.published_doi})"
            )

        evidence = EvidenceRecord(
            cached=cached,
            raw_path=raw_path,
            sha256=sha256_digest,
        )

        res = VerificationResult(
            ok=ok,
            status=status_str,
            input=target if isinstance(target, str) else target.raw_id,
            identifier=classified.identifier,
            identifier_type="arxiv",
            kind=classified.kind,
            registration_agency="arxiv",
            via="arxiv",
            retrieved_at=retrieved_at,
            issued=effective_issued,
            metadata=meta,
            checks=checks,
            evidence=evidence,
            warnings=warnings,
        )
        if ok:
            res.okf_source = res.format_okf_source()
        return res

    def verify_url(
        self,
        target: ClassifiedTarget | str,
        *,
        issued: str | None = None,
        expect_title: str | None = None,
        expect_author: str | None = None,
        expect_year: int | None = None,
        check_claim: list[str] | None = None,
    ) -> VerificationResult:
        classified = (
            target
            if isinstance(target, ClassifiedTarget)
            else classify_target(target)
        )
        url = classified.identifier
        key = snapshot_key_for_url(classified)

        snapshots_dir = self.cache_dir / "snapshots"
        meta_path = snapshots_dir / f"{key}.meta.json"
        today = date.today().isoformat()
        retrieved_at = today

        cached = False
        snapshot_path: str | None = None
        sha256_digest: str | None = None
        title: str | None = None
        extracted_publisher: str | None = None
        extracted_issued: str | None = None
        year: int | None = None
        text: str = ""
        pdf_ok: bool = True
        is_pdf: bool = False
        warnings: list[str] = []

        if meta_path.exists() and not self.refresh:
            cached = True
            meta_json = json.loads(meta_path.read_text("utf-8"))
            sha256_digest = meta_json.get("sha256")
            title = meta_json.get("title")
            extracted_publisher = meta_json.get("extracted_publisher")
            extracted_issued = meta_json.get("extracted_issued")
            retrieved_at = meta_json.get("retrieved_at", today)
            if extracted_issued:
                try:
                    year = int(extracted_issued[:4])
                except (ValueError, TypeError):
                    pass

            # Find companion file (excluding .meta.json, .extracted.txt, and .tmp)
            companions = [
                p
                for p in snapshots_dir.glob(f"{key}.*")
                if not p.name.endswith(".meta.json")
                and not p.name.endswith(".extracted.txt")
                and not p.name.endswith(".tmp")
            ]
            if not companions:
                raise SourceVerifyError(f"Missing companion snapshot body for {meta_path}")
            body_file = companions[0]
            snapshot_path = str(body_file)

            extracted_path = snapshots_dir / f"{key}.extracted.txt"
            if extracted_path.exists():
                text = extracted_path.read_text("utf-8")
                is_pdf = (body_file.suffix.lower() == ".pdf")
                ext_name = meta_json.get("extractor")
                if ext_name in ("ascii_fallback", "pypdf_error"):
                    pdf_ok = False
                else:
                    pdf_ok = True
            else:
                body_bytes = body_file.read_bytes()
                if body_file.suffix.lower() == ".pdf":
                    is_pdf = True
                    text, pdf_ok, *_ = _extract_pdf_text(body_bytes)
                else:
                    parser = _HtmlSnapshotParser()
                    parser.feed(body_bytes.decode("utf-8", errors="replace"))
                    text = parser.get_visible_text()
        else:
            status, resp_headers, body = self._http_request(
                url,
                headers={"Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"},
                is_ledger=False,
                interval=1.5,
            )
            if len(body) > MAX_SNAPSHOT_BYTES:
                raise SourceVerifyError(
                    f"Snapshot exceeds size limit of {MAX_SNAPSHOT_BYTES} bytes ({len(body)} bytes)"
                )

            if status in (401, 403):
                checks = VerificationChecks()
                return VerificationResult(
                    ok=False,
                    status="unverified",
                    input=target if isinstance(target, str) else target.raw_id,
                    identifier=url,
                    identifier_type="url",
                    kind=classified.kind,
                    registration_agency=None,
                    via="web",
                    retrieved_at=today,
                    issued=issued or None,
                    metadata=SourceMetadata(),
                    checks=checks,
                    evidence=EvidenceRecord(cached=False),
                    error=f"HTTP {status}: paywall or access restricted",
                )

            if status == 404:
                checks = VerificationChecks(
                    title_match=False if expect_title else None,
                    author_match=False if expect_author else None,
                    year_match=False if expect_year else None,
                )
                return VerificationResult(
                    ok=False,
                    status="not_found",
                    input=target if isinstance(target, str) else target.raw_id,
                    identifier=url,
                    identifier_type="url",
                    kind=classified.kind,
                    registration_agency=None,
                    via="web",
                    retrieved_at=today,
                    issued=issued or None,
                    metadata=SourceMetadata(),
                    checks=checks,
                    evidence=EvidenceRecord(cached=False),
                )

            if status != 200:
                raise SourceApiError(status, url, body.decode("utf-8", errors="replace"))

            content_type = resp_headers.get("Content-Type", "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                ext = "pdf"
            elif "html" in content_type or "xml" in content_type:
                ext = "html"
            elif "text/plain" in content_type:
                ext = "txt"
            else:
                ext = "html" if b"<html" in body.lower() else "bin"

            sha256_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            if ext == "pdf":
                is_pdf = True
                extracted_publisher = None
                extracted_issued = None
                title = None
                year = None
                text, pdf_ok, extractor = _extract_pdf_text(body)
            else:
                parser = _HtmlSnapshotParser()
                parser.feed(body.decode("utf-8", errors="replace"))
                title = parser.get_title()
                extracted_publisher = parser.get_publisher()
                extracted_issued, year = parser.get_date()
                text = parser.get_visible_text()
                extractor = "html.parser"

            body_path = snapshots_dir / f"{key}.{ext}"
            _atomic_write_bytes(body_path, body)
            snapshot_path = str(body_path)

            extracted_path = snapshots_dir / f"{key}.extracted.txt"
            _atomic_write_bytes(extracted_path, text)
            extracted_bytes = text.encode("utf-8")
            extracted_sha256 = f"sha256:{hashlib.sha256(extracted_bytes).hexdigest()}"

            meta_payload = {
                "url": url,
                "final_url": self._last_final_url or url,
                "extractor": extractor,
                "extracted_sha256": extracted_sha256,
                "status": status,
                "content_type": content_type or "application/octet-stream",
                "content_length": len(body),
                "retrieved_at": today,
                "timestamp": now_iso,
                "sha256": sha256_digest,
                "title": title,
                "extracted_publisher": extracted_publisher,
                "extracted_issued": extracted_issued,
            }
            _atomic_write_bytes(
                meta_path,
                json.dumps(meta_payload, indent=2, ensure_ascii=False),
            )

        meta = SourceMetadata(
            title=title,
            publisher=extracted_publisher,
            year=year,
            url=url,
            abstract_available=bool(text),
            abstract=text,
        )

        effective_issued = issued or extracted_issued
        all_years = {year} if year is not None else set()
        if effective_issued and len(effective_issued) >= 4:
            try:
                all_years.add(int(effective_issued[:4]))
            except ValueError:
                pass

        checks, status_str, ok = _evaluate_checks(
            meta,
            expect_title=expect_title,
            expect_author=expect_author,
            expect_year=expect_year,
            check_claim=check_claim,
            all_years=all_years,
            text_to_check=text,
        )

        if is_pdf and check_claim and not pdf_ok:
            warnings.append(
                "PDF text extraction requires pypdf extra (uv run --extra pdf ...)"
            )

        evidence = EvidenceRecord(
            cached=cached,
            snapshot_path=snapshot_path,
            meta_path=str(meta_path),
            sha256=sha256_digest,
        )

        res = VerificationResult(
            ok=ok,
            status=status_str,
            input=target if isinstance(target, str) else target.raw_id,
            identifier=url,
            identifier_type="url",
            kind=classified.kind,
            registration_agency=None,
            via="web",
            retrieved_at=retrieved_at,
            issued=effective_issued,
            metadata=meta,
            checks=checks,
            evidence=evidence,
            warnings=warnings,
        )
        if ok:
            res.okf_source = res.format_okf_source()
        return res


