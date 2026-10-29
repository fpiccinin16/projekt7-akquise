from datetime import date

from p7_akquise.models import Lead
from p7_akquise.steps.drafts import (
    add_workdays, generate_initial_draft, is_followup_due, process_leads_drafts_and_followups,
)


def test_initial_draft_segments():
    a = generate_initial_draft(Lead(domain="a.ch", firma="Alpha AG", segment="A", kontakt_name="Max Muster", kontakt_rolle="Geschäftsführer"))
    b = generate_initial_draft(Lead(domain="b.ch", firma="Beta AG", segment="B"))
    assert "Guten Tag Herr Muster" in a and "Produktion" in a
    assert "Handelsunternehmen" in b and "Guten Tag\n" in b
    for d in (a, b):
        assert "projekt7" in d and "Kein Interesse?" in d and len(d.split()) <= 120


def test_followup_weekly_sequence():
    lead = Lead(domain="a.ch", firma="A", status="kontaktiert", kontaktiert_am="2026-11-02")  # Montag
    assert not is_followup_due(lead, date(2026, 11, 6))
    assert is_followup_due(lead, date(2026, 11, 9))
    lead.anzahl_nachfass = 1
    assert not is_followup_due(lead, date(2026, 11, 13))
    assert is_followup_due(lead, date(2026, 11, 16))
    assert add_workdays(date(2026, 11, 6), 1) == date(2026, 11, 9)


def test_process_sequence_to_cold(store):
    store.upsert_lead(Lead(domain="f.ch", firma="F", status="freigegeben", klasse="A"))
    store.upsert_lead(Lead(domain="k.ch", firma="K", status="kontaktiert", kontaktiert_am="2026-11-02"))
    r = process_leads_drafts_and_followups(store, reference_date=date(2026, 11, 9))
    assert r.initial_drafts_created == 1 and len(r.followups_generated) == 1
    assert store.get_lead_by_domain("k.ch").status == "nachgefasst_1"
    process_leads_drafts_and_followups(store, reference_date=date(2026, 11, 16))
    assert store.get_lead_by_domain("k.ch").status == "nachgefasst_2"
    process_leads_drafts_and_followups(store, reference_date=date(2026, 11, 23))
    assert store.get_lead_by_domain("k.ch").status == "kalt"
