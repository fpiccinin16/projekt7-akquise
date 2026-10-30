"""Schritt C5: Website-Ermittlung, vertiefte Recherche, Anti-Halluzinationsprüfung und LLM-Extraktion."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

from p7_akquise.clients.firecrawl_client import BLOCKED_DOMAINS, FirecrawlClient
from p7_akquise.clients.llm import LLMClient
from p7_akquise.models import (
    CriterionResult,
    Evidence,
    Lead,
    LeadStatus,
    has_real_domain,
    normalize_domain,
)
from p7_akquise.steps.discover import DiscoveredCandidate
from p7_akquise.steps.scoring import ScoringEngine

logger = logging.getLogger("p7_akquise.research")

SUBPAGE_PATTERNS = {
    "about": re.compile(r"/(ueber-uns|about|unternehmen|wir-ueber-uns|profil)", re.IGNORECASE),
    "news": re.compile(r"/(news|aktuell|events|messen|agenda|aktuelles)", re.IGNORECASE),
    "team": re.compile(r"/(kontakt|team|ansprechpartner|ansprechspartner|mitarbeiter|standorte|leitung|management)", re.IGNORECASE),
    "impressum": re.compile(r"/(impressum|legal|rechtliches)", re.IGNORECASE),
    "karriere": re.compile(r"/(karriere|jobs|stellen|offene-stellen|stellenangebote)", re.IGNORECASE),
}


def _normalize_for_match(text: str) -> str:
    """Reduziert Text auf Kleinbuchstaben, Ziffern und einfache Leerzeichen.

    Firecrawl und Claude liefern Markdown (z. B. **fett**, [Link](url), \\+600, Zeilenumbrüche).
    Damit echte Zitate nicht an Formatierung scheitern, zählen nur Buchstaben und Ziffern.
    """
    import re as _re
    text = text.lower()
    text = _re.sub(r"\]\([^)]*\)", " ", text)          # Markdown-Link-Ziele entfernen: [Text](url)
    text = _re.sub(r"[^0-9a-zäöüàâçéèêëîïôûùüÿñæœß]+", " ", text)
    return " ".join(text.split())


def verify_evidence(quote: str, raw_pages_text: str) -> bool:
    """Prüft, ob das Zitat tatsächlich im Text der geladenen Seiten vorkommt.

    Formatierung, Satzzeichen, Gross-/Kleinschreibung und Zeilenumbrüche werden ignoriert.
    Kleine Abweichungen (z. B. ein fehlendes Wort) werden toleriert, wenn mindestens 90 %
    des Zitats als zusammenhängender Text auf der Seite stehen.
    """
    if not quote or not raw_pages_text:
        return False

    q = quote.strip()
    for ellipsis in ("...", "…"):
        q = q.replace(ellipsis, " ")
    norm_quote = _normalize_for_match(q)
    norm_text = _normalize_for_match(raw_pages_text)
    if not norm_quote or len(norm_quote) < 4:
        return False

    if norm_quote in norm_text:
        return True

    from difflib import SequenceMatcher
    matcher = SequenceMatcher(None, norm_text, norm_quote, autojunk=False)
    match = matcher.find_longest_match(0, len(norm_text), 0, len(norm_quote))
    return match.size >= 0.9 * len(norm_quote)


def suggest_angebot(segment: str | None, branche: str | None, kaufsignal: str | None) -> str:
    """Schlägt einen Gesprächseinstieg aus dem projekt7-Angebot vor (fliesst nicht in den Score ein)."""
    text = f"{branche or ''} {kaufsignal or ''}".lower()
    if any(k in text for k in ("lohn", "personal", "hr ")):
        return "Abacus Lohn/HR"
    if any(k in text for k in ("finanz", "buchhaltung", "treuhand")):
        return "Abacus Finanzen"
    if segment == "A" or any(k in text for k in ("produktion", "fertigung", "maschinen", "herstell")):
        return "Abacus ERP mit Produktion/PPS"
    if segment == "B" or any(k in text for k in ("handel", "lager", "shop", "vertrieb")):
        return "Abacus Auftragsbearbeitung und Lager"
    return "Abacus ERP (Erstgespräch)"


SEGMENT_MAP = {"produktion": "A", "handel": "B"}


def segment_from_text(value: str | None) -> str:
    v = (value or "").strip().lower()
    for key, seg in SEGMENT_MAP.items():
        if key in v:
            return seg
    return "C"


class CompanyResearcher:
    """Führt Multipage-Scrapes durch, extrahiert Kriterien via LLM und verifiziert Belege."""

    def __init__(
        self,
        firecrawl_client: FirecrawlClient | None = None,
        llm_client: LLMClient | None = None,
        scoring_engine: ScoringEngine | None = None,
        prompt_path: str | Path | None = None,
        max_pages: int = 5,
    ) -> None:
        self.firecrawl = firecrawl_client or FirecrawlClient()
        self.llm = llm_client or LLMClient()
        self.scoring_engine = scoring_engine or ScoringEngine()
        self.max_pages = max_pages
        self.system_prompt = self._load_prompt(prompt_path)

    def _load_prompt(self, path: str | Path | None) -> str:
        if path is None:
            base_dir = Path(__file__).resolve().parents[3]
            path = base_dir / "prompts" / "extraction.md"
        else:
            path = Path(path)

        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return "Du bist ein präziser Recherche-Assistent für die Lead-Bewertung der projekt7 AG."

    def find_subpage_urls(self, base_url: str, markdown: str) -> list[str]:
        """Extrahiert relevante interne Unterseiten aus dem Startseiten-Markdown."""
        subpages: list[str] = []
        parsed_base = urlparse(base_url)
        base_netloc = parsed_base.netloc.lower()

        # Markdown-Links suchen
        matches = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", markdown)
        categorized: dict[str, str] = {}

        for text, href in matches:
            clean_href = href.split("#")[0].split("?")[0].strip()
            if not clean_href:
                continue

            full_url = urljoin(base_url, clean_href)
            parsed_href = urlparse(full_url)

            # Nur Links auf der gleichen Domain
            if parsed_href.netloc.lower() != base_netloc:
                continue

            path = parsed_href.path
            for cat, pattern in SUBPAGE_PATTERNS.items():
                if pattern.search(path):
                    if cat == "team" and "team" in categorized:
                        is_specific = any(k in path.lower() for k in ["team", "ansprech", "mitarbeit", "leitung", "management"])
                        old_is_specific = any(k in urlparse(categorized["team"]).path.lower() for k in ["team", "ansprech", "mitarbeit", "leitung", "management"])
                        if is_specific and not old_is_specific:
                            categorized["team"] = full_url
                    elif cat not in categorized:
                        categorized[cat] = full_url

        for url in categorized.values():
            if url not in subpages and url != base_url:
                subpages.append(url)
            if len(subpages) >= self.max_pages - 1:
                break

        return subpages

    def resolve_website(self, candidate: DiscoveredCandidate) -> str | None:
        """Sucht die offizielle Website einer importierten Firma ohne Website-Angabe."""
        query = f"{candidate.firma} {candidate.ort or ''} Schweiz".strip()
        try:
            results = self.firecrawl.search(query, limit=5)
        except Exception as e:
            logger.warning("Websuche für %s fehlgeschlagen: %s", candidate.firma, e)
            return None
        name_tokens = [t for t in re.split(r"[^a-z0-9äöü]+", candidate.firma.lower()) if len(t) >= 3 and t not in ("gmbh", "ag", "und", "the")]
        for item in results:
            url = str(item.get("url") or "")
            domain = normalize_domain(url)
            if not domain or domain in BLOCKED_DOMAINS or any(domain.endswith("." + b) for b in BLOCKED_DOMAINS):
                continue
            haystack = f"{domain} {item.get('title') or ''}".lower()
            if name_tokens and not any(tok in haystack for tok in name_tokens):
                continue
            parsed = urlparse(url if url.startswith("http") else f"https://{url}")
            return f"{parsed.scheme}://{parsed.netloc}"
        return None

    def fetch_company_pages(self, candidate: DiscoveredCandidate) -> dict[str, str]:
        """Lädt bis zu max_pages Seiten einer Firma und gibt URL -> Text zurück."""
        pages: dict[str, str] = {}
        base_url = candidate.website or f"https://{candidate.domain}"

        start_res = self.firecrawl.scrape(base_url)
        start_md = start_res.get("markdown", "")
        pages[base_url] = start_md
        if not start_md:
            return pages

        for sub_url in self.find_subpage_urls(base_url, start_md):
            if len(pages) >= self.max_pages:
                break
            sub_md = self.firecrawl.scrape(sub_url).get("markdown", "")
            if sub_md:
                pages[sub_url] = sub_md
        return pages

    def _base_lead(self, candidate: DiscoveredCandidate, base: Lead | None) -> Lead:
        """Ausgangsdatensatz: vorhandener (importierter) Lead oder neuer Lead aus dem Kandidaten."""
        if base is not None:
            return base.model_copy(deep=True)
        return Lead(
            domain=candidate.domain,
            firma=candidate.firma,
            website=candidate.website,
            ort=candidate.ort,
            segment=candidate.segment,
            kaufsignal=candidate.kaufsignal,
            quelle_kanal=candidate.quelle_kanal,
        )

    def _error_lead(self, lead: Lead, grund: str) -> Lead:
        lead.status = "fehler"
        lead.score = 0
        lead.klasse = "C"
        lead.teilscores = {}
        lead.begruendung = grund
        lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()
        return lead

    def research_candidate(self, candidate: DiscoveredCandidate, base: Lead | None = None) -> Lead:
        """Recherchiert einen Kandidaten, extrahiert Kriterien und berechnet den Score.

        `base` ist ein bestehender Lead (z. B. aus der importierten Adressliste). Seine Stamm- und
        Importdaten bleiben erhalten; nur leere Felder werden aus der Recherche ergänzt.
        """
        lead = self._base_lead(candidate, base)
        logger.info("Starte Recherche für: %s (%s)", candidate.firma, candidate.domain)

        # 0. Website ermitteln, falls die Adresse ohne Website importiert wurde
        if not has_real_domain(candidate.domain):
            website = self.resolve_website(candidate)
            if not website:
                return self._error_lead(lead, "Website nicht gefunden. Bitte Website in der Adressliste ergänzen.")
            candidate = candidate.model_copy(update={"website": website, "domain": normalize_domain(website)})
            lead.website = website
            lead.domain = candidate.domain

        # 1. Seiteninhalte laden
        pages = self.fetch_company_pages(candidate)
        combined_text = "\n\n--- NEUE SEITE ---\n\n".join(
            f"URL: {url}\nINHALT:\n{text[:4000]}" for url, text in pages.items() if text
        )
        if not combined_text.strip():
            logger.warning("Kein Webseiten-Inhalt gefunden für %s", candidate.domain)
            return self._error_lead(lead, "Webseite konnte nicht geladen werden.")

        # 2. Prompt
        prompt = f"""
