"""Schritt C4: Abgleich und Deduplizierung gegen Bestand, Ausschluss und Verworfen."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from p7_akquise.models import Lead, normalize_domain
from p7_akquise.steps.discover import DiscoveredCandidate
from p7_akquise.storage.base import LeadStore


class DeduplicationResult(BaseModel):
    """Ergebnis des Kandidaten-Abgleichs."""
    candidates_to_research: list[DiscoveredCandidate] = Field(default_factory=list)
    dropped_exclusion: list[tuple[str, str]] = Field(default_factory=list)  # (domain, grund)
    dropped_rejected: list[tuple[str, str]] = Field(default_factory=list)   # (domain, grund)
    dropped_existing: list[tuple[str, str]] = Field(default_factory=list)   # (domain, status)
    b_leads_resurfaced: list[DiscoveredCandidate] = Field(default_factory=list)
    total_input: int = 0
    total_output: int = 0


def filter_candidates(
    candidates: list[DiscoveredCandidate],
    store: LeadStore,
    reference_date: date | str | None = None,
) -> DeduplicationResult:
    """Filtert Kandidaten anhand von Ausschlussliste, Verworfen-Liste und Bestand.
    
    Ausnahme: Fällige B-Leads (wiedervorlage_am <= reference_date) werden erneut recherchiert.
    """
    if reference_date is None:
        ref_str = date.today().strftime("%Y-%m-%d")
    elif isinstance(reference_date, str):
        ref_str = reference_date
    else:
        ref_str = reference_date.strftime("%Y-%m-%d")

    # 1. Bestände aus Store laden
    exclusion_map = {normalize_domain(e.domain): e.grund for e in store.get_exclusion_list()}
    rejected_map = {normalize_domain(r.domain): r.grund for r in store.get_rejected_leads()}
    existing_map = {normalize_domain(l.domain): l for l in store.get_all_leads()}

    to_research: list[DiscoveredCandidate] = []
    dropped_ex: list[tuple[str, str]] = []
    dropped_rej: list[tuple[str, str]] = []
    dropped_ext: list[tuple[str, str]] = []
    resurfaced_b: list[DiscoveredCandidate] = []

    seen_in_batch: set[str] = set()

    for cand in candidates:
        domain = normalize_domain(cand.domain)
        if not domain or domain in seen_in_batch:
            continue
        seen_in_batch.add(domain)

        # 1. Prüfung Ausschlussliste (Bestandskunde / Wettbewerber / Opt-out)
        if domain in exclusion_map:
            dropped_ex.append((domain, exclusion_map[domain]))
            continue

        # 2. Prüfung Verworfen-Blatt (C-Leads / abgelehnte Leads)
        if domain in rejected_map:
            dropped_rej.append((domain, rejected_map[domain] or ""))
            continue

        # 3. Prüfung Bestand in Leads
        if domain in existing_map:
            existing_lead = existing_map[domain]
            # B-Leads mit fälliger Wiedervorlage erneut zulassen
            is_b_due = (
                existing_lead.klasse == "B"
                and existing_lead.status == "beobachten"
                and existing_lead.wiedervorlage_am is not None
                and existing_lead.wiedervorlage_am <= ref_str
            )

            if is_b_due:
                to_research.append(cand)
                resurfaced_b.append(cand)
            else:
                dropped_ext.append((domain, f"Bereits vorhanden (Klasse {existing_lead.klasse}, Status {existing_lead.status})"))
            continue

        # Neuer, qualifizierter Kandidat
        to_research.append(cand)

    return DeduplicationResult(
        candidates_to_research=to_research,
        dropped_exclusion=dropped_ex,
        dropped_rejected=dropped_rej,
        dropped_existing=dropped_ext,
        b_leads_resurfaced=resurfaced_b,
        total_input=len(candidates),
        total_output=len(to_research),
    )
