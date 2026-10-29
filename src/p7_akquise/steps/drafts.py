"""Schritt C12–C14: Kontaktmail-Entwürfe und Nachfass-Tracking für projekt7.

Grundsätze:
- Kein automatischer E-Mail-Versand (UWG Art. 3 Abs. 1 lit. o). Der Key Account Manager sendet selbst.
- Sequenz wie im Interview (LeadRise): Erstmail, Nachfass 1 nach einer Woche, Nachfass 2 eine Woche später.
- Personalisierung nach Segment (A: Produktion, B: Handel) und stärkstem Signal.
- Maximal 120 Wörter für die Erstmail, 80 Wörter für Nachfass-Mails, immer mit Abmeldehinweis.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field

from p7_akquise.config import setting
from p7_akquise.models import Lead
from p7_akquise.storage.base import LeadStore

logger = logging.getLogger("p7_akquise.drafts")

OPT_OUT_NOTE = "Kein Interesse? Eine kurze Rückmeldung genügt, und ich melde mich nicht mehr."
ABSENDER = "Key Account Manager Produktion und Handel\nprojekt7 AG"


def add_workdays(start_date: date, workdays: int = 5) -> date:
    """Addiert eine Anzahl reiner Werktage (Montag bis Freitag) zu einem Startdatum."""
    cur = start_date
    added = 0
    while added < workdays:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def is_followup_due(lead: Lead, reference_date: date | str | None = None) -> bool:
    """Prüft, ob für einen kontaktierten Lead eine Nachfass-Aktion fällig ist."""
    if lead.status not in ("kontaktiert", "nachgefasst_1", "nachgefasst_2"):
        return False
    if not lead.kontaktiert_am:
        return False

    if reference_date is None:
        ref = date.today()
    elif isinstance(reference_date, str):
        ref = datetime.strptime(reference_date[:10], "%Y-%m-%d").date()
    else:
        ref = reference_date

    try:
        k_date = datetime.strptime(lead.kontaktiert_am[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    werktage = int(setting("nachfass", "werktage_bis_nachfass", 5))
    # Jede Stufe ist eine Woche nach der vorherigen fällig
    due_date = add_workdays(k_date, werktage * (lead.anzahl_nachfass + 1))
    return ref >= due_date


def _build_salutation(lead: Lead) -> str:
    """Ermittelt die passende persönliche oder allgemeine Anrede."""
    if not lead.kontakt_name:
        return "Guten Tag"

    name_parts = lead.kontakt_name.strip().split()
    last_name = name_parts[-1] if name_parts else lead.kontakt_name
    raw_lower = f"{lead.kontakt_name} {lead.kontakt_rolle or ''}".lower()
    if "frau" in raw_lower or "leiterin" in raw_lower or "inhaberin" in raw_lower or "geschäftsführerin" in raw_lower:
        return f"Guten Tag Frau {last_name}"
    if "herr" in raw_lower or "leiter " in raw_lower or raw_lower.endswith("leiter") or "inhaber" in raw_lower or "geschäftsführer" in raw_lower:
        return f"Guten Tag Herr {last_name}"
    return f"Guten Tag {lead.kontakt_name}"


def _signal_satz(lead: Lead) -> str:
    if lead.kaufsignal:
        return f"Ich bin auf {lead.firma} gestossen ({lead.kaufsignal.rstrip('.')}). "
    return f"Ich bin auf {lead.firma} gestossen. "


def generate_initial_draft(lead: Lead) -> str:
    """Erzeugt einen zielgruppengerechten Erstansprache-Entwurf (max. 120 Wörter)."""
    salutation = _build_salutation(lead)
    if lead.segment == "B":
        betreff = f"Auftragsabwicklung und Lager bei {lead.firma}"
        nutzen = (
            "Wir unterstützen Handelsunternehmen dabei, Einkauf, Lager, Auftragsabwicklung und Finanzen "
            "in einer durchgängigen Abacus-Lösung zu führen, statt in Excel und Insellösungen."
        )
    else:
        betreff = f"Produktion, Lager und Finanzen bei {lead.firma}"
        nutzen = (
            "Wir unterstützen produzierende KMU dabei, Auftrag, Produktion, Lager und Finanzen "
            "in einer durchgängigen Abacus-Lösung abzubilden und so effizienter zu werden."
        )

    body = (
        f"{salutation}\n\n"
        f"{_signal_satz(lead)}Wir sind die projekt7 AG, Abacus-Partner aus der Ostschweiz. {nutzen}\n\n"
        f"Gerne würde ich Ihnen in einem kurzen persönlichen Gespräch zeigen, wie das bei Ihnen aussehen könnte. "
        f"Passt Ihnen ein Termin in den nächsten zwei Wochen?\n\n"
        f"Freundliche Grüsse\n{ABSENDER}\n\n---\n{OPT_OUT_NOTE}"
    )
    draft = f"Betreff: {betreff}\n\n{body}"
    if len(draft.split()) > 120:
        logger.warning("Mail-Entwurf für %s hat %d Wörter (Ziel <= 120)", lead.domain, len(draft.split()))
    return draft


def generate_followup_draft(lead: Lead, step: int) -> str:
    """Erzeugt einen kurzen Nachfass-Entwurf (Stufe 1 oder 2, max. 80 Wörter)."""
    salutation = _build_salutation(lead)
    if step == 1:
        betreff = f"Re: Abläufe bei {lead.firma}"
        body = (
            f"{salutation}\n\n"
            f"ich komme kurz auf meine Nachricht von letzter Woche zurück. "
            f"Ist das Thema durchgängige Abläufe von Auftrag bis Buchhaltung für Sie aktuell?\n\n"
            f"Ein Gespräch von 20 Minuten genügt, um zu sehen, ob es passt.\n\n"
            f"Freundliche Grüsse\n{ABSENDER}\n\n---\n{OPT_OUT_NOTE}"
        )
    else:
        betreff = f"Re: Abläufe bei {lead.firma} – letzte Nachricht"
        body = (
            f"{salutation}\n\n"
            f"da ich nichts von Ihnen gehört habe, ist das Thema im Moment wohl nicht aktuell. Das ist völlig in Ordnung, "
            f"ich melde mich deshalb nicht mehr.\n\n"
            f"Falls es später spannend wird, freue ich mich über eine kurze Nachricht.\n\n"
            f"Freundliche Grüsse\n{ABSENDER}\n\n---\n{OPT_OUT_NOTE}"
        )
    return f"Betreff: {betreff}\n\n{body}"


class FollowupProcessResult(BaseModel):
    initial_drafts_created: int = 0
    followups_generated: list[Lead] = Field(default_factory=list)
    leads_marked_cold: list[Lead] = Field(default_factory=list)


def process_leads_drafts_and_followups(
    store: LeadStore,
    reference_date: date | str | None = None,
    dry_run: bool = False,
) -> FollowupProcessResult:
    """Verarbeitet alle freigegebenen und fälligen Leads im Wochenlauf."""
    all_leads = store.get_all_leads()
    result = FollowupProcessResult()
    max_nachfass = int(setting("nachfass", "max_nachfass", 2))

    for lead in all_leads:
        modified = False

        if lead.status == "freigegeben" and not lead.mail_entwurf:
            lead.mail_entwurf = generate_initial_draft(lead)
            result.initial_drafts_created += 1
            modified = True

        elif is_followup_due(lead, reference_date=reference_date):
            if lead.anzahl_nachfass < max_nachfass:
                lead.anzahl_nachfass += 1
                lead.status = "nachgefasst_1" if lead.anzahl_nachfass == 1 else "nachgefasst_2"
                lead.mail_entwurf = generate_followup_draft(lead, step=lead.anzahl_nachfass)
                result.followups_generated.append(lead)
            else:
                # Sequenz abgeschlossen, keine Antwort -> kalt
                lead.status = "kalt"
                result.leads_marked_cold.append(lead)
            modified = True

        if modified:
            lead.aktualisiert_am = datetime.now().astimezone().isoformat()
            if not dry_run:
                store.upsert_lead(lead)

    return result