Bitte analysiere folgende Webseiten-Inhalte des Unternehmens '{candidate.firma}' (Domain: {candidate.domain}).
Bekannter Ort: {candidate.ort or lead.ort or 'Keine Angabe'}
Herkunft: {candidate.quelle_kanal} ({candidate.quelle_suche or ''})

SEITENTEXTE:
{combined_text}
"""

        # 3. LLM-Aufruf mit einem Wiederholungsversuch
        extracted_data: dict[str, Any] = {}
        for attempt in range(2):
            try:
                extracted_data = self.llm.generate_json(prompt=prompt, system_prompt=self.system_prompt)
                if extracted_data and "kriterien" in extracted_data:
                    break
            except Exception as e:
                logger.warning("LLM-Extraktionsfehler für %s (Versuch %d/2): %s", candidate.domain, attempt + 1, e)
                if attempt == 1:
                    return self._error_lead(lead, f"LLM-Extraktionsfehler: {e}")

        # 4. Anti-Halluzinations-Prüfung der Belege
        verified_criteria: list[CriterionResult] = []
        valid_sources: set[str] = set()
        for c_dict in extracted_data.get("kriterien", []) or []:
            crit_name = c_dict.get("kriterium", "")
            verified_belege: list[Evidence] = []
            for b in c_dict.get("belege", []) or []:
                quote = str(b.get("zitat", "")).strip()
                url = b.get("url", "")
                if not quote or not url:
                    continue
                if verify_evidence(quote, combined_text):
                    try:
                        ev = Evidence(zitat=quote, url=url)
                        verified_belege.append(ev)
                        valid_sources.add(str(ev.url))
                    except Exception:
                        continue
                else:
                    logger.info("Nicht belegtes Zitat abgewiesen für %s (%s): '%s'", candidate.domain, crit_name, quote[:50])
            verified_criteria.append(
                CriterionResult(
                    kriterium=crit_name,
                    erfuellt=c_dict.get("erfuellt") if verified_belege else False,
                    wert=c_dict.get("wert"),
                    belege=verified_belege,
                )
            )

        # 5. Deterministisches Scoring
        ko_flags = {"bestandskunde": True} if base is not None and base.status == "verworfen" and (base.verwerfungsgrund or "") == "Bestandskunde" else None
        scoring_res = self.scoring_engine.score_lead(verified_criteria, ko_flags=ko_flags)

        # 6. Stammdaten ergänzen (Importwerte haben Vorrang)
        seg_crit = next((c for c in verified_criteria if c.kriterium == "segment_produktion_handel" and c.erfuellt), None)
        segment = segment_from_text((seg_crit.wert if seg_crit else None) or extracted_data.get("segment"))

        def keep(current: Any, new: Any) -> Any:
            return current if current not in (None, "") else (new or None)

        lead.firma = keep(lead.firma, extracted_data.get("firma"))
        lead.website = lead.website or candidate.website or f"https://{candidate.domain}"
        lead.ort = keep(lead.ort, extracted_data.get("ort"))
        lead.kanton = keep(lead.kanton, extracted_data.get("kanton"))
        lead.land = extracted_data.get("land") or lead.land or "CH"
        lead.segment = segment  # type: ignore[assignment]
        lead.branche = extracted_data.get("branche") or lead.branche
        lead.groesse = extracted_data.get("groesse") or lead.groesse
        lead.kaufsignal = extracted_data.get("kaufsignal") or lead.kaufsignal
        for field in ("kontakt_name", "kontakt_rolle", "kontakt_email", "kontakt_tel"):
            setattr(lead, field, keep(getattr(lead, field), extracted_data.get(field)))
        lead.angebot = suggest_angebot(segment, lead.branche, lead.kaufsignal)
        lead.score = scoring_res.score
        lead.klasse = scoring_res.klasse
        lead.teilscores = scoring_res.teilscores
        lead.begruendung = scoring_res.begruendung
        lead.quellen = sorted(valid_sources)
        lead.modell_version = scoring_res.modell_version
        lead.status = "zur Prüfung" if scoring_res.klasse == "A" else ("beobachten" if scoring_res.klasse == "B" else "verworfen")

        # Manuelle Änderungen und Sperren respektieren
        if base is not None:
            lead.gesperrte_felder = list(base.gesperrte_felder)
            lead.score_manuell = base.score_manuell
            for f in base.gesperrte_felder:
                if hasattr(base, f):
                    setattr(lead, f, getattr(base, f))
            if base.score_manuell:
                lead.score = base.score
                lead.klasse = base.klasse
                lead.teilscores = dict(base.teilscores)
                lead.begruendung = base.begruendung
                lead.status = base.status

        lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()
        return lead
