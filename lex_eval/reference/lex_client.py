"""The Worker's research tools, calling the same APIs LexChat's Worker calls.

Three legislation tools against the LEX API, and two case law tools against the
National Archives Find Case Law service. Mirrors
`LexChat/server_py/src/agent/tools/executor.py`, `.../tools/lex.py` and
`.../tools/caselaw.py`: the same endpoints, the same request payloads, the same
response parsing and slimming and the same Phase-2 nudges. The retrieved text is
therefore byte-for-byte what LexChat would have received for the same queries.

Ported, not imported: `LexChat/` is in this repo for reference only and the eval
never imports from it.

Tool results are recorded in full. LexChat summarises anything over a size
threshold, but a reference answer should rest on the primary text.
"""

from __future__ import annotations

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

LEX_API_URL = os.getenv("LEX_API_URL", "https://lex.lab.i.ai.gov.uk").rstrip("/")

# TLS is verified by default, as LexChat's own outbound calls are
# (OUTBOUND_TLS_VERIFY). Behind a TLS-inspecting proxy, supply its CA with
# SSL_CERT_FILE rather than turning this off.
LEX_API_TLS_VERIFY = os.getenv("LEX_API_TLS_VERIFY", "true").strip().lower() == "true"

# LexChat's production values (executor.py), with no research filters active.
SEARCH_LIMIT = 5
SECTION_LIMIT = 10

CASE_LAW_API_URL = os.getenv(
    "CASE_LAW_API_URL", "https://caselaw.nationalarchives.gov.uk"
).rstrip("/")

TOOLS = ("search_legislation", "search_legislation_sections", "get_legislation_text")
CASE_LAW_TOOLS = ("search_case_law", "get_case_law_text")

# Which tools a question in each research mode may be researched with, taken from
# the Worker prompt for that mode (LexChat prompts.py, CITATION PROTOCOL). A
# reference answer researched outside its own brief would expect a response to
# cite a source the response was never allowed to look at.
TOOLS_BY_MODE = {
    "legislation_only": TOOLS,
    "case_law_only": CASE_LAW_TOOLS,
    "legislation_and_case_law": TOOLS + CASE_LAW_TOOLS,
}

