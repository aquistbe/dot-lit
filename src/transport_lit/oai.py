"""Minimal, well-behaved OAI-PMH 2.0 client.

Design points that matter for ROSA-P specifically (all verified live on 2026-08-26):

* Pages hold 100 records; the resumptionToken carries a ``cursor`` but no
  ``completeListSize``, so completeness cannot be checked against a declared total.
* Tokens expire roughly 60 seconds after issue.  Retries therefore use short backoff and
  a ``badResumptionToken`` error is surfaced as its own exception so the harvester can
  decide how to recover.
* An empty selective harvest does NOT return ``<error code="noRecordsMatch">``; the server
  answers with an OAI-PMH envelope that simply has no ``<ListRecords>`` element.  We map
  that to :class:`NoRecordsMatch` when no token was in play, and to
  :class:`TruncatedList` when a token was supplied (a page mid-harvest must never be empty).
* ``from``/``until`` must be full ``YYYY-MM-DDThh:mm:ssZ`` timestamps; a bare date returns
  ``badArgument``.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
NS = {"oai": OAI_NS}


class OAIError(Exception):
    """Protocol-level error reported by the repository."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}".strip())
        self.code = code
        self.message = message


class BadResumptionToken(OAIError):
    pass


class NoRecordsMatch(OAIError):
    pass


class TruncatedList(Exception):
    """A paginated list ended in a way that is not a clean completion."""


class TransportError(Exception):
    """HTTP failure after retries."""


@dataclass
class ListPage:
    records: list[ET.Element]
    token: str | None
    cursor: int | None
    complete_list_size: int | None
    expiration: str | None
    response_date: str | None
    raw: bytes = field(repr=False)


@dataclass
class MetadataFormat:
    prefix: str
    schema: str
    namespace: str


@dataclass
class OAISet:
    spec: str
    name: str


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


class OAIClient:
    def __init__(
        self,
        base_url: str,
        user_agent: str,
        *,
        min_interval: float = 1.0,
        timeout: float = 90.0,
        max_retries: int = 3,
        raw_dir: Path | None = None,
    ):
        self.base_url = base_url
        self.raw_dir = raw_dir
        self.max_retries = max_retries
        self._limiter = RateLimiter(min_interval)
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "text/xml, application/xml"},
            timeout=timeout,
            follow_redirects=True,
        )
        self.requests_made = 0

    # -- transport --------------------------------------------------------------------
    def _get(self, params: dict[str, str]) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            try:
                self.requests_made += 1
                r = self._client.get(self.base_url, params=params)
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"server returned {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                # Keep total retry time short: a resumption token only lives ~60 s.
                backoff = min(2.0 * (attempt + 1), 8.0)
                log.warning("OAI request failed (%s); retry %d/%d in %.0fs",
                            exc, attempt + 1, self.max_retries, backoff)
                time.sleep(backoff)
        raise TransportError(str(last_exc))

    def _save_raw(self, label: str, content: bytes) -> None:
        if not self.raw_dir:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / f"{label}.xml.gz").write_bytes(gzip.compress(content))

    @staticmethod
    def _parse(content: bytes) -> ET.Element:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise TruncatedList(f"response is not well-formed XML: {exc}") from exc
        if root.tag != f"{{{OAI_NS}}}OAI-PMH":
            raise TruncatedList("response is not an OAI-PMH envelope")
        err = root.find("oai:error", NS)
        if err is not None:
            code = err.get("code", "unknown")
            msg = (err.text or "").strip()
            if code == "badResumptionToken":
                raise BadResumptionToken(code, msg)
            if code == "noRecordsMatch":
                raise NoRecordsMatch(code, msg)
            raise OAIError(code, msg)
        return root

    # -- verbs ------------------------------------------------------------------------
    def identify(self) -> dict[str, str]:
        root = self._parse(self._get({"verb": "Identify"}))
        node = root.find("oai:Identify", NS)
        out: dict[str, str] = {}
        if node is not None:
            for child in node:
                tag = child.tag.split("}")[-1]
                if child.text and child.text.strip():
                    out[tag] = child.text.strip()
        return out

    def list_metadata_formats(self) -> list[MetadataFormat]:
        root = self._parse(self._get({"verb": "ListMetadataFormats"}))
        return [
            MetadataFormat(
                prefix=n.findtext("oai:metadataPrefix", "", NS),
                schema=n.findtext("oai:schema", "", NS),
                namespace=n.findtext("oai:metadataNamespace", "", NS),
            )
            for n in root.findall(".//oai:metadataFormat", NS)
        ]

    def list_sets(self) -> list[OAISet]:
        root = self._parse(self._get({"verb": "ListSets"}))
        return [
            OAISet(spec=n.findtext("oai:setSpec", "", NS), name=n.findtext("oai:setName", "", NS))
            for n in root.findall(".//oai:set", NS)
        ]

    def get_record(self, identifier: str, metadata_prefix: str) -> ET.Element | None:
        root = self._parse(
            self._get({"verb": "GetRecord", "metadataPrefix": metadata_prefix, "identifier": identifier})
        )
        return root.find(".//oai:record", NS)

    def list_records_page(
        self,
        metadata_prefix: str,
        *,
        from_: str | None = None,
        until: str | None = None,
        set_spec: str | None = None,
        token: str | None = None,
        raw_label: str | None = None,
    ) -> ListPage:
        if token:
            params = {"verb": "ListRecords", "resumptionToken": token}
        else:
            params = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
            if from_:
                params["from"] = from_
            if until:
                params["until"] = until
            if set_spec:
                params["set"] = set_spec
        content = self._get(params)
        if raw_label:
            self._save_raw(raw_label, content)
        root = self._parse(content)
        container = root.find("oai:ListRecords", NS)
        if container is None:
            # ROSA-P quirk: empty selective harvests come back with no ListRecords element
            # and no <error>.  Mid-list (token supplied) this is a truncation, not "done".
            if token:
                raise TruncatedList("empty envelope returned for a live resumption token")
            raise NoRecordsMatch("noRecordsMatch", "(empty envelope, inferred)")
        records = container.findall("oai:record", NS)
        tok_el = container.find("oai:resumptionToken", NS)
        token_out = (tok_el.text or "").strip() if tok_el is not None else ""
        cursor = tok_el.get("cursor") if tok_el is not None else None
        cls = tok_el.get("completeListSize") if tok_el is not None else None
        return ListPage(
            records=records,
            token=token_out or None,
            cursor=int(cursor) if cursor is not None else None,
            complete_list_size=int(cls) if cls is not None else None,
            expiration=tok_el.get("expirationDate") if tok_el is not None else None,
            response_date=root.findtext("oai:responseDate", None, NS),
            raw=content,
        )

    def close(self) -> None:
        self._client.close()


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]
