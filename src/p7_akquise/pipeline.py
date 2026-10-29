"""Orchestrierung der projekt7 KI-Akquise Pipeline.

Ablauf eines Laufs:
 1. Rückmeldungen des Key Account Managers verarbeiten (Freigabe / Verwerfen)
 1b. Mail-Entwürfe und Nachfass-Fristen (Erstmail, Nachfass 1 und 2 im Wochentakt)
 2. Importierte Adressen («zu bewerten») recherchieren und neu bewerten
 3. Neue Zielfirmen per KI-Websuche finden (Produktion und Handel)
 4. Abgleich gegen Bestand, Ausschlussliste und Verworfen
 5./6. Recherche, Belegprüfung und deterministisches Scoring
 7. Speichern und Protokoll
 8. Wochenübersicht (Digest)
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from p7_akquise.clients.llm import LLMClient
from p7_akquise.config import setting
from p7_akquise.logging_config import setup_logging
from p7_akquise.models import Lead, ProtocolEntry, RejectedLead, normalize_domain
from p7_akquise.steps.dedupe import filter_candidates
from p7_akquise.steps.digest import generate_digest_html, send_digest_email
from p7_akquise.steps.discover import CandidateDiscoverer, DiscoveredCandidate
from p7_akquise.steps.drafts import process_leads_drafts_and_followups
from p7_akquise.steps.importer import leads_to_evaluate
from p7_akquise.steps.research import CompanyResearcher
from p7_akquise.steps.scoring import ScoringEngine
from p7_akquise.storage.base import LeadStore
from p7_akquise.storage.excel_store import ExcelStore

logger = setup_logging()


class AkquisePipeline:
    """Haupt-Pipeline der wöchentlichen Lead-Akquise für projekt7."""

    def __init__(
        self,
        store: LeadStore | None = None,
        firecrawl_client: Any = None,
        llm_client: LLMClient | None = None,
        scoring_engine: ScoringEngine | None = None,
        dry_run: bool = False,
        kandidaten_pro_lauf: int | None = None,
        import_pro_lauf: int | None = None,
        a_leads_pro_woche: int | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.store = store or ExcelStore(setting("storage", "excel_path", "data/projekt7_Leads.xlsx"))
        if firecrawl_client is None:
            from p7_akquise.clients.claude_web_client import get_default_web_client
            firecrawl_client = get_default_web_client()
        self.firecrawl = firecrawl_client
        self.llm = llm_client or LLMClient()
        self.scoring_engine = scoring_engine or ScoringEngine()
        self.kandidaten_pro_lauf = kandidaten_pro_lauf if kandidaten_pro_lauf is not None else int(setting("pipeline", "kandidaten_pro_lauf", 40))
        self.import_pro_lauf = import_pro_lauf if import_pro_lauf is not None else int(setting("pipeline", "import_bewertungen_pro_lauf", 60))
        self.a_leads_pro_woche = a_leads_pro_woche if a_leads_pro_woche is not None else int(setting("pipeline", "a_leads_pro_woche", 8))
        self.wiedervorlage_tage = int(setting("pipeline", "b_lead_wiedervorlage_tage", 28))

    # ------------------------------------------------------------------ Schritt 1
    def process_advisor_feedback(self) -> tuple[dict[str, int], list[Lead]]:
        """Übernimmt Freigaben/Verwerfungen, die direkt in der Excel-Datei erfasst wurden."""
        logger.info("Schritt 1: Lese Rückmeldungen des Key Account Managers ein...")
        stats = {"freigegeben": 0, "verworfen": 0, "ohne_grund": 0}
        pending: list[Lead] = []
        for lead in self.store.get_all_leads():
            if not lead.freigabe:
                continue
            val = lead.freigabe.strip().lower()
            if val == "ja" and lead.status in ("zur Prüfung", "beobachten", "verworfen", "neu"):
                lead.status = "freigegeben"
                lead.wiedervorlage_am = None
                if lead.klasse != "A":
                    lead.manuelle_freigabe = True
                lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()
                if not self.dry_run:
                    self.store.upsert_lead(lead)
                stats["freigegeben"] += 1
            elif val == "nein":
                if lead.verwerfungsgrund and lead.verwerfungsgrund.strip():
                    if lead.status != "verworfen":
                        lead.status = "verworfen"
                        lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()
                        if not self.dry_run:
                            self.store.upsert_lead(lead)
                            self.store.add_rejected_lead(RejectedLead(domain=lead.domain, firma=lead.firma, grund=lead.verwerfungsgrund))
                        stats["verworfen"] += 1
                else:
                    stats["ohne_grund"] += 1
                    pending.append(lead)
        return stats, pending

    # ------------------------------------------------------------- Klassen-Logik
    def _apply_status(self, lead: Lead, buckets: dict[str, list[Lead]]) -> None:
        if lead.status == "fehler":
            buckets["fehler"].append(lead)
            return
        if lead.klasse == "A":
            lead.status = "zur Prüfung"
            lead.wiedervorlage_am = None
            buckets["A"].append(lead)
        elif lead.klasse == "B":
            lead.status = "beobachten"
            lead.wiedervorlage_am = (date.today() + timedelta(days=self.wiedervorlage_tage)).strftime("%Y-%m-%d")
            buckets["B"].append(lead)
        else:
            lead.status = "verworfen"
            buckets["C"].append(lead)
            if not self.dry_run:
                self.store.add_rejected_lead(RejectedLead(domain=lead.domain, firma=lead.firma, grund=f"Klasse C (Score {lead.score} < 50)"))

    # ------------------------------------------------------------------ Schritt 2
    def evaluate_imported(self, researcher: CompanyResearcher, buckets: dict[str, list[Lead]]) -> int:
        """Recherchiert und bewertet importierte Adressen (Status «zu bewerten»)."""
        pending = leads_to_evaluate(self.store, self.import_pro_lauf)
        logger.info("Schritt 2: Bewerte %d importierte Adressen...", len(pending))
        for base in pending:
            cand = DiscoveredCandidate(
                firma=base.firma, domain=base.domain, website=base.website, ort=base.ort,
                segment=base.segment, quelle_suche=base.import_datei, quelle_kanal=base.quelle_kanal,
            )
            lead = researcher.research_candidate(cand, base=base)
            self._apply_status(lead, buckets)
            if self.dry_run:
                continue
            # Website ermittelt und Firma existiert schon mit dieser Domain: Dublette zusammenführen
            if normalize_domain(lead.domain) != normalize_domain(base.domain):
                other = self.store.get_lead_by_domain(lead.domain)
                if other and other.lead_id != lead.lead_id:
                    if lead.historie and lead.historie not in (other.historie or ""):
                        other.historie = " | ".join(x for x in (other.historie, lead.historie) if x)
                        self.store.upsert_lead(other)
                    try:
                        self.store.remove_lead(base.lead_id)
                    except NotImplementedError:
                        pass
                    continue
            self.store.upsert_lead(lead)
        return len(pending)

    # ------------------------------------------------------------------ Wochenlauf
    def run_weekly(self, reference_date: date | str | None = None) -> dict[str, Any]:
        lauf_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:8]}"
        ref_date = reference_date or date.today()
        logger.info("=== Starte projekt7 Wochenlauf [%s] (Dry-Run: %s) ===", lauf_id, self.dry_run)

        fb_stats, pending_clarifications = self.process_advisor_feedback()

        logger.info("Schritt 1b: Mail-Entwürfe und Nachfass-Fristen...")
        drafts_res = process_leads_drafts_and_followups(store=self.store, reference_date=ref_date, dry_run=self.dry_run)

        researcher = CompanyResearcher(firecrawl_client=self.firecrawl, llm_client=self.llm, scoring_engine=self.scoring_engine)
        buckets: dict[str, list[Lead]] = {"A": [], "B": [], "C": [], "fehler": []}

        # 2. Importierte Adressen bewerten
        anzahl_import = self.evaluate_imported(researcher, buckets)

        # 3. Neue Firmen per Websuche
        logger.info("Schritt 3: Suche neue Zielfirmen (max. %d)...", self.kandidaten_pro_lauf)
        discoverer = CandidateDiscoverer(firecrawl_client=self.firecrawl, max_candidates=self.kandidaten_pro_lauf)
        discovery_res = discoverer.discover_all(max_candidates=self.kandidaten_pro_lauf)

        # 4. Abgleich
        dedupe_res = filter_candidates(candidates=discovery_res.candidates, store=self.store, reference_date=ref_date)
        to_research = dedupe_res.candidates_to_research
        logger.info(
            "Zur Recherche: %d (gefiltert: %d Ausschluss, %d Verworfen, %d Bestand; %d B-Leads wieder vorgelegt)",
            len(to_research), len(dedupe_res.dropped_exclusion), len(dedupe_res.dropped_rejected),
            len(dedupe_res.dropped_existing), len(dedupe_res.b_leads_resurfaced),
        )

        # 5./6. Recherche und Scoring der neuen Kandidaten
        web_leads: list[Lead] = []
        for cand in to_research:
            existing = self.store.get_lead_by_domain(cand.domain)
            lead = researcher.research_candidate(cand, base=existing)
            self._apply_status(lead, buckets)
            web_leads.append(lead)

        # 7. Speichern und Protokoll
        saved = 0
        if not self.dry_run:
            saved = self.store.upsert_leads(web_leads) + anzahl_import
            self.store.add_protocol_entry(ProtocolEntry(
                lauf_id=lauf_id,
                modell_version=self.scoring_engine.version,
                anzahl_kandidaten=discovery_res.unique_candidates,
                anzahl_import_bewertet=anzahl_import,
                duplikate=discovery_res.duplicates_dropped + len(dedupe_res.dropped_existing),
                anzahl_a=len(buckets["A"]),
                anzahl_b=len(buckets["B"]),
                anzahl_c=len(buckets["C"]),
                fehler=len(buckets["fehler"]),
                verbrauchte_credits=getattr(self.firecrawl, "credits_used", 0.0),
            ))

        # 8. Digest
        top_a = sorted(buckets["A"], key=lambda l: l.score, reverse=True)[: self.a_leads_pro_woche]
        run_stats = {
            "lauf_id": lauf_id,
            "a_leads": len(buckets["A"]),
            "b_leads": len(buckets["B"]),
            "c_leads": len(buckets["C"]),
            "import_bewertet": anzahl_import,
            "kandidaten_recherchiert": len(to_research),
            "credits_used": getattr(self.firecrawl, "credits_used", 0.0),
            "fehler": len(buckets["fehler"]),
        }
        digest_html = generate_digest_html(
            a_leads=top_a, run_stats=run_stats,
            pending_clarifications=pending_clarifications, due_followups=drafts_res.followups_generated,
            excel_link=os.getenv("DASHBOARD_URL", ""),
        )
        send_digest_email(
            html_content=digest_html,
            subject=f"projekt7 KI-Akquise: {len(buckets['A'])} neue A-Leads ({lauf_id})",
            dry_run=self.dry_run,
        )

        logger.info("=== projekt7 Wochenlauf [%s] abgeschlossen ===", lauf_id)
        return {
            "lauf_id": lauf_id,
            "dry_run": self.dry_run,
            "fb_stats": fb_stats,
            "import_bewertet": anzahl_import,
            "kandidaten_gefunden": discovery_res.total_found_raw,
            "kandidaten_recherchiert": len(to_research),
            "leads_gespeichert": saved,
            "neue_a_leads": len(buckets["A"]),
            "neue_b_leads": len(buckets["B"]),
            "neue_c_leads": len(buckets["C"]),
            "top_a_leads": top_a,
            "fehler": len(buckets["fehler"]),
            "credits_used": getattr(self.firecrawl, "credits_used", 0.0),
            "digest_html": digest_html,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="projekt7 KI-Akquise Wochenlauf")
    parser.add_argument("--dry-run", action="store_true", help="Nichts schreiben, nichts senden")
    parser.add_argument("--max-kandidaten", type=int, default=None, help="Max. neue Firmen aus der Websuche (0 = keine Websuche)")
    parser.add_argument("--max-import", type=int, default=None, help="Max. importierte Adressen, die in diesem Lauf bewertet werden")
    parser.add_argument("--summary-file", default=None, help="Pfad für eine Markdown-Zusammenfassung (z. B. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()

    pipeline = AkquisePipeline(dry_run=args.dry_run, kandidaten_pro_lauf=args.max_kandidaten, import_pro_lauf=args.max_import)
    result = pipeline.run_weekly()

    if args.summary_file:
        lines = [
            f"## projekt7 Wochenlauf {result.get('lauf_id', '')}",
            "",
            f"- Dry-Run: **{result.get('dry_run')}**",
            f"- Importierte Adressen bewertet: {result.get('import_bewertet')}",
            f"- Neue Firmen gefunden / recherchiert: {result.get('kandidaten_gefunden')} / {result.get('kandidaten_recherchiert')}",
            f"- Leads A / B / C: {result.get('neue_a_leads')} / {result.get('neue_b_leads')} / {result.get('neue_c_leads')}",
            f"- Fehler: {result.get('fehler')}",
            f"- Web-Aktionen: {result.get('credits_used')}",
            "",
            "| Firma | Score | Klasse | Signal | Begründung |",
            "|---|---|---|---|---|",
        ]
        for lead in result.get("top_a_leads") or []:
            l = lead if isinstance(lead, dict) else lead.model_dump()
            begr = str(l.get("begruendung") or "").replace("|", "/").replace("\n", " ")[:160]
            sig = str(l.get("kaufsignal") or "").replace("|", "/")[:80]
            lines.append(f"| {l.get('firma')} | {l.get('score')} | {l.get('klasse')} | {sig} | {begr} |")
        with open(args.summary_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
