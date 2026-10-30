from datetime import date
from fastapi.testclient import TestClient
import pytest

from p7_akquise.dashboard.api import create_app
from p7_akquise.models import Lead
from p7_akquise.pipeline import AkquisePipeline
from tests.test_research import HOME, TEXT, llm_answer


def client(store):
    return TestClient(create_app(store=store))


def test_patch_updates_field_and_writes_change_protocol_and_locks(store):
    store.upsert_lead(Lead(domain="muster.ch", firma="Muster AG", ort="St. Gallen", score=80, klasse="A", status="zur Prüfung"))
    c = client(store)
    lead_before = store.get_lead_by_domain("muster.ch")

    res = c.patch(f"/api/leads/{lead_before.lead_id}", json={
        "felder": {"ort": "Rorschach", "kontakt_name": "Hans Muster"},
        "aktualisiert_am": lead_before.aktualisiert_am,
        "benutzer": "KAM",
        "grund": "Telefonische Auskunft",
    })
    assert res.status_code == 200
    data = res.json()["lead"]
    assert data["ort"] == "Rorschach"
    assert data["kontakt_name"] == "Hans Muster"
    assert "ort" in data["gesperrte_felder"]
    assert "kontakt_name" in data["gesperrte_felder"]

    # Änderungsprotokoll prüfen
    entries = store.get_change_entries(lead_before.lead_id)
    fields_changed = {e.feld: e for e in entries}
    assert "ort" in fields_changed
    assert fields_changed["ort"].alt == "St. Gallen"
    assert fields_changed["ort"].neu == "Rorschach"
    assert fields_changed["ort"].grund == "Telefonische Auskunft"
    assert "kontakt_name" in fields_changed

    # CSV-Export aller Änderungen
    csv_res = c.get("/api/aenderungen.csv")
    assert csv_res.status_code == 200
    assert "Rorschach" in csv_res.content.decode("utf-8-sig")


def test_patch_409_conflict_on_stale_update_and_domain_duplicate(store):
    store.upsert_lead(Lead(domain="firma1.ch", firma="Firma 1 AG", website="https://firma1.ch"))
    store.upsert_lead(Lead(domain="firma2.ch", firma="Firma 2 AG", website="https://firma2.ch"))
    c = client(store)
    l1 = store.get_lead_by_domain("firma1.ch")

    # 1. 409 bei veraltetem aktualisiert_am
    res_stale = c.patch(f"/api/leads/{l1.lead_id}", json={
        "felder": {"firma": "Firma 1 Neu"},
        "aktualisiert_am": "2020-01-01T00:00:00+00:00",
    })
    assert res_stale.status_code == 409

    # 2. 409 bei Domain-Dublette
    res_dup = c.patch(f"/api/leads/{l1.lead_id}", json={
        "felder": {"website": "https://www.firma2.ch"},
        "aktualisiert_am": l1.aktualisiert_am,
    })
    assert res_dup.status_code == 409
    assert "Dublette" in str(res_dup.json())


def test_patch_422_validations(store):
    store.upsert_lead(Lead(domain="valid.ch", firma="Valid AG", status="zur Prüfung", score=60, klasse="B"))
    c = client(store)
    l = store.get_lead_by_domain("valid.ch")

    # Ungültige E-Mail
    r_mail = c.patch(f"/api/leads/{l.lead_id}", json={"felder": {"kontakt_email": "invalid-email"}})
    assert r_mail.status_code == 422
    assert "kontakt_email" in r_mail.json()["detail"]

    # Teilscore über Maximum (segment_produktion_handel hat max. 10 Punkte im scoring_model.yaml)
    r_ts = c.patch(f"/api/leads/{l.lead_id}", json={"felder": {"teilscores": {"segment_produktion_handel": 999}}})
    assert r_ts.status_code == 422
    assert "teilscores" in r_ts.json()["detail"]

    # Score direkt geändert ohne Grund
    r_score = c.patch(f"/api/leads/{l.lead_id}", json={"felder": {"score": 85}})
    assert r_score.status_code == 422
    assert "grund" in r_score.json()["detail"].lower()

    # Status 'verworfen' ohne verwerfungsgrund
    r_verworfen = c.patch(f"/api/leads/{l.lead_id}", json={"felder": {"status": "verworfen"}})
    assert r_verworfen.status_code == 422
    assert "verwerfungsgrund" in r_verworfen.json()["detail"].lower()


