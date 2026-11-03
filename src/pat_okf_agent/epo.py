"""Shared access layer for EPO patent data.

Two independent EPO services are wrapped here so that every script uses the same
credential handling, rate limiting, cache layout and response parsing:

``EPO OPS 3.2``
    OAuth2 client-credentials API for worldwide bibliographic search,
    publication data, families, full text, legal status and the EP Register.
    Requires a free OPS account. Credentials are read from
    ``.secrets/ops_credentials.json`` or ``EPO_OPS_*`` environment variables.

``EPO European Publication Server (EPS) REST v1.2``
    Unauthenticated, officially published EP A/B documents (XML/HTML/PDF).
    Used for authoritative claim text; no account is required.

The module is deliberately usable both as a library and from the command line::

    python -m pat_okf_agent.epo check
    python -m pat_okf_agent.epo search 'pa="EXAMPLE CORP" and ti="alloy"'
    python -m pat_okf_agent.epo biblio WO9999999A1
    python -m pat_okf_agent.epo eps-claims EP9999999B1

Fair-use defaults: one token per 20 minutes, at least 3 s between OPS requests,
all successful responses cached on disk, and no credentials in tracked files.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass, field

try:  # pragma: no cover - availability is platform dependent
    import fcntl
except ImportError:  # non-POSIX: cross-process coordination is disabled
    fcntl = None  # type: ignore[assignment]
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CREDENTIALS = ROOT / ".secrets" / "ops_credentials.json"
DEFAULT_TOKEN_CACHE = ROOT / ".secrets" / "ops_token_cache.json"
DEFAULT_OPS_CACHE = ROOT / "data" / "external" / "ops_raw"
DEFAULT_EPS_CACHE = ROOT / "data" / "external" / "eps_raw"
DEFAULT_API_ROOT = "https://ops.epo.org/3.2/rest-services"
DEFAULT_TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
LEGACY_TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
EPS_ROOT = "https://data.epo.org/publication-server/rest/v1.2"
DEFAULT_USER_AGENT = (
    f"pat-okf-agent/{__version__} (research; non-commercial; "
    "EPO OPS free plan; cache-first)"
)
DEFAULT_SLEEP_S = 3.0
DEFAULT_EPS_SLEEP_S = 0.5

# Free-plan fair-use guards ---------------------------------------------------
# The OPS free tier rejects large result windows well before 100; 25 is the
# documented practical window (docs/epo_api.md, section 4).
MAX_SEARCH_RESULTS = 25
# Cross-process rate-limit state: one shared file stores the timestamp of the
# last OPS request so that several scripts running at the same time keep the
# minimum spacing. Override with EPO_OPS_RATE_STATE; set it to "" to disable.
DEFAULT_RATE_STATE = Path(
    os.environ.get("EPO_OPS_RATE_STATE")
    or ROOT / "data" / "external" / "ops_rate_state.json"
)


# ---------------------------------------------------------------------------
# Errors and small helpers
# ---------------------------------------------------------------------------


class EpoError(Exception):
    """Base class for EPO client errors."""


class EpoCredentialsError(EpoError):
    """OPS credentials are missing or unusable."""


class EpoOfflineError(EpoError):
    """A network request was required but the client is in offline mode."""


class EpoApiError(EpoError):
    """An EPO endpoint returned an error or an unparsable response."""

    def __init__(self, message: str, *, status: int | None = None,
                 url: str | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> str | None:
    """Return the ``$`` text of an OPS/EPO exchange node, recursively if needed."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("$"), str):
            return value["$"]
        for key in ("name", "text", "doc-number", "country", "kind", "date"):
            if key in value:
                got = _text(value[key])
                if got:
                    return got
    return None


