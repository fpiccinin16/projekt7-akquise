from p7_akquise.models import Lead
from p7_akquise.steps.discover import DiscoveredCandidate
from p7_akquise.steps.research import CompanyResearcher, segment_from_text, suggest_angebot, verify_evidence

HOME = "https://www.muster-metallbau.ch"
TEXT = (
    "Die Muster Metallbau AG in St. Gallen fertigt Blechteile. Rund 45 Mitarbeitende. "
    "Wir suchen eine Fachperson ERP und Informatik. Kontakt: Max Muster, Geschäftsführer, Tel. 071 000 00 00. "
    "[Über uns](/ueber-uns)"
)


def llm_answer(url=HOME):
    def k(name, wert, zitat):
        return {"kriterium": name, "erfuellt": True, "wert": wert, "belege": [{"zitat": zitat, "url": url}]}
    return {
        "firma": "Muster Metallbau AG", "ort": "St. Gallen", "kanton": "SG", "segment": "Produktion",
        "branche": "Metallbau", "groesse": "45 MA", "kontakt_name": "Max Muster", "kontakt_rolle": "Geschäftsführer",
        "kontakt_tel": "071 000 00 00", "kaufsignal": "Stelle ERP und Informatik",
        "kriterien": [
            k("segment_produktion_handel", "Produktion", "fertigt Blechteile"),
            k("groesse_20_100", "45 MA", "Rund 45 Mitarbeitende"),
            k("region", "Ostschweiz", "Muster Metallbau AG in St. Gallen"),
            k("deutschsprachig", "ja", "Wir suchen eine Fachperson"),
            k("erp_signal", "Stelle", "Fachperson ERP und Informatik"),
            k("prozesskomplexitaet", "Fertigung", "fertigt Blechteile"),
            k("ansprechperson_zielrolle", "GF", "Max Muster, Geschäftsführer"),
            k("direkter_kontaktweg", "Tel", "Tel. 071 000 00 00"),
            k("wachstum_signal", "Neubau", "Wir eröffnen 2027 ein neues Werk"),  # halluziniert
        ],
    }


def test_verify_evidence_tolerates_formatting():
    assert verify_evidence("Max Muster, Geschäftsführer", "**Max Muster** – Geschäftsführer")
    assert not verify_evidence("neues Werk", TEXT)


def test_research_scores_and_rejects_hallucination(fake_web, fake_llm):
    web = fake_web(pages={HOME: TEXT})
    r = CompanyResearcher(firecrawl_client=web, llm_client=fake_llm(llm_answer()))
    lead = r.research_candidate(DiscoveredCandidate(firma="Muster Metallbau AG", domain="muster-metallbau.ch", website=HOME, segment="A"))
    assert lead.teilscores["wachstum_signal"] == 0
    assert lead.score == 90 and lead.klasse == "A" and lead.status == "zur Prüfung"
    assert lead.segment == "A" and lead.angebot == "Abacus ERP mit Produktion/PPS"


def test_import_without_website_resolves_and_keeps_import_data(fake_web, fake_llm):
    web = fake_web(
        pages={HOME: TEXT},
        search_results={"Muster Metallbau": [
            {"url": "https://www.moneyhouse.ch/de/company/muster", "title": "Muster Metallbau AG - Moneyhouse"},
            {"url": "https://www.muster-metallbau.ch/kontakt", "title": "Muster Metallbau AG"},
        ]},
    )
    base = Lead(domain="name:muster-metallbau-ag-st-gallen", firma="Muster Metallbau AG", ort="St. Gallen",
                kontakt_name="Anna Import", status="zu bewerten", quelle_kanal="Adressliste", historie="Bemerkung: alt")
    r = CompanyResearcher(firecrawl_client=web, llm_client=fake_llm(llm_answer()))
    cand = DiscoveredCandidate(firma=base.firma, domain=base.domain, ort=base.ort, quelle_kanal=base.quelle_kanal)
    lead = r.research_candidate(cand, base=base)
    assert lead.domain == "muster-metallbau.ch" and lead.lead_id == base.lead_id
    assert lead.kontakt_name == "Anna Import" and lead.historie == "Bemerkung: alt"
    assert lead.klasse == "A"


def test_website_not_found_gives_error(fake_web, fake_llm):
    r = CompanyResearcher(firecrawl_client=fake_web(), llm_client=fake_llm({}))
    lead = r.research_candidate(DiscoveredCandidate(firma="Nirgends AG", domain="name:nirgends"))
    assert lead.status == "fehler" and "Website" in lead.begruendung


def test_helpers():
    assert segment_from_text("Handel") == "B" and segment_from_text("Produktion") == "A" and segment_from_text(None) == "C"
    assert suggest_angebot("B", "Grosshandel", None) == "Abacus Auftragsbearbeitung und Lager"