def test_patch_teilscores_recalculates_score_and_class(store):
    store.upsert_lead(Lead(domain="calc.ch", firma="Calc AG", score=30, klasse="C", status="beobachten"))
    c = client(store)
    l = store.get_lead_by_domain("calc.ch")

    # Setze Teilscores in Summe = 75 (Klasse A ab 70)
    new_ts = {
        "segment_produktion_handel": 20,
        "groesse_20_100": 15,
        "region": 10,
        "deutschsprachig": 5,
        "erp_signal": 15,
        "prozesskomplexitaet": 10,
    }
    res = c.patch(f"/api/leads/{l.lead_id}", json={
        "felder": {"teilscores": new_ts},
        "aktualisiert_am": l.aktualisiert_am,
    })
    assert res.status_code == 200
    updated = res.json()["lead"]
    assert updated["score"] == 75
    assert updated["klasse"] == "A"
    assert updated["score_manuell"] is True


def test_neu_bewerten_resets_only_evaluation_locks(store):
    store.upsert_lead(Lead(
        domain="locked.ch",
        firma="Locked AG",
        ort="St. Gallen",
        score=90,
        klasse="A",
        status="freigegeben",
        score_manuell=True,
        gesperrte_felder=["firma", "ort", "score", "klasse", "teilscores", "begruendung"]
    ))
    c = client(store)
    l = store.get_lead_by_domain("locked.ch")

    res = c.post(f"/api/leads/{l.lead_id}/neu-bewerten")
    assert res.status_code == 200
    data = res.json()["lead"]
    assert data["status"] == "zu bewerten"
    assert data["score_manuell"] is False
    # Stammdatensperren (firma, ort) bleiben gesperrt, Bewertungssperren sind weg
    assert "firma" in data["gesperrte_felder"]
    assert "ort" in data["gesperrte_felder"]
    assert "score" not in data["gesperrte_felder"]
    assert "klasse" not in data["gesperrte_felder"]
    assert "teilscores" not in data["gesperrte_felder"]


def test_sperre_aufheben(store):
    store.upsert_lead(Lead(domain="lock2.ch", firma="L2", gesperrte_felder=["ort", "branche"]))
    c = client(store)
    l = store.get_lead_by_domain("lock2.ch")

    res = c.post(f"/api/leads/{l.lead_id}/sperre-aufheben", json={"feld": "ort"})
    assert res.status_code == 200
    assert "ort" not in res.json()["lead"]["gesperrte_felder"]
    assert "branche" in res.json()["lead"]["gesperrte_felder"]


def test_pipeline_run_respects_locked_fields_and_manual_score(store, fake_web, fake_llm):
    # Vorhandener Lead mit manuell gesperrtem Ort und manuellem Score
    store.upsert_lead(Lead(
        domain="muster-metallbau.ch",
        firma="Muster Metallbau AG",
        website=HOME,
        ort="Manuell Fixiert",
        status="beobachten",
        score=88,
        klasse="A",
        score_manuell=True,
        gesperrte_felder=["ort"],
        quelle_kanal="Adressliste"
    ))
    web = fake_web(pages={HOME: TEXT})
    llm = fake_llm(llm_answer())
    p = AkquisePipeline(store=store, firecrawl_client=web, llm_client=llm, kandidaten_pro_lauf=0)
    p.run_weekly(reference_date=date(2026, 11, 2))

    lead = store.get_lead_by_domain("muster-metallbau.ch")
    # Ort blieb manuell fixiert (nicht vom Webseiten-Text überschrieben)
    assert lead.ort == "Manuell Fixiert"
    assert lead.score == 88
    assert lead.klasse == "A"
    assert lead.score_manuell is True
    # Status wurde nicht automatisch überschrieben
    assert lead.status == "beobachten"


