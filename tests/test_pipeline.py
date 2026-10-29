from datetime import date

from p7_akquise.models import Lead
from p7_akquise.pipeline import AkquisePipeline
from tests.test_research import HOME, TEXT, llm_answer

NEU = "https://www.neu-handel.ch"
NEU_TEXT = "Neu Handel AG, Grosshandel in Wil. 30 Mitarbeitende."


def test_weekly_run_imports_and_web(store, fake_web, fake_llm):
    store.upsert_lead(Lead(domain="muster-metallbau.ch", firma="Muster Metallbau AG", website=HOME,
                           status="zu bewerten", quelle_kanal="Adressliste"))
    web = fake_web(
        pages={HOME: TEXT, NEU: NEU_TEXT},
        companies={"Grosshandel KMU St. Gallen": [{"firma": "Neu Handel AG", "website": NEU, "ort": "Wil"},
                                                  {"firma": "Verzeichnis", "website": "https://www.local.ch/x"}]},
    )
    neu_answer = {"firma": "Neu Handel AG", "segment": "Handel", "kriterien": [
        {"kriterium": "segment_produktion_handel", "erfuellt": True, "wert": "Handel",
         "belege": [{"zitat": "Grosshandel in Wil", "url": NEU}]}]}
    llm = fake_llm(by_domain={"muster-metallbau.ch": llm_answer(), "neu-handel.ch": neu_answer})
    p = AkquisePipeline(store=store, firecrawl_client=web, llm_client=llm, kandidaten_pro_lauf=5, import_pro_lauf=10)
    res = p.run_weekly(reference_date=date(2026, 11, 2))

    assert res["import_bewertet"] == 1 and res["neue_a_leads"] == 1
    assert store.get_lead_by_domain("muster-metallbau.ch").status == "zur Prüfung"
    neu = store.get_lead_by_domain("neu-handel.ch")
    assert neu.klasse == "C" and neu.status == "verworfen" and neu.segment == "B"
    assert store.get_lead_by_domain("local.ch") is None
    assert store.get_protocol_entries()[0].anzahl_import_bewertet == 1


def test_duplicate_after_website_resolution_is_merged(store, fake_web, fake_llm):
    store.upsert_lead(Lead(domain="muster-metallbau.ch", firma="Muster Metallbau AG", website=HOME, status="kontaktiert"))
    store.upsert_lead(Lead(domain="name:muster-metallbau-ag", firma="Muster Metallbau AG", status="zu bewerten",
                           quelle_kanal="Adressliste", historie="Bemerkung: alt"))
    web = fake_web(pages={HOME: TEXT}, search_results={"Muster": [{"url": HOME, "title": "Muster Metallbau AG"}]})
    p = AkquisePipeline(store=store, firecrawl_client=web, llm_client=fake_llm(llm_answer()), kandidaten_pro_lauf=0)
    p.run_weekly()
    leads = store.get_all_leads()
    assert len(leads) == 1 and leads[0].status == "kontaktiert" and leads[0].historie == "Bemerkung: alt"


def test_dry_run_writes_nothing(store, fake_web, fake_llm):
    store.upsert_lead(Lead(domain="muster-metallbau.ch", firma="M", website=HOME, status="zu bewerten"))
    p = AkquisePipeline(store=store, firecrawl_client=fake_web(pages={HOME: TEXT}), llm_client=fake_llm(llm_answer()),
                        kandidaten_pro_lauf=0, dry_run=True)
    p.run_weekly()
    assert store.get_lead_by_domain("muster-metallbau.ch").status == "zu bewerten"
