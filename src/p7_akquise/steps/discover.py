"""Schritt C3: Suche nach neuen Zielfirmen (Produktion und Handel) per KI-Websuche.

Grundsätze:
- Suchbegriffe pro Segment aus config/segmente.yaml (A: Produktion, B: Handel).
- Claude liest die Suchergebnisse und nennt konkrete Firmen mit offizieller Website,
  keine Verzeichnisse, Jobportale oder Medien.
- Normalisierung aller Domains, Dubletten-Ausschluss und striktes Limit pro Lauf.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from p7_akquise.clients.firecrawl_client import BLOCKED_DOMAINS
from p7_akquise.config import segmente
from p7_akquise.models import KANAL_WEB, LeadSegment, normalize_domain

logger = logging.getLogger("p7_akquise.discover")


class DiscoveredCandidate(BaseModel):
    """Kandidat vor der Detailrecherche (aus Websuche oder Adressliste)."""
    firma: str
    domain: str
    website: str | None = None
    ort: str | None = None
    segment: LeadSegment = "C"
    kaufsignal: str | None = None
    quelle_suche: str | None = None
    quelle_kanal: str = KANAL_WEB


class DiscoveryResult(BaseModel):
    """Ergebnis der Kandidatensuche."""
    candidates: list[DiscoveredCandidate] = Field(default_factory=list)
    total_found_raw: int = 0
    unique_candidates: int = 0
    duplicates_dropped: int = 0
    credits_used: float = 0.0
    stats_by_segment: dict[str, int] = Field(default_factory=dict)


def is_blocked(domain: str) -> bool:
    return not domain or domain in BLOCKED_DOMAINS or any(domain.endswith("." + b) for b in BLOCKED_DOMAINS)


def load_segmente_config() -> dict[str, Any]:
    data = segmente()
    return data.get("segmente", {}) if isinstance(data, dict) else {}


class CandidateDiscoverer:
    """Findet neue Zielfirmen über die Websuche."""

    def __init__(self, firecrawl_client: Any = None, max_candidates: int = 40) -> None:
        if firecrawl_client is None:
            from p7_akquise.clients.claude_web_client import get_default_web_client
            firecrawl_client = get_default_web_client()
        self.client = firecrawl_client
        self.max_candidates = max_candidates

    def _from_query(self, query: str, segment: str, limit: int) -> list[DiscoveredCandidate]:
        out: list[DiscoveredCandidate] = []
        if hasattr(self.client, "find_companies"):
            items = self.client.find_companies(query, max_items=limit)
            for i in items:
                firma = str(i.get("firma") or "").strip()
                website = str(i.get("website") or "").strip()
                if len(firma) < 2 or not website.startswith("http"):
                    continue
                domain = normalize_domain(website)
                if is_blocked(domain):
                    continue
                out.append(DiscoveredCandidate(
                    firma=firma, domain=domain, website=website, ort=i.get("ort") or None,
                    segment=segment, kaufsignal=None, quelle_suche=f"Websuche: {query}",
                ))
        else:
            # Fallback (z. B. Firecrawl): Suchtreffer direkt als Firmen-Websites verwenden
            for item in self.client.search(query, limit=limit):
                url = item.get("url", "")
                domain = normalize_domain(url)
                if is_blocked(domain):
                    continue
                title = str(item.get("title") or "")
                firma = title.split("|")[0].split(" - ")[0].strip() or domain.split(".")[0].capitalize()
                out.append(DiscoveredCandidate(
                    firma=firma, domain=domain, website=url, segment=segment, quelle_suche=f"Websuche: {query}",
                ))
        return out

    def discover_segment(self, segment: str, limit: int) -> list[DiscoveredCandidate]:
        queries = load_segmente_config().get(segment, {}).get("suchbegriffe", []) or []
        candidates: list[DiscoveredCandidate] = []
        for q in queries:
            if len(candidates) >= limit:
                break
            logger.info("Segment %s – Suche: '%s'", segment, q)
            candidates.extend(self._from_query(q, segment, limit=10))
        return candidates

    def discover_all(self, max_candidates: int | None = None) -> DiscoveryResult:
        """Sucht in beiden Zielsegmenten (ca. 60 % Produktion, 40 % Handel) und dedupliziert."""
        limit = max_candidates if max_candidates is not None else self.max_candidates
        if limit <= 0:
            return DiscoveryResult()
        limit_a = max(1, int(round(limit * 0.6)))
        limit_b = max(1, limit - limit_a)

        all_raw = self.discover_segment("A", limit_a) + self.discover_segment("B", limit_b)
        seen: set[str] = set()
        unique: list[DiscoveredCandidate] = []
        stats: dict[str, int] = {"A": 0, "B": 0, "C": 0}
        for cand in all_raw:
            if cand.domain in seen:
                continue
            seen.add(cand.domain)
            unique.append(cand)
            stats[cand.segment] = stats.get(cand.segment, 0) + 1
            if len(unique) >= limit:
                break

        return DiscoveryResult(
            candidates=unique,
            total_found_raw=len(all_raw),
            unique_candidates=len(unique),
            duplicates_dropped=len(all_raw) - len(unique),
            credits_used=getattr(self.client, "credits_used", 0.0),
            stats_by_segment=stats,
        )
