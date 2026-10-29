from p7_akquise.models import Lead, ProtocolEntry, RejectedLead


def test_roundtrip_and_update_by_lead_id(store):
    lead = Lead(domain="name:ohne-web-ag-rorschach", firma="Ohne Web AG", plz="9400", status="zu bewerten",
                teilscores={"region": 10}, quellen=["https://a.ch"])
    store.upsert_lead(lead)
    got = store.get_lead_by_domain("name:ohne-web-ag-rorschach")
    assert got.lead_id == lead.lead_id and got.plz == "9400" and got.teilscores == {"region": 10}

    # Domain ändert sich nach der Website-Ermittlung: gleiche Zeile wird aktualisiert
    got.domain = "ohneweb.ch"
    store.upsert_lead(got)
    leads = store.get_all_leads()
    assert len(leads) == 1 and leads[0].domain == "ohneweb.ch"


def test_manual_fields_protected(store):
    store.upsert_lead(Lead(domain="a.ch", firma="A", kommentar_berater="wichtig"))
    store.upsert_lead(Lead(domain="a.ch", firma="A neu"))
    assert store.get_lead_by_domain("a.ch").kommentar_berater == "wichtig"


def test_feedback_reject_and_remove(store):
    store.upsert_lead(Lead(domain="a.ch", firma="A", status="zur Prüfung", klasse="A", score=80))
    store.update_feedback("a.ch", "nein", verwerfungsgrund="hat Abacus")
    assert store.get_lead_by_domain("a.ch").status == "verworfen"
    assert store.get_rejected_leads()[0].grund == "hat Abacus"
    lead = store.get_lead_by_domain("a.ch")
    assert store.remove_lead(lead.lead_id) and store.get_all_leads() == []


def test_protocol(store):
    store.add_protocol_entry(ProtocolEntry(lauf_id="r1", anzahl_import_bewertet=5))
    assert store.get_protocol_entries()[0].anzahl_import_bewertet == 5
    store.add_rejected_lead(RejectedLead(domain="b.ch", grund="Klasse C"))
    assert store.get_rejected_leads()[0].domain == "b.ch"