def _flatten_text(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        if isinstance(value.get("$"), str):
            out.append(value["$"])
        for key, v in value.items():
            if key != "$":
                out.extend(_flatten_text(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_flatten_text(v))
    return out


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def walk(node: Any, key: str = "ops:publication-reference") -> list[Any]:
    """Recursively collect values stored under a (namespaced) tag name."""
    out: list[Any] = []
    if isinstance(node, Mapping):
        if key in node:
            out.append(node[key])
        for value in node.values():
            out.extend(walk(value, key))
    elif isinstance(node, list):
        for value in node:
            out.extend(walk(value, key))
    return out


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return name or hashlib.sha1(name.encode()).hexdigest()[:12]


def _atomic_write(path: Path, data: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Credentials and token handling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpsCredentials:
    """Resolved OPS credentials (never includes a bearer token)."""

    consumer_key: str
    consumer_secret: str
    token_url: str = DEFAULT_TOKEN_URL
    api_root: str = DEFAULT_API_ROOT
    source: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None,
             environ: Mapping[str, str] | None = None) -> "OpsCredentials":
        env = os.environ if environ is None else environ
        configured = path or env.get("EPO_OPS_CREDENTIALS") or DEFAULT_CREDENTIALS
        path_obj = Path(configured)
        raw: dict[str, Any] = {}
        if path_obj.exists():
            try:
                raw = json.loads(path_obj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EpoCredentialsError(
                    f"cannot read OPS credentials at {path_obj}: {exc}"
                ) from exc

        key = (env.get("EPO_OPS_CONSUMER_KEY")
               or raw.get("consumer_key") or raw.get("key"))
        secret = (env.get("EPO_OPS_CONSUMER_SECRET")
                  or raw.get("consumer_secret") or raw.get("secret"))
        if not key or not secret:
            raise EpoCredentialsError(
                "EPO OPS credentials not found. Create .secrets/ops_credentials.json "
                'with {"consumer_key": "...", "consumer_secret": "..."} or set '
                "EPO_OPS_CONSUMER_KEY / EPO_OPS_CONSUMER_SECRET. "
                "See docs/epo_api.md."
            )
        return cls(
            consumer_key=str(key),
            consumer_secret=str(secret),
            token_url=str(env.get("EPO_OPS_TOKEN_URL")
                          or raw.get("token_url") or DEFAULT_TOKEN_URL),
            api_root=str(env.get("EPO_OPS_API_ROOT")
                         or raw.get("api_root") or DEFAULT_API_ROOT),
            source=str(path_obj if path_obj.exists() else "environment"),
        )

    @property
    def token_urls(self) -> list[str]:
        """Configured token URL plus the legacy endpoint as a fallback.

        The configured URL (often ``https://auth.epo.org/oauth/token``) can be
        unreachable from some networks; the legacy ``/3.2/auth/accesstoken``
        endpoint has remained usable and is therefore tried second.
        """
        urls = [self.token_url]
        if self.token_url.rstrip("/") != LEGACY_TOKEN_URL.rstrip("/"):
            urls.append(LEGACY_TOKEN_URL)
        return urls

    def fingerprint(self) -> str:
        """Short non-reversible identifier used to avoid cross-key token reuse."""
        digest = hashlib.sha256(self.consumer_key.encode()).hexdigest()
        return digest[:12]


class RateLimiter:
    """Minimum interval between network requests.

    In-process spacing uses a monotonic clock. When ``state_path`` is given
    (and ``fcntl`` is available), the same interval is enforced *across
    processes*: the timestamp of the last OPS request is stored in a small
    file and requests serialize on an exclusive lock, so running several
    scripts in parallel never removes the spacing. Coordination is best
    effort and never raises.
    """

    def __init__(self, min_interval: float = DEFAULT_SLEEP_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep,
                 state_path: str | Path | bool | None = None,
                 wall: Callable[[], float] = time.time):
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleeper
        self._last: float | None = None
        if state_path and fcntl is not None:
            self.state_path = Path(state_path)
        else:
            self.state_path = None
        self._wall = wall

    def _wait_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.state_path, os.O_RDWR | os.O_CREAT)
            with os.fdopen(descriptor, "r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    last = 0.0
                    raw = handle.read().strip()
                    if raw:
                        last = float(raw)
                    # Clamp so a skewed clock cannot stall the caller.
                    delay = min(self.min_interval - (self._wall() - last),
                                self.min_interval)
                    if delay > 0:
                        # Sleep while holding the lock: this serializes
                        # concurrent processes and preserves the interval.
                        self._sleep(delay)
                    handle.seek(0)
                    handle.truncate()
                    handle.write(f"{self._wall():.6f}\n")
                    handle.flush()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            return

    def wait(self) -> None:
        if self.state_path is not None:
            self._wait_state()
            return
        if self._last is not None:
            delay = self.min_interval - (self._clock() - self._last)
            if delay > 0:
                self._sleep(delay)
        self._last = self._clock()


def _basic_auth(key: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


def _default_transport(request: urllib.request.Request, timeout: float):
    """urllib transport with an explicit keyword timeout argument."""
    return urllib.request.urlopen(request, timeout=timeout)


# ---------------------------------------------------------------------------
# OPS response parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicationRef:
    country: str
    number: str
    kind: str | None = None
    date: str | None = None
    family_id: str | None = None

    @property
    def docdb(self) -> str:
        if self.kind:
            return f"{self.country}.{self.number}.{self.kind}"
        return f"{self.country}.{self.number}"

    @property
    def epodoc(self) -> str:
        return f"{self.country}{self.number}"

    def as_dict(self) -> dict[str, Any]:
        return {"country": self.country, "number": self.number,
                "kind": self.kind, "date": self.date,
                "family_id": self.family_id, "docdb": self.docdb,
                "epodoc": self.epodoc}


@dataclass
class SearchResult:
    query: str
    total_count: int | None
    publications: list[PublicationRef]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def docdbs(self) -> list[str]:
        return [p.docdb for p in self.publications]

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.query, "total_count": self.total_count,
                "publications": [p.as_dict() for p in self.publications]}


def _world(data: Mapping[str, Any]) -> Mapping[str, Any]:
    node = data.get("ops:world-patent-data")
    return node if isinstance(node, Mapping) else data


def _first_docid(ref: Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    docids = _as_list(ref.get("document-id"))
    if not docids:
        return {}, None
    for docid in docids:
        if isinstance(docid, Mapping) and docid.get("@document-id-type") == "docdb":
            return docid, "docdb"
    docid = docids[0]
    return (docid, docid.get("@document-id-type")) if isinstance(docid, Mapping) else ({}, None)


def publication_from_ref(ref: Mapping[str, Any]) -> PublicationRef | None:
    """Convert an OPS ``publication-reference``/family member into a ref."""
    docid, _ = _first_docid(ref)
    country = _text(docid.get("country"))
    number = _text(docid.get("doc-number"))
    if not country or not number:
        return None
    return PublicationRef(
        country=country.upper(),
        number=number,
        kind=_text(docid.get("kind")),
        date=_text(docid.get("date")),
        family_id=ref.get("@family-id"),
    )


def _search_block(world: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("ops:biblio-search", "ops:search-result", "ops:patent-family"):
        block = world.get(key)
        if isinstance(block, Mapping):
            return block
    return world


def parse_search(data: Mapping[str, Any], query: str = "") -> SearchResult:
    """Parse an OPS search/family response into a stable result object."""
    if not isinstance(data, Mapping):
        raise EpoApiError("OPS search response is not a JSON object")
    world = _world(data)
    block = _search_block(world)

    total: int | None = None
    for node in (block, world):
        if isinstance(node, Mapping) and node.get("@total-result-count") is not None:
            try:
                total = int(str(node["@total-result-count"]))
            except ValueError:
                total = None
            break

    containers: list[Mapping[str, Any]] = [block, world]
    results: list[Mapping[str, Any]] = []
    for container in containers:
        sr = container.get("ops:search-result")
        if isinstance(sr, Mapping):
            results.extend(_as_list(sr.get("ops:publication-reference")))
        results.extend(_as_list(container.get("ops:publication-reference")))
        results.extend(_as_list(container.get("ops:family-member")))
    pubs: list[PublicationRef] = []
    seen: set[str] = set()
    for ref in results:
        if not isinstance(ref, Mapping):
            continue
        pub = publication_from_ref(ref)
        if pub and pub.docdb not in seen:
            seen.add(pub.docdb)
            pubs.append(pub)
    return SearchResult(query=query, total_count=total, publications=pubs, raw=dict(data))


def _party_names(parties: Mapping[str, Any], group: str, item: str) -> list[str]:
    container = parties.get(group) if isinstance(parties, Mapping) else None
    if not isinstance(container, Mapping):
        return []
    names: list[str] = []
    for entry in _as_list(container.get(item)):
        if not isinstance(entry, Mapping):
            continue
        raw = entry.get(f"{item}-name") or entry.get("name")
        name = _text(raw)
        if name:
            names.append(_clean_space(name))
    return list(dict.fromkeys(names))


def parse_biblio(data: Mapping[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    """Normalise an OPS ``biblio`` response.

    Backwards-compatible keys used by the original iteration scripts:
    ``title``, ``applicants``, ``inventors``, ``priority``. Extra keys are
    ``family_id``, ``country``, ``number``, ``kind`` and ``publication_date``.
    """
    if not isinstance(data, Mapping):
        return {}
    world = _world(data)
    docs = _as_list(world.get("exchange-documents"))
    ex: Mapping[str, Any] = {}
    if docs:
        first = docs[0]
        if isinstance(first, Mapping):
            exs = _as_list(first.get("exchange-document"))
            ex = exs[0] if exs and isinstance(exs[0], Mapping) else first
    if not ex:
        return {}

    bd = ex.get("bibliographic-data")
    bd = bd if isinstance(bd, Mapping) else {}
    title_node = bd.get("invention-title") or ex.get("invention-title")
    titles = [t for t in (_text(x) for x in _as_list(title_node)) if t]
    if titles:
        # Prefer English when multiple languages are returned.
        english = [t for x in _as_list(title_node) if isinstance(x, Mapping)
                   and str(x.get("@lang", "")).lower() == "en"
                   for t in [_text(x)] if t]
        title = (english or titles)[0]
    else:
        title = ""

    parties = bd.get("parties") or ex.get("parties")
    parties = parties if isinstance(parties, Mapping) else {}
    pub_ref = bd.get("publication-reference") or ex.get("publication-reference")
    pub_ref = pub_ref if isinstance(pub_ref, Mapping) else {}
    pub_docid, _ = _first_docid(pub_ref)
    country = _text(pub_docid.get("country")) or ex.get("@country")
    number = _text(pub_docid.get("doc-number")) or ex.get("@doc-number")
    kind = _text(pub_docid.get("kind")) or ex.get("@kind")
    pub_date = _text(pub_docid.get("date"))

    priority_ids: list[str] = []
    prio = bd.get("priority-claims") or bd.get("priority-claim")
    if isinstance(prio, Mapping):
        prio = prio.get("priority-claim") or prio
    for claim in _as_list(prio):
        if not isinstance(claim, Mapping):
            continue
        docid, _ = _first_docid(claim)
        cc = _text(docid.get("country"))
        num = _text(docid.get("doc-number"))
        kd = _text(docid.get("kind"))
        dt = _text(docid.get("date"))
        if num:
            priority_ids.append("".join(x for x in (cc, num, kd, dt) if x))

    rec: dict[str, Any] = {
        "title": title,
        "applicants": _party_names(parties, "applicants", "applicant"),
        "inventors": _party_names(parties, "inventors", "inventor"),
        "priority": "; ".join(dict.fromkeys(priority_ids))[:500],
        "family_id": ex.get("@family-id"),
        "country": country,
        "number": number,
        "kind": kind,
        "publication_date": pub_date,
    }
    if include_raw:
        rec["raw"] = data
    return rec


def parse_family(data: Mapping[str, Any]) -> list[PublicationRef]:
    """Return the publication references of an OPS patent-family response."""
    if not isinstance(data, Mapping):
        return []
    world = _world(data)
    family = world.get("ops:patent-family")
    family = family if isinstance(family, Mapping) else world
    refs: list[Mapping[str, Any]] = []
    for member in _as_list(family.get("ops:family-member")):
        if not isinstance(member, Mapping):
            continue
        for pr in _as_list(member.get("publication-reference")):
            if isinstance(pr, Mapping):
                refs.append(pr)
    if not refs:
        for ref in _as_list(family.get("ops:publication-reference")):
            if isinstance(ref, Mapping):
                refs.append(ref)
    out: list[PublicationRef] = []
    seen: set[str] = set()
    for ref in refs:
        pub = publication_from_ref(ref)
        if pub and pub.docdb not in seen:
            seen.add(pub.docdb)
            out.append(pub)
    return out


def parse_ops_claims(data: Mapping[str, Any]) -> list[str]:
    """Extract claim texts from an OPS ``claims``/``fulltext`` JSON response."""
    if not isinstance(data, Mapping):
        return []
    world = _world(data)

    def claims_from_node(claims_node: Any) -> list[str]:
        out: list[str] = []
        for container in _as_list(claims_node):
            if not isinstance(container, Mapping):
                continue
            claim_nodes = container.get("claim")
            if claim_nodes is not None:
                for claim in _as_list(claim_nodes):
                    if not isinstance(claim, Mapping):
                        continue
                    fragments = _as_list(claim.get("claim-text"))
                    if fragments:
                        # OPS sometimes flattens every claim into one <claim>
                        # node, with one <claim-text> per claim. Start a new
                        # claim when a fragment begins with a claim number;
                        # otherwise treat it as a continuation.
                        claim_out: list[str] = []
                        for fragment in fragments:
                            text = _clean_space(" ".join(_flatten_text(fragment)))
                            if not text:
                                continue
                            if not claim_out or re.match(r"^\d+\s*[.)]", text):
                                claim_out.append(text)
                            else:
                                claim_out[-1] += " " + text
                        out.extend(claim_out)
                    else:
                        texts = _flatten_text(claim)
                        if texts:
                            out.append(_clean_space(" ".join(texts)))
            else:
                # Some responses expose claim-text directly under <claims>.
                texts = _flatten_text(container)
                if texts:
                    out.append(_clean_space(" ".join(texts)))
        return out

    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key.rsplit(":", 1)[-1] == "claims":
                    found.extend(claims_from_node(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(world)
    return list(dict.fromkeys(c for c in found if c))


# ---------------------------------------------------------------------------
# OPS client
# ---------------------------------------------------------------------------


class OpsClient:
    """Cache-first EPO OPS 3.2 client with token refresh and fair-use spacing."""

    def __init__(
        self,
        credentials: OpsCredentials | Mapping[str, Any] | None = None,
        *,
        credentials_path: str | Path | None = None,
        token_cache: str | Path | None = None,
        cache_dir: str | Path | None = None,
        sleep_s: float = DEFAULT_SLEEP_S,
        timeout: float = 90.0,
        offline: bool = False,
        transports: Callable[[urllib.request.Request, float], Any] | None = None,
        limiter: RateLimiter | None = None,
        state_path: str | Path | bool | None = DEFAULT_RATE_STATE,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        user_agent: str | None = None,
    ):
        if user_agent is None:
            user_agent = os.environ.get("EPO_OPS_USER_AGENT") or DEFAULT_USER_AGENT
        if credentials is None:
            credentials = OpsCredentials.load(credentials_path)
        elif isinstance(credentials, Mapping):
            credentials = OpsCredentials(
                consumer_key=str(credentials.get("consumer_key")
                                 or credentials.get("key") or ""),
                consumer_secret=str(credentials.get("consumer_secret")
                                    or credentials.get("secret") or ""),
                token_url=str(credentials.get("token_url") or DEFAULT_TOKEN_URL),
                api_root=str(credentials.get("api_root") or DEFAULT_API_ROOT),
                source=str(credentials.get("source") or "mapping"),
            )
        if not credentials.consumer_key or not credentials.consumer_secret:
            raise EpoCredentialsError("consumer_key and consumer_secret are required")

        self.credentials = credentials
        self.token_cache = Path(token_cache or DEFAULT_TOKEN_CACHE)
        self.cache_dir = Path(cache_dir or DEFAULT_OPS_CACHE)
        self.timeout = float(timeout)
        self.offline = bool(offline)
        self.user_agent = user_agent
        self._transport = transports or _default_transport
        self._limiter = limiter or RateLimiter(
            sleep_s, clock=time.monotonic, sleeper=sleeper,
            state_path=state_path)
        self._clock = clock
        self._sleep = sleeper
        self._token: str | None = None

    # -- token -------------------------------------------------------------

    def _read_token_cache(self) -> str | None:
        if not self.token_cache.exists():
            return None
        try:
            cached = json.loads(self.token_cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = cached.get("access_token")
        expires = float(cached.get("expires_at") or 0)
        fingerprint = cached.get("credential_fingerprint")
        if token and self._clock() < expires - 60:
            if not fingerprint or fingerprint == self.credentials.fingerprint():
                return str(token)
        return None

    def _write_token_cache(self, token: str, expires_in: int,
                           product_list: Any = None) -> None:
        payload = {
            "access_token": token,
            "expires_at": self._clock() + int(expires_in),
            "api_product_list": product_list,
            "credential_fingerprint": self.credentials.fingerprint(),
        }
        _atomic_write(self.token_cache, json.dumps(payload), private=True)

    def get_token(self, *, force: bool = False) -> str:
        """Return a cached bearer token, refreshing it when needed."""
        if self._token and not force:
            return self._token
        if not force:
            cached = self._read_token_cache()
            if cached:
                self._token = cached
                return cached
        if self.offline:
            raise EpoOfflineError("OPS token required but client is offline")
        errors: list[str] = []
        for url in self.credentials.token_urls:
            try:
                token, expires_in, products = self._request_token(url)
            except (urllib.error.URLError, TimeoutError, OSError,
                    EpoApiError, EpoCredentialsError) as exc:
                errors.append(f"{url}: {exc}")
                continue
            self._token = token
            self._write_token_cache(token, expires_in, products)
            return token
        raise EpoCredentialsError(
            "could not obtain an OPS access token; tried "
            + " | ".join(errors)
        )

    def _request_token(self, url: str) -> tuple[str, int, Any]:
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": self.user_agent,
                "Authorization": _basic_auth(self.credentials.consumer_key,
                                             self.credentials.consumer_secret),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with self._transport(req, self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            with closing(exc):
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise EpoCredentialsError(
                    f"HTTP {exc.code}: {detail}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EpoApiError("token endpoint returned non-JSON", status=200,
                              url=url, body=body[:500]) from exc
        token = payload.get("access_token")
        if not token:
            raise EpoCredentialsError(
                f"token response has no access_token: {str(payload)[:300]}"
            )
        return str(token), int(payload.get("expires_in") or 1199), payload.get(
            "api_product_list")

    def invalidate_token(self) -> None:
        self._token = None
        try:
            self.token_cache.unlink()
        except OSError:
            pass

    # -- generic requests --------------------------------------------------

    def _cache_path(self, name: str | None, url: str) -> Path:
        key = _safe_name(name) if name else hashlib.sha1(url.encode()).hexdigest()[:20]
        return self.cache_dir / f"{key}.json"

    def _write_error(self, name: str | None, url: str, status: int,
                     body: str) -> None:
        key = _safe_name(name) if name else hashlib.sha1(url.encode()).hexdigest()[:20]
        try:
            _atomic_write(self.cache_dir / f"{key}.error.txt",
                          f"URL: {url}\nSTATUS: {status}\n\n{body}")
        except OSError:
            pass

    def _decode(self, body: bytes, parse: str, url: str) -> Any:
        text = body.decode("utf-8", errors="replace")
        if parse == "text":
            return text
        if parse == "bytes":
            return body
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise EpoApiError("EPO OPS returned non-JSON", status=200,
                              url=url, body=text[:1000]) from exc

    def get_url(
        self,
        url: str,
        *,
        name: str | None = None,
        accept: str = "application/json",
        parse: str = "json",
        refresh: bool = False,
        cache: bool = True,
        auth: bool = True,
        retries: int = 2,
    ) -> Any:
        """GET an absolute URL, using the on-disk cache unless ``refresh``."""
        path = self._cache_path(name, url)
        if cache and not refresh and path.exists():
            try:
                return self._decode(path.read_bytes(), parse, url)
            except EpoApiError:
                # Legacy iterations cached HTTP-error bodies (often XML) under
                # *.json. Treat an unparsable cache entry as a miss.
                pass
        if self.offline:
            raise EpoOfflineError(
                f"no cached response for {name or url}; offline mode is active"
            )

        last_error: Exception | None = None
        force_token = False
        for attempt in range(retries + 1):
            headers = {"User-Agent": self.user_agent, "Accept": accept}
            if auth:
                headers["Authorization"] = "Bearer " + self.get_token(force=force_token)
            req = urllib.request.Request(url, headers=headers)
            self._limiter.wait()
            try:
                with self._transport(req, self.timeout) as response:
                    body = response.read()
                    status = getattr(response, "status", 200)
            except urllib.error.HTTPError as exc:
                with closing(exc):
                    body = exc.read()
                    status = exc.code
                    text = body.decode("utf-8", errors="replace")
                    if status == 401 and auth and not force_token:
                        force_token = True
                        self.invalidate_token()
                        continue
                    if (status in (429, 500, 502, 503, 504)
                            and attempt < retries):
                        retry_after = 0.0
                        try:
                            retry_after = float(
                                (exc.headers or {}).get("Retry-After", 0))
                        except (TypeError, ValueError):
                            pass
                        self._sleep(max(retry_after, 2.0 * (attempt + 1)))
                        last_error = EpoApiError(
                            f"HTTP {status}", status=status, url=url,
                            body=text[:1000])
                        continue
                    self._write_error(name, url, status, text)
                    raise EpoApiError(
                        f"EPO OPS HTTP {status} for {url}",
                        status=status, url=url, body=text[:1000]) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                raise EpoApiError(f"EPO OPS request failed for {url}: {exc}",
                                  url=url) from exc

            if status >= 400:
                text = body.decode("utf-8", errors="replace")
                self._write_error(name, url, status, text)
                raise EpoApiError(f"EPO OPS HTTP {status} for {url}",
                                  status=status, url=url, body=text[:1000])
            if cache:
                _atomic_write(path, body.decode("utf-8", errors="replace"))
            return self._decode(body, parse, url)

        raise EpoApiError(f"EPO OPS request failed for {url}: {last_error}",
                          url=url)

    def get_json(self, path: str, *, name: str | None = None,
                 params: Mapping[str, Any] | None = None,
                 refresh: bool = False, cache: bool = True,
                 auth: bool = True) -> Any:
        """GET a path relative to the configured OPS REST root."""
        url = path if "://" in path else self.credentials.api_root.rstrip("/") + "/" + path.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return self.get_url(url, name=name, refresh=refresh, cache=cache, auth=auth)

    def get_json_url(self, url: str, *, name: str | None = None,
                     refresh: bool = False, cache: bool = True) -> Any:
        return self.get_url(url, name=name, refresh=refresh, cache=cache)

    # -- convenience methods ----------------------------------------------

    def search(self, cql: str, *, begin: int = 1, end: int = 25,
               name: str | None = None, refresh: bool = False) -> SearchResult:
        """Run a CQL bibliographic search (25 results is the free-plan window)."""
        if not 1 <= begin <= end <= MAX_SEARCH_RESULTS:
            raise ValueError(
                "range must satisfy 1 <= begin <= end <= "
                f"{MAX_SEARCH_RESULTS} (free-plan window)")
        params = {"q": cql, "Range": f"{begin}-{end}"}
        cache_name = name or f"search_{hashlib.sha1(cql.encode()).hexdigest()[:12]}"
        data = self.get_json("/published-data/search", name=cache_name,
                             params=params, refresh=refresh)
        return parse_search(data, query=cql)

    def biblio(self, publication: str, *, name: str | None = None,
               refresh: bool = False, include_raw: bool = False) -> dict[str, Any]:
        docdb = normalize_docdb(publication)
        cache_name = name or f"biblio_{docdb.replace('.', '_')}"
        data = self.get_json(
            f"/published-data/publication/docdb/{docdb}/biblio",
            name=cache_name, refresh=refresh)
        return parse_biblio(data, include_raw=include_raw)

    def family(self, publication: str, *, name: str | None = None,
               refresh: bool = False) -> list[PublicationRef]:
        docdb = normalize_docdb(publication)
        cache_name = name or f"family_{docdb.replace('.', '_')}"
        data = self.get_json(
            f"/family/publication/docdb/{docdb}",
            name=cache_name, refresh=refresh)
        return parse_family(data)

    def fulltext(self, publication: str, *, section: str | None = None,
                 name: str | None = None, refresh: bool = False) -> Any:
        """Fetch OPS full text, or one section (``claims``/``description``)."""
        docdb = normalize_docdb(publication)
        suffix = f"/{section}" if section else "/fulltext"
        cache_name = name or (
            f"fulltext_{docdb.replace('.', '_')}"
            + (f"_{section}" if section else "")
        )
        return self.get_json(
            f"/published-data/publication/docdb/{docdb}{suffix}",
            name=cache_name, refresh=refresh)

    def claims(self, publication: str, *, name: str | None = None,
               refresh: bool = False) -> list[str]:
        docdb = normalize_docdb(publication)
        data = self.fulltext(docdb, section="claims", name=name, refresh=refresh)
        return parse_ops_claims(data)

    def register_search(self, query: str, *, begin: int = 1, end: int = 10,
                        name: str | None = None, refresh: bool = False) -> Any:
        """Search the EP Register. Free-plan detail scope is limited (often 404)."""
        if not 1 <= begin <= end <= MAX_SEARCH_RESULTS:
            raise ValueError(
                "range must satisfy 1 <= begin <= end <= "
                f"{MAX_SEARCH_RESULTS} (free-plan window)")
        params = {"q": query, "Range": f"{begin}-{end}"}
        cache_name = name or f"register_search_{hashlib.sha1(query.encode()).hexdigest()[:12]}"
        return self.get_json("/register/search", name=cache_name, params=params,
                             refresh=refresh)

    def register(self, publication: str, section: str = "biblio", *,
                 use_kind: bool = False, name: str | None = None,
                 refresh: bool = False) -> Any:
        """Fetch an EP Register section.

        The Register normally addresses publications without the kind code
        (``EP9999998``); pass ``use_kind=True`` for ``EP.9999998.A1``.
        """
        docdb = normalize_docdb(publication)
        allowed = {"biblio", "events", "procedural-steps", "publication"}
        if section not in allowed:
            raise ValueError(f"section must be one of {sorted(allowed)}")
        country, number, kind = (docdb.split(".") + [None, None])[:3]
        target = docdb if use_kind and kind else f"{country}{number}"
        cache_name = name or f"register_{target.replace('.', '_')}_{section}"
        return self.get_json(
            f"/register/patent/{target}/{section}", name=cache_name,
            refresh=refresh)

    def legal(self, publication: str, *, name: str | None = None,
              refresh: bool = False) -> Any:
        docdb = normalize_docdb(publication)
        cache_name = name or f"legal_{docdb.replace('.', '_')}"
        return self.get_json(f"/legal/publication/docdb/{docdb}",
                             name=cache_name, refresh=refresh)


# ---------------------------------------------------------------------------
# Publication Server (EPS) client
# ---------------------------------------------------------------------------

_PUBLICATION_RE = re.compile(
    r"^\s*([A-Za-z]{2})[\s.\-/]*([0-9][0-9A-Za-z]*?)[\s.\-/]*([A-Za-z][0-9]?)?\s*$"
)


def normalize_docdb(publication: str, kind: str | None = None) -> str:
    """Normalise ``EP9999999B1`` / ``EP.9999999.B1`` to ``EP.9999999.B1``.

    A kind supplied explicitly overrides any kind embedded in ``publication``.
    """
    raw = str(publication).strip()
    explicit_kind = kind
    match = _PUBLICATION_RE.match(raw)
    if match:
        country, number, embedded = match.groups()
    else:
        m2 = re.match(r"^\s*([A-Za-z]{2})[\s.\-/]*(.+?)\s*$", raw)
        if not m2:
            raise ValueError(f"cannot parse publication number: {publication!r}")
        country = m2.group(1)
        rest = m2.group(2)
        m3 = re.match(r"^(.*?)[\s.\-/]*([A-Za-z][0-9]?)$", rest)
        if m3 and re.search(r"\d", m3.group(1)):
            number, embedded = m3.group(1), m3.group(2)
        else:
            number, embedded = rest, None
    kind = explicit_kind or embedded
    if explicit_kind and match:
        # Remove a trailing kind from the number when the caller passed both.
        if embedded and number.upper().endswith(embedded.upper()):
            number = number[: -len(embedded)]
    country = country.upper()
    number = re.sub(r"[\s.\-/]+", "", number)
    if not number:
        raise ValueError(f"cannot parse publication number: {publication!r}")
    return f"{country}.{number}.{kind.upper()}" if kind else f"{country}.{number}"


def parse_eps_claims(xml_text: str) -> list[str]:
    """Extract claim texts from an EP publication-server XML document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EpoApiError(f"EPS returned invalid XML: {exc}") from exc
    claims: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) == "claim":
            text = _clean_space("".join(element.itertext()))
            if text:
                claims.append(text)
    if not claims:
        for element in root.iter():
            if _local_name(element.tag) == "claim-text":
                text = _clean_space("".join(element.itertext()))
                if text:
                    claims.append(text)
    return list(dict.fromkeys(claims))


def parse_eps_document(xml_text: str) -> dict[str, Any]:
    """Return document attributes and claims from an EPS XML document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EpoApiError(f"EPS returned invalid XML: {exc}") from exc
    attrib = {_local_name(k): v for k, v in root.attrib.items()}
    return {
        "publication_number": "".join(
            x for x in (attrib.get("country"), attrib.get("doc-number"),
                        attrib.get("kind")) if x),
        "date_publ": attrib.get("date-publ"),
        "status": attrib.get("status"),
        "lang": attrib.get("lang"),
        "claims": parse_eps_claims(xml_text),
    }


class EpsClient:
    """Unauthenticated client for the EPO European Publication Server."""

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        timeout: float = 90.0,
        sleep_s: float = DEFAULT_EPS_SLEEP_S,
        offline: bool = False,
        transports: Callable[[urllib.request.Request, float], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        user_agent: str | None = None,
    ):
        if user_agent is None:
            user_agent = os.environ.get("EPO_OPS_USER_AGENT") or DEFAULT_USER_AGENT
        self.cache_dir = Path(cache_dir or DEFAULT_EPS_CACHE)
        self.timeout = float(timeout)
        self.offline = bool(offline)
        self._transport = transports or _default_transport
        self._limiter = RateLimiter(sleep_s, clock=time.monotonic, sleeper=sleeper)
        self.user_agent = user_agent

    @staticmethod
    def document_url(publication: str, kind: str | None = None,
                     doc_format: str = "document.xml") -> str:
        docdb = normalize_docdb(publication, kind=kind)
        parts = docdb.split(".")
        country, number = parts[0], parts[1]
        kind = parts[2] if len(parts) > 2 else None
        if not kind:
            raise ValueError("EPS requires a kind code, e.g. EP9999999B1")
        return f"{EPS_ROOT}/patents/{country}{number}NW{kind}/{doc_format}"

    def document(self, publication: str, *, kind: str | None = None,
                 doc_format: str = "document.xml", refresh: bool = False,
                 cache: bool = True) -> str:
        """Fetch an official EP document (default: XML)."""
        url = self.document_url(publication, kind=kind, doc_format=doc_format)
        docdb = normalize_docdb(publication, kind=kind)
        suffix = "xml" if doc_format.endswith(".xml") else _safe_name(doc_format)
        path = self.cache_dir / f"{docdb.replace('.', '_')}.{suffix}"
        if cache and not refresh and path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        if self.offline:
            raise EpoOfflineError(f"no cached EPS document for {docdb}")
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                   "Accept": "application/xml"})
        self._limiter.wait()
        try:
            with self._transport(req, self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            with closing(exc):
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise EpoApiError(
                    f"EPS HTTP {exc.code} for {docdb}", status=exc.code,
                    url=url, body=detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EpoApiError(f"EPS request failed for {docdb}: {exc}", url=url) from exc
        if cache:
            _atomic_write(path, body)
        return body

    def exists(self, publication: str, *, kind: str | None = None) -> bool:
        try:
            self.document(publication, kind=kind)
        except EpoApiError as exc:
            if exc.status in (404, 500):
                return False
            raise
        return True

    def claims(self, publication: str, *, kind: str | None = None,
               refresh: bool = False) -> list[str]:
        return parse_eps_claims(self.document(publication, kind=kind,
                                              refresh=refresh))

    def info(self, publication: str, *, kind: str | None = None,
             refresh: bool = False) -> dict[str, Any]:
        return parse_eps_document(self.document(publication, kind=kind,
                                                refresh=refresh))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _make_client(args) -> OpsClient:
    kwargs: dict[str, Any] = {}
    if args.no_rate_state:
        kwargs["state_path"] = False
    return OpsClient(
        credentials_path=args.credentials,
        token_cache=args.token_cache,
        cache_dir=args.cache_dir,
        sleep_s=args.sleep,
        offline=args.offline,
        **kwargs,
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m pat_okf_agent.epo",
        description="Shared EPO OPS / Publication Server client")
    parser.add_argument("--credentials", default=None,
                        help="OPS credentials JSON (default: .secrets/ops_credentials.json)")
    parser.add_argument("--token-cache", default=None,
                        help="OPS token cache path")
    parser.add_argument("--cache-dir", default=None,
                        help="raw OPS response cache directory")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S,
                        help="minimum seconds between OPS requests (default 3)")
    parser.add_argument("--offline", action="store_true",
                        help="use only cached responses")
    parser.add_argument("--no-rate-state", action="store_true",
                        help="disable global (cross-process) request spacing; "
                             "single-user, sequential usage only")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached responses and refetch")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="resolve credentials and cached token without network")
    sub.add_parser("token", help="fetch/refresh an OPS token (one network request)")

    p = sub.add_parser("search", help="CQL bibliographic search")
    p.add_argument("query")
    p.add_argument("--begin", type=int, default=1)
    p.add_argument("--end", type=int, default=25)

    p = sub.add_parser("biblio", help="bibliographic record for one publication")
    p.add_argument("publication")

    p = sub.add_parser("family", help="INPADOC family for one publication")
    p.add_argument("publication")

    p = sub.add_parser("register", help="EP Register search or detail")
    p.add_argument("query", nargs="?")
    p.add_argument("--publication")
    p.add_argument("--section", default="biblio",
                   choices=["biblio", "events", "procedural-steps", "publication"])

    p = sub.add_parser("legal", help="OPS legal-status record")
    p.add_argument("publication")

    p = sub.add_parser("claims", help="claims from OPS full text")
    p.add_argument("publication")

    p = sub.add_parser("eps-document", help="official EPS document (XML)")
    p.add_argument("publication")
    p.add_argument("--kind")
    p.add_argument("--format", default="document.xml", dest="doc_format")

    p = sub.add_parser("eps-claims", help="claims from an official EPS document")
    p.add_argument("publication")
    p.add_argument("--kind")

    p = sub.add_parser("eps-info", help="EPS metadata + claims")
    p.add_argument("publication")
    p.add_argument("--kind")

    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            creds = OpsCredentials.load(args.credentials)
            print(f"credentials: {creds.source}")
            print(f"api_root: {creds.api_root}")
            cache = Path(args.token_cache or DEFAULT_TOKEN_CACHE)
            state = "missing"
            if cache.exists():
                try:
                    data = json.loads(cache.read_text(encoding="utf-8"))
                    state = "valid" if float(data.get("expires_at", 0)) > time.time() else "expired"
                except (OSError, json.JSONDecodeError, ValueError):
                    state = "unreadable"
            print(f"token_cache: {cache} ({state})")
            return 0
        if args.command == "token":
            client = _make_client(args)
            client.get_token(force=True)
            print("OPS token acquired and cached")
            return 0
        if args.command.startswith("eps-"):
            eps = EpsClient(cache_dir=args.cache_dir, offline=args.offline)
            if args.command == "eps-document":
                print(eps.document(args.publication, kind=args.kind,
                                   doc_format=args.doc_format,
                                   refresh=args.refresh))
            elif args.command == "eps-claims":
                _json_print(eps.claims(args.publication, kind=args.kind,
                                       refresh=args.refresh))
            else:
                _json_print(eps.info(args.publication, kind=args.kind,
                                     refresh=args.refresh))
            return 0
        client = _make_client(args)
        if args.command == "search":
            _json_print(client.search(args.query, begin=args.begin, end=args.end,
                                      refresh=args.refresh).as_dict())
        elif args.command == "biblio":
            _json_print(client.biblio(args.publication, refresh=args.refresh,
                                      include_raw=False))
        elif args.command == "family":
            _json_print([p.as_dict() for p in client.family(
                args.publication, refresh=args.refresh)])
        elif args.command == "register":
            if args.publication:
                _json_print(client.register(args.publication, section=args.section,
                                            refresh=args.refresh))
            elif args.query:
                _json_print(client.register_search(args.query, refresh=args.refresh))
            else:
                parser.error("register needs QUERY or --publication")
        elif args.command == "legal":
            _json_print(client.legal(args.publication, refresh=args.refresh))
        elif args.command == "claims":
            _json_print(client.claims(args.publication, refresh=args.refresh))
        return 0
    except EpoError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