def test_counts_endpoint(store):
    store.upsert_lead(Lead(domain="a.ch", firma="A", status="zur Prüfung", klasse="A", segment="A", quelle_kanal="KI-Websuche"))
    store.upsert_lead(Lead(domain="b.ch", firma="B", status="beobachten", klasse="B", segment="B", quelle_kanal="Adressliste"))
    c = client(store)

    res = c.get("/api/counts")
    assert res.status_code == 200
    counts = res.json()
    assert counts["total"] == 2
    assert counts["status"]["zur Prüfung"] == 1
    assert counts["status"]["beobachten"] == 1
    assert counts["klasse"]["A"] == 1
    assert counts["klasse"]["B"] == 1
    assert counts["segment"]["A"] == 1
    assert counts["segment"]["B"] == 1
    assert counts["quelle_kanal"]["KI-Websuche"] == 1
    assert counts["quelle_kanal"]["Adressliste"] == 1


def test_dashboard_html_dom_counts_bar_and_editing_elements(store):
    c = client(store)
    res = c.get("/")
    assert res.status_code == 200
    html = res.text

    # DOM Elemente für Zählerzeile und Filter-Reset
    assert 'id="table-counts-bar"' in html
    assert 'id="leads-counter"' in html
    assert 'id="reset-filter-btn"' in html
    assert 'resetFilters()' in html

    # Zähler-Logik im Skript
    assert "Angezeigt: ${count} von ${total} Leads" in html or "Angezeigt: " in html
    assert "updateCountsBar" in html
    assert "updateFilterDropdowns" in html
    assert "/api/counts" in html

    # Bearbeitungsmodus, Sperre und Änderungsverlauf
    assert "startEditing" in html
    assert "saveEdit" in html
    assert "unlockField" in html
    assert "triggerNeuBewerten" in html
    assert 'id="aenderungen-box"' in html
    assert "/api/leads/${leadId}/sperre-aufheben" in html or "sperre-aufheben" in html
    assert "/api/leads/${leadId}/neu-bewerten" in html or "neu-bewerten" in html
    assert "/api/leads/${leadId}/aenderungen" in html or "aenderungen" in html

    # Massenlöschung
    assert 'id="bulk-actions"' in html
    assert 'id="select-all-leads"' in html
    assert "bulkDeleteSelected" in html
    assert "toggleSelectAll" in html
    assert "batch-delete" in html


def test_aenderungen_csv_export(store):
    c = client(store)
    lead = Lead(domain="csvtest.ch", firma="CSV Test AG", score=50, klasse="B")
    store.upsert_lead(lead)
    l = store.get_lead_by_domain("csvtest.ch")

    c.patch(f"/api/leads/{l.lead_id}", json={
        "felder": {"ort": "Winterthur"},
        "aktualisiert_am": l.aktualisiert_am,
        "benutzer": "KAM",
        "grund": "Telefonische Bestätigung"
    })

    res = c.get("/api/aenderungen.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    content = res.content.decode("utf-8-sig")
    assert "Lead ID;Unternehmen;Feld;Alter Wert;Neuer Wert;Benutzer;Zeitpunkt;Grund" in content
    assert "Winterthur" in content
    assert "Telefonische Bestätigung" in content


def test_batch_delete_leads(store):
    store.upsert_lead(Lead(domain="d1.ch", firma="D1 AG", status="zur Prüfung", score=60, klasse="B"))
    store.upsert_lead(Lead(domain="d2.ch", firma="D2 AG", status="beobachten", score=40, klasse="C"))
    store.upsert_lead(Lead(domain="keep.ch", firma="Keep AG", status="freigegeben", score=80, klasse="A"))

    l1 = store.get_lead_by_domain("d1.ch")
    l2 = store.get_lead_by_domain("d2.ch")
    c = client(store)

    # 422 wenn leer
    assert c.post("/api/leads/batch-delete", json={"lead_ids": []}).status_code == 422

    # Massenlöschung
    res = c.post("/api/leads/batch-delete", json={
        "lead_ids": [l1.lead_id, l2.lead_id],
        "grund": "Massenlöschung Test"
    })
    assert res.status_code == 200
    assert res.json()["deleted_count"] == 2

    # d1 und d2 gelöscht, keep noch da
    assert store.get_lead_by_domain("d1.ch") is None
    assert store.get_lead_by_domain("d2.ch") is None
    assert store.get_lead_by_domain("keep.ch") is not None

    # In Verworfen eingetragen
    rejected = store.get_rejected_leads()
    rej_domains = {r.domain for r in rejected}
    assert "d1.ch" in rej_domains
    assert "d2.ch" in rej_domains


