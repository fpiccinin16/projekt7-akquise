import base64

from fastapi.testclient import TestClient

from p7_akquise.dashboard.api import create_app
from p7_akquise.models import Lead
from tests.test_importer import ROWS, make_xlsx


def client(store):
    return TestClient(create_app(store=store))


def test_import_preview_and_import(store):
    c = client(store)
    payload = {"dateiname": "liste.xlsx", "inhalt_base64": "data:x;base64," + base64.b64encode(make_xlsx(ROWS)).decode()}
    prev = c.post("/api/import/vorschau", json=payload).json()
    assert prev["firmen"] == 3 and prev["ergebnis"]["spalten"]["firma"] == ["Firmenname"]
    assert store.get_all_leads() == []
    res = c.post("/api/import", json=payload).json()
    assert res["ergebnis"]["neu"] == 3
    kpis = c.get("/api/kpis").json()
    assert kpis["zu_bewerten"] == 3 and kpis["aus_adressliste"] == 3
    assert len(c.get("/api/leads", params={"kanal": "Adressliste"}).json()) == 3


def test_import_bad_file(store):
    r = client(store).post("/api/import", json={"dateiname": "x.csv", "inhalt_base64": base64.b64encode(b"a;b\n1;2").decode()})
    assert r.status_code == 422


def test_template_download(store):
    r = client(store).get("/api/import/vorlage.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_freigabe_and_answers(store):
    store.upsert_lead(Lead(domain="a.ch", firma="A", status="zur Prüfung", klasse="A", score=80))
    c = client(store)
    assert c.post("/api/leads/a.ch/freigabe", json={"entscheid": "nein"}).status_code == 422
    c.post("/api/leads/a.ch/freigabe", json={"entscheid": "ja"})
    c.post("/api/leads/a.ch/kontaktiert", json={})
    assert store.get_lead_by_domain("a.ch").status == "kontaktiert"
    c.post("/api/leads/a.ch/antwort", json={"antwort": "spaeter"})
    lead = store.get_lead_by_domain("a.ch")
    assert lead.status == "beobachten" and lead.wiedervorlage_am
    c.post("/api/leads/a.ch/antwort", json={"antwort": "termin"})
    assert store.get_lead_by_domain("a.ch").status == "termin"
    assert c.get("/api/kpis").json()["termine"] == 1


def test_detail_has_labels_and_export(store):
    store.upsert_lead(Lead(domain="a.ch", firma="A", segment="B", teilscores={"region": 10}))
    c = client(store)
    d = c.get("/api/leads/a.ch").json()
    assert d["kriterien_katalog"]["region"]["label"] == "Zielregion Ost-/Deutschschweiz"
    csv = c.get("/api/export.csv").content.decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("Lead ID;Firma") and ";Handel;" in csv
    assert "projekt7" in c.get("/").text
