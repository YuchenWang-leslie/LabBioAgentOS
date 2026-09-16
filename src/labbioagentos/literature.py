"""Bounded public literature retrieval; never access analysis files or credentials."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import unescape
import json
import re
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import Field, StrictStr


LiteratureQuery = Annotated[StrictStr, Field(min_length=1, max_length=400)]
LiteratureLimit = Annotated[int, Field(strict=True, ge=1, le=5)]
_MAX_RESPONSE_BYTES = 1_048_576


class LiteratureSearchError(ValueError):
    """A fixed diagnostic, independent of query text or remote response bodies."""

    MESSAGES = {
        "INVALID_LITERATURE_QUERY": "Use 1-400 characters of public scientific search terms and an integer limit of 1-5; private data, paths and credentials are not search terms. The source may reject unsupported query syntax.",
        "LITERATURE_TIMEOUT": "The literature source timed out; no search results were obtained.",
        "LITERATURE_RATE_LIMITED": "The literature source rate limit was reached; no search results were obtained.",
        "LITERATURE_UNAVAILABLE": "The literature source could not be reached; no search results were obtained.",
        "LITERATURE_RESPONSE_INVALID": "The literature source returned an invalid or oversized response; no search results were released.",
    }

    def __init__(self, code: str):
        self.code = code
        self.safe_message = self.MESSAGES[code]
        super().__init__(code)


class LiteratureSearchService:
    """One fixed HTTPS source, default TLS verification, no internal retries.

    Only model-selected public query terms leave the host. Environment proxy
    handling is inherited without modifying any process or system settings.
    Search results are external context, not proof of current-run observations.
    """

    async def search(self, query: str, limit: int = 3) -> dict:
        if (not isinstance(query, str) or not 1 <= len(query) <= 400
                or not query.strip() or any(ord(char) < 32 for char in query)
                or query.lstrip().startswith(("/", "~", "file:"))
                or type(limit) is not int or not 1 <= limit <= 5):
            raise LiteratureSearchError("INVALID_LITERATURE_QUERY")
        return await asyncio.to_thread(self._search, query, limit)

    def _search(self, query: str, limit: int) -> dict:
        request = Request(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode({
                "query": query, "format": "json", "resultType": "core", "pageSize": limit,
            }),
            headers={"Accept": "application/json", "User-Agent": "LabBioAgentOS/0.1"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            code = {400: "INVALID_LITERATURE_QUERY", 429: "LITERATURE_RATE_LIMITED"}.get(
                exc.code, "LITERATURE_UNAVAILABLE",
            )
            raise LiteratureSearchError(code) from None
        except TimeoutError:
            raise LiteratureSearchError("LITERATURE_TIMEOUT") from None
        except URLError as exc:
            code = "LITERATURE_TIMEOUT" if isinstance(exc.reason, TimeoutError) else "LITERATURE_UNAVAILABLE"
            raise LiteratureSearchError(code) from None
        except OSError:
            raise LiteratureSearchError("LITERATURE_UNAVAILABLE") from None
        try:
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("Response bound")
            payload = json.loads(body)
            hit_count = payload["hitCount"]
            rows = payload["resultList"]["result"]
            if type(hit_count) is not int or hit_count < 0 or not isinstance(rows, list):
                raise ValueError("Response shape")
            if len(rows) > limit or len(rows) > hit_count:
                raise ValueError("Result count")
            items = [self._article(row) for row in rows]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise LiteratureSearchError("LITERATURE_RESPONSE_INVALID") from None
        return {
            "source": "Europe PMC",
            "content_authority": "MODEL_CONTEXT",
            "content_scope": "PUBLIC_LITERATURE_METADATA_AND_ABSTRACT_EXCERPTS",
            "query": query,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "hit_count": hit_count,
            "returned_count": len(items),
            "items": items,
        }

    @staticmethod
    def _article(row: dict) -> dict:
        source, identifier = row["source"], row["id"]
        if (not isinstance(source, str) or not re.fullmatch(r"[A-Z]{2,6}", source)
                or not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", identifier)):
            raise ValueError("Article identity")
        url = f"https://europepmc.org/article/{source}/{identifier}"
        result = {
            "source": source, "id": identifier,
            "url": url,
            # Existing generic RuntimeReference identity: external context is
            # not registered as a local ARTIFACT or as experimental evidence.
            "source_reference": {"kind": "OTHER", "reference_id": url},
            "truncated_fields": [],
        }
        for target, field, bound in (
            ("title", "title", 400), ("authors", "authorString", 200),
            ("year", "pubYear", 4), ("doi", "doi", 200),
            ("abstract", "abstractText", 1600),
        ):
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError("Article field")
            if value is not None:
                value = " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())
                if len(value) > bound:
                    result["truncated_fields"].append(target)
            result[target] = value[:bound] if value is not None else None
        if not result["title"]:
            raise ValueError("Article title")
        return result