_RETRY_STATUS = {429, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 8.0


# ---------------------------------------------------------------------------
# Response shaping, copied from LexChat/server_py/src/agent/tools/lex.py
# ---------------------------------------------------------------------------


def slim_search_results(resp_json: dict) -> dict:
    """Strip a `search_legislation` response to the fields LexChat passes on.

    `description` is excluded upstream on purpose, and `legislation_id` is derived
    from the URI so it can be used directly in a section search.
    """
    slimmed = []
    for item in resp_json.get("results", []):
        uri = item.get("uri", "")
        legislation_id = urlparse(uri).path.lstrip("/") if uri else ""
        if legislation_id.startswith("id/"):
            legislation_id = legislation_id[3:]
        slimmed.append(
            {
                "legislation_id": legislation_id,
                "title": item.get("title", ""),
                "url": uri,
                "status": item.get("status", ""),
                "year": item.get("year"),
                "extent": item.get("extent", []),
            }
        )
    return {"results": slimmed, "total": resp_json.get("total", len(slimmed))}


# LexChat's jurisdiction filter, mirrored. The words the LEX API sends in a
# result's `extent`, mapped to territory codes. "United Kingdom" and "Great
# Britain" are retained by a single-nation filter, because the question the
# filter answers is "does this apply here", not "was this made for here".
_TERRITORY_ALIASES = {
    "england": "E",
    "wales": "W",
    "scotland": "S",
    "northern ireland": "NI",
    "united kingdom": "UK",
    "great britain": "GB",
}

_JURISDICTION_ACCEPTS = {
    "england_and_wales": {"E", "W", "UK", "GB"},
    "scotland": {"S", "UK", "GB"},
    "northern_ireland": {"NI", "UK"},
    "wales": {"W", "UK", "GB"},
    "uk_wide": {"UK"},
}

# Where an instrument's own id says which territory it belongs to, used only
# when the API sent no usable extent, which is common.
_ID_PREFIX_JURISDICTION = {
    "asp": "scotland",
    "ssi": "scotland",
    "nia": "northern_ireland",
    "nisr": "northern_ireland",
    "nisro": "northern_ireland",
    "nisi": "northern_ireland",
    "apni": "northern_ireland",
    "asc": "wales",
    "anaw": "wales",
    "wsi": "wales",
    "mwa": "wales",
}


def _extent_tokens(extent: List[str]) -> set:
    """Normalise an `extent` list to territory codes, dropping blanks."""
    tokens = set()
    for e in extent or []:
        for part in str(e).split("+"):
            part = part.strip()
            if not part:
                continue
            tokens.add(_TERRITORY_ALIASES.get(part.lower(), part.upper()))
    return tokens


def matches_jurisdiction(
    extent: List[str], jurisdiction: str, legislation_id: str = ""
) -> bool:
    """Would LexChat's jurisdiction filter keep a result with this `extent`?

    Ported from `LexChat/server_py/src/agent/tools/lex.py::_matches_jurisdiction`.
    The filter is applied by LexChat to search results after the API returns them,
    not by the API, so anything checking it has to apply the same rule here.

    Nothing in the eval calls this. It is here as the executable mirror of the
    deployed rule. It used to reproduce TOM_TO_DO.md finding 41, a filter that
    split extents on "+" looking for a single letter while the API sent words,
    so a Scotland filter dropped almost every Scottish result. LexChat fixed
    that (FIX_PLAN P1.1, in `main` at c77e779). Track the deployed rule here
    rather than correcting it toward what the API sends.

        >>> matches_jurisdiction(["Scotland"], "scotland")
        True

    One divergence from the deployed rule is worth knowing: an extent whose
    words are none of the recognised six is dropped, not treated as unknown.
    The API sends no such value today, so this is a forward-looking risk
    rather than a live defect.
    """
    accepts = _JURISDICTION_ACCEPTS.get(jurisdiction)
    if accepts is None:
        return True  # unknown filter value: never silently narrow

    tokens = _extent_tokens(extent)
    if tokens:
        return bool(tokens & accepts)

    # Extent unknown from here on.
    if jurisdiction == "uk_wide":
        return False

    owner = _ID_PREFIX_JURISDICTION.get(
        (legislation_id or "").split("/")[0].strip().lower()
    )
    if owner is None:
        return True  # UK-level or unrecognised: could apply anywhere
    if owner == jurisdiction:
        return True
    # England & Wales and Wales overlap: a Welsh instrument is in scope for an
    # England & Wales search, but an English one is not exclusively Welsh.
    return owner == "wales" and jurisdiction == "england_and_wales"


# ---------------------------------------------------------------------------
# Case law parsing, ported from LexChat/server_py/src/agent/tools/caselaw.py
# ---------------------------------------------------------------------------

_ATOM_NS = "http://www.w3.org/2005/Atom"
# The Atom feed's TNA extension elements sit on the bare host namespace, not on
# a /terms/v1 URI; reading the wrong one returns an empty ncn on every result.
_TNA_NS = "https://caselaw.nationalarchives.gov.uk"
# A judgment's data.xml carries its neutral citation in <uk:cite> on the AKN namespace.
_TNA_AKN_NS = "https://caselaw.nationalarchives.gov.uk/akn"
_AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"


def parse_case_law_atom(xml_text: str) -> List[Dict[str, Any]]:
    """Parse a Find Case Law Atom feed into the judgment dicts LexChat passes on."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries = []
    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        title = entry.findtext(f"{{{_ATOM_NS}}}title", "")
        url_el = entry.find(f"{{{_ATOM_NS}}}link[@rel='alternate']")
        if url_el is None:
            url_el = entry.find(f"{{{_ATOM_NS}}}link")
        url = (
            url_el.get("href", "")
            if url_el is not None
            else entry.findtext(f"{{{_ATOM_NS}}}id", "")
        )
        published = entry.findtext(f"{{{_ATOM_NS}}}published", "")
        # <tna:identifier slug="ewca/civ/2025/1671" type="ukncn">[2025] EWCA Civ 1671</...>
        ncn = ""
        court = ""
        for ident in entry.findall(f"{{{_TNA_NS}}}identifier"):
            if ident.get("type") == "ukncn":
                ncn = (ident.text or "").strip()
                slug = ident.get("slug", "")
                court = slug.rsplit("/", 2)[0] if slug else ""
                break
        entries.append(
            {
                "title": title,
                "ncn": ncn,
                "court": court,
                "date": published[:10] if published else "",
                "url": url,
            }
        )
    return entries


def parse_judgment(xml_text: str, url: str) -> Dict[str, Any]:
    """Parse a LegalDocML judgment into the dict `get_case_law_text` returns."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {"url": url, "title": "", "ncn": "", "text": ""}

    parts = []
    for el in root.iter():
        if el.text and el.text.strip():
            parts.append(el.text.strip())
        if el.tail and el.tail.strip():
            parts.append(el.tail.strip())

    ncn = (root.findtext(f".//{{{_TNA_AKN_NS}}}cite") or "").strip()
    title_el = root.find(f".//{{{_AKN_NS}}}FRBRname")
    title = title_el.get("value", "") if title_el is not None else ""
    return {"url": url, "title": title, "ncn": ncn, "text": "\n".join(parts)}


def _extract_ids(resp_json: dict) -> List[Tuple[str, str]]:
    return [
        (item["legislation_id"], item.get("title", ""))
        for item in resp_json.get("results", [])
        if item.get("legislation_id")
    ]


def _phase2_nudge(slimmed: dict) -> str:
    """The `[NEXT STEP: ...]` note LexChat appends after every `search_legislation`."""
    id_pairs = _extract_ids(slimmed)
    if not id_pairs:
        return ""
    lines = "\n".join(f'  - legislation_id: "{i}"  ({t})' for i, t in id_pairs[:5])
    return (
        "\n\n[NEXT STEP: Call search_legislation_sections with the relevant "
        "legislation_id(s) below to retrieve the actual legal text before "
        f"composing your answer:\n{lines}]"
    )


def _case_law_nudge(parsed: dict) -> str:
    """The note LexChat appends after every `search_case_law` (agent_shared.py).

    Its appellate-decisions nudge is deliberately not ported: it rests on a
    party-name matching routine, and nothing here reads the note back.
    """
    total = parsed.get("total", 0)
    if total == 0 and not parsed.get("error"):
        return (
            "\n\n[This search returned 0 results. The National Archives Find Case Law "
            "database does not comprehensively index Scottish Court of Session cases. "
            "If you have already tried 2-3 different queries without results, stop "
            "searching and compose your answer noting that no directly relevant case "
            "law was found in this database.]"
        )
    if total <= 0:
        return ""
    url_lines = "\n".join(
        f'  - url: "{r["url"]}"  ({r.get("title", "")} {r.get("ncn", "")})'
        for r in parsed.get("results", [])[:3]
        if r.get("url")
    )
    return (
        "\n\n[MANDATORY NEXT STEP, DO NOT synthesise yet. Call get_case_law_text for "
        "the 1-3 most relevant cases below to retrieve the full judgment text before "
        f"composing your answer. Pass the exact url field:\n{url_lines}]"
    )


def _section_text(section: dict) -> str:
    return (
        section.get("content")
        or section.get("text")
        or section.get("section_text")
        or ""
    )


def _sections_of(response: Any) -> List[dict]:
    """Normalise a section-search response, which may be a list or a wrapper dict."""
    if isinstance(response, list):
        return [s for s in response if isinstance(s, dict)]
    if isinstance(response, dict):
        items = response.get("sections") or response.get("results") or []
        return [s for s in items if isinstance(s, dict)]
    return []


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


@dataclass
class ApiCall:
    """One LEX request/response, the unit the retrieval audit is derived from."""

    tool: str
    url: str
    payload: Dict[str, Any]
    status: int
    elapsed_ms: int
    response: Any


@dataclass
class ToolRun:
    """One tool execution: what was asked, what came back, and the request made.

    `call` is None when the request never completed, so the run has an output
    but no HTTP exchange of its own. Pairing them here is what stops a failed
    call shifting every later output onto the wrong request.
    """

    name: str
    args: Dict[str, Any]
    output: str
    call: Optional[ApiCall]


class LexTools:
    """Executes the legislation tools and records everything it did."""

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        base_url: str = LEX_API_URL,
        verify: bool = LEX_API_TLS_VERIFY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, verify=verify)
        self.api_calls: List[ApiCall] = []
        self.runs: List[ToolRun] = []

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LexTools":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- HTTP ------------------------------------------------------------

    def _send(
        self, method: str, url: str, tool: str, **kwargs: Any
    ) -> Tuple[httpx.Response, float]:
        """One request with LexChat's bounded backoff. Returns (response, ms)."""
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                resp = self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= _MAX_RETRIES:
                    raise
                delay = min(_BASE_BACKOFF_S * (2**attempt), _MAX_BACKOFF_S)
                logger.warning(
                    "[LEX] %s network error (%r); retrying in %.1fs", tool, exc, delay
                )
                time.sleep(delay)
                attempt += 1
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                delay = min(_BASE_BACKOFF_S * (2**attempt), _MAX_BACKOFF_S)
                logger.warning(
                    "[LEX] %s HTTP %d; retrying in %.1fs", tool, resp.status_code, delay
                )
                time.sleep(delay)
                attempt += 1
                continue

            return resp, elapsed_ms

    def _post(self, tool: str, path: str, payload: Dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        resp, elapsed_ms = self._send("POST", url, tool, json=payload)
        try:
            resp_json = resp.json()
        except ValueError:
            resp_json = {"text": resp.text}

        self.api_calls.append(
            ApiCall(tool, url, payload, resp.status_code, round(elapsed_ms), resp_json)
        )
        resp.raise_for_status()
        return resp_json

    def _get_case_law(
        self,
        tool: str,
        url: str,
        params: Dict[str, Any],
        parse,
        allow_status: Tuple[int, ...] = (),
    ) -> Tuple[Any, int]:
        """A Find Case Law GET, recorded with its parsed result as the response.

        The service answers in XML, so unlike the LEX API there is no JSON body
        to record. What is stored is the parsed form, which is what LexChat
        hands the model and what `utils/audit_capture.py` reads back out of a
        live run's tool result.

        Raises on any unsuccessful status except those in *allow_status*, which
        the caller answers itself. Recording the call first, then raising, is
        what `_post` does: a failed request is still part of the audit.
        """
        resp, elapsed_ms = self._send("GET", url, tool, params=params, timeout=15.0)
        parsed = parse(resp) if resp.is_success else {}
        self.api_calls.append(
            ApiCall(tool, url, params, resp.status_code, round(elapsed_ms), parsed)
        )
        if resp.status_code not in allow_status:
            resp.raise_for_status()
        return parsed, resp.status_code

    # -- Dispatch --------------------------------------------------------

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Run one tool and return the JSON string LexChat would hand to the model."""
        before = len(self.api_calls)
        try:
            result = self._execute(name, args)
        except Exception as exc:
            logger.warning("[LEX] %s failed: %r", name, exc)
            result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        # A request that never completed records no ApiCall, so the run is kept
        # with None rather than borrowing the next tool's call.
        call = self.api_calls[before] if len(self.api_calls) > before else None
        self.runs.append(ToolRun(name, args, result, call))
        return result

    def _execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "search_legislation":
            payload = {
                "query": args["query"],
                "year_from": args.get("year_from"),
                "year_to": args.get("year_to"),
                "limit": SEARCH_LIMIT,
                "include_text": False,
            }
            slimmed = slim_search_results(
                self._post(name, "/legislation/search", payload)
            )
            slimmed["results"] = slimmed["results"][:SEARCH_LIMIT]
            slimmed["total"] = len(slimmed["results"])
            return json.dumps(slimmed) + _phase2_nudge(slimmed)

        if name == "search_legislation_sections":
            payload = {
                "query": args["query"],
                "legislation_id": args["legislation_id"],
                "limit": SECTION_LIMIT,
            }
            return json.dumps(self._post(name, "/legislation/section/search", payload))

        if name == "get_legislation_text":
            payload = {"legislation_id": args["legislation_id"]}
            return json.dumps(self._post(name, "/legislation/text", payload))

        if name == "search_case_law":
            params = {"query": args["query"]}
            for key in ("court", "date_from", "date_to"):
                if args.get(key):
                    params[key] = args[key]
            parsed, status = self._get_case_law(
                name,
                f"{CASE_LAW_API_URL}/atom.xml",
                params,
                lambda resp: {
                    "results": parse_case_law_atom(resp.text),
                    "query": args["query"],
                },
                allow_status=(400,),
            )
            if status == 400:
                # Mirrors executor.py: a bad court code is answered, not raised,
                # so the author sees which filter to drop.
                return json.dumps(
                    {
                        "error": (
                            f"Invalid court filter {args.get('court', '')!r}. Use only "
                            "the exact court codes from the tool description, e.g. "
                            "'uksc', 'ewca/civ', 'ewhc/admin'."
                        ),
                        "results": [],
                        "total": 0,
                    }
                )
            parsed["total"] = len(parsed.get("results", []))
            return json.dumps(parsed) + _case_law_nudge(parsed)

        if name == "get_case_law_text":
            url = args["url"].rstrip("/")
            try:
                parsed, _status = self._get_case_law(
                    name,
                    f"{url}/data.xml",
                    {},
                    lambda resp: parse_judgment(resp.text, url),
                )
            except httpx.HTTPStatusError as exc:
                # Mirrors executor.py, which catches this around
                # _fetch_judgment_text and answers with an empty judgment. An
                # unread judgment is not retrieval evidence: `text` stays empty,
                # so nothing downstream counts the case as read.
                parsed = {
                    "error": f"HTTP {exc.response.status_code} fetching judgment",
                    "url": url,
                    "text": "",
                }
            return json.dumps(parsed)

        return json.dumps(
            {"error": f"Unknown tool {name}. Expected one of {TOOLS + CASE_LAW_TOOLS}."}
        )

    # -- Derived audit fields --------------------------------------------

    def tool_sequence(self) -> List[str]:
        """Ordered tool names, prefixed as `utils/audit_capture.py` prefixes them.

        Read from the runs, not the requests, so a tool whose request failed
        outright still appears in the sequence it was called in.
        """
        return [f"Worker: {r.name}" for r in self.runs]

    def tools_called(self) -> List[Dict[str, Any]]:
        """The `tools_called` list, shaped as the `responses` table shapes it.

        Each output stays with the request that produced it. Where the request
        never completed, the tool's own arguments stand in for the payload.
        """
        return [
            {
                "name": f"Worker: {run.name}",
                "input_parameters": run.call.payload if run.call else run.args,
                "output": run.output,
            }
            for run in self.runs
        ]

    def retrieval_context(self) -> List[str]:
        """The retrieved legal text, shaped as `lex_eval`'s `retrieval_context`.

        Same derivation as `utils/audit_capture.py`, so groundedness metrics can read
        a reference record without a second code path.
        """
        context: List[str] = []
        for call in self.api_calls:
            if call.tool == "get_legislation_text":
                if isinstance(call.response, dict) and call.response.get("full_text"):
                    context.append(call.response["full_text"])
            elif call.tool == "search_legislation_sections":
                for sec in _sections_of(call.response):
                    text = _section_text(sec)
                    if text:
                        title = sec.get("title") or sec.get("section_title") or ""
                        context.append(f"{title}: {text}" if title else text)
            elif call.tool == "search_case_law":
                for case in self._case_results(call):
                    parts = [
                        p
                        for p in [case.get("ncn"), case.get("court"), case.get("date")]
                        if p
                    ]
                    title = case.get("title", "")
                    context.append(f"{title} ({' | '.join(parts)})" if parts else title)
            elif call.tool == "get_case_law_text":
                judgment = call.response if isinstance(call.response, dict) else {}
                text = judgment.get("text", "")
                if text:
                    header = " ".join(
                        p
                        for p in [judgment.get("title", ""), judgment.get("ncn", "")]
                        if p
                    )
                    context.append(f"{header}: {text}" if header else text)
        return context

    @staticmethod
    def _case_results(call: ApiCall) -> List[Dict[str, Any]]:
        results = (
            call.response.get("results") if isinstance(call.response, dict) else []
        )
        return [r for r in (results or []) if isinstance(r, dict)]

    def _case_search_hits(self) -> Dict[str, Dict[str, Any]]:
        """Every judgment a search returned, keyed by url.

        A search hit carries the court and the date; the judgment XML does not,
        so this is what fills those in for a case whose text was read.
        """
        hits: Dict[str, Dict[str, Any]] = {}
        for call in self.api_calls:
            if call.tool != "search_case_law":
                continue
            for case in self._case_results(call):
                url = (case.get("url") or "").rstrip("/")
                if url and url not in hits:
                    hits[url] = case
        return hits

    def cases_retrieved(self) -> List[Dict[str, Any]]:
        """Judgments whose full text was read: what a case citation must match.

        The case law counterpart of `sources_retrieved`, kept as its own field
        because a judgment has no legislation_id and the citation checks that
        read `sources_retrieved` are legislation-only.
        """
        hits = self._case_search_hits()
        cases: Dict[str, Dict[str, Any]] = {}
        for call in self.api_calls:
            if call.tool != "get_case_law_text":
                continue
            judgment = call.response if isinstance(call.response, dict) else {}
            url = (judgment.get("url") or "").rstrip("/")
            if not judgment.get("text") or not url or url in cases:
                continue
            hit = hits.get(url, {})
            cases[url] = {
                "url": url,
                "title": judgment.get("title") or hit.get("title", ""),
                "ncn": judgment.get("ncn") or hit.get("ncn", ""),
                "court": hit.get("court", ""),
                "date": hit.get("date", ""),
            }
        return list(cases.values())

    def cases_discovered(self) -> List[Dict[str, Any]]:
        """Judgments a search found but whose text was never read.

        The case law counterpart of `sources_discovered`, and the same weaker
        claim: the judgment exists and was located, but nobody read it.
        """
        read = {c["url"] for c in self.cases_retrieved()}
        return [
            case for url, case in self._case_search_hits().items() if url not in read
        ]

    def sources_retrieved(self) -> List[Dict[str, Any]]:
        """Provisions whose text was actually pulled: what a citation must match."""
        seen: Dict[str, Dict[str, Any]] = {}
        for call in self.api_calls:
            if call.tool == "search_legislation_sections":
                for sec in _sections_of(call.response):
                    uri = sec.get("uri") or sec.get("id") or ""
                    if uri and uri not in seen:
                        seen[uri] = {
                            "uri": uri,
                            "title": sec.get("title", ""),
                            "legislation_id": call.payload.get("legislation_id", ""),
                            "provision_type": sec.get("provision_type", ""),
                            "extent": sec.get("extent", []),
                        }
            elif call.tool == "get_legislation_text":
                lid = call.payload.get("legislation_id", "")
                uri = f"http://www.legislation.gov.uk/id/{lid}"
                if uri not in seen:
                    # LEX nests the title under "legislation", so reading it off
                    # the top level left every full-Act row in the reviewer's
                    # citation schedule with a blank title.
                    body = call.response if isinstance(call.response, dict) else {}
                    leg = body.get("legislation") or {}
                    seen[uri] = {
                        "uri": uri,
                        "title": leg.get("title") or body.get("title", ""),
                        "legislation_id": lid,
                        "provision_type": "full_text",
                        "extent": leg.get("extent") or [],
                    }
        return list(seen.values())

    def sources_discovered(self) -> List[Dict[str, Any]]:
        """Acts and SIs seen in a search result but never read.

        Citing one of these is a weaker claim than citing a provision above: the Act
        exists and was found, but its text was never retrieved. Keeping the two apart
        is what lets a citation check tell "cited what it read" from "cited what it
        merely saw".
        """
        retrieved = {s["legislation_id"] for s in self.sources_retrieved()}
        seen: Dict[str, Dict[str, Any]] = {}
        for call in self.api_calls:
            if call.tool != "search_legislation" or not isinstance(call.response, dict):
                continue
            for item in slim_search_results(call.response).get("results", []):
                lid = item.get("legislation_id")
                if lid and lid not in retrieved and lid not in seen:
                    seen[lid] = {
                        "uri": item.get("url", ""),
                        "title": item.get("title", ""),
                        "legislation_id": lid,
                        "status": item.get("status", ""),
                        "year": item.get("year"),
                        "extent": item.get("extent", []),
                    }
        return list(seen.values())

    def fallback_used(self) -> bool:
        """Whether the full-Act fallback was used. A legislation-only signal."""
        return any(c.tool == "get_legislation_text" for c in self.api_calls)
