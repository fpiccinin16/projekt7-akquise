from p7_akquise.models import ExclusionEntry, Lead, RejectedLead
from p7_akquise.steps.dedupe import filter_candidates
from p7_akquise.steps.discover import CandidateDiscoverer, DiscoveredCandidate


def cand(d):
    return DiscoveredCandidate(firma=d, domain=d)


def test_filters(store):
    store.add_exclusion(ExclusionEntry(domain="kunde.ch", grund="Bestandskunde"))
    store.add_rejected_lead(RejectedLead(domain="weg.ch", grund="kein Bedarf"))
    store.upsert_lead(Lead(domain="da.ch", firma="Da"))
    store.upsert_lead(Lead(domain="b.ch", firma="B", klasse="B", status="beobachten", wiedervorlage_am="2026-01-01"))
    res = filter_candidates([cand(x) for x in ("kunde.ch", "weg.ch", "da.ch", "b.ch", "neu.ch", "neu.ch")], store, "2026-11-02")
    assert [c.domain for c in res.candidates_to_research] == ["b.ch", "neu.ch"]
    assert len(res.b_leads_resurfaced) == 1


def test_discover_blocks_directories(fake_web):
    web = fake_web(companies={"Maschinenbau KMU Thurgau": [
        {"firma": "Echt AG", "website": "https://www.echt.ch", "ort": "Arbon"},
        {"firma": "Moneyhouse", "website": "https://www.moneyhouse.ch/x"},
        {"firma": "Echt AG", "website": "https://echt.ch/kontakt"},
    ]})
    res = CandidateDiscoverer(firecrawl_client=web).discover_all(max_candidates=10)
    assert [c.domain for c in res.candidates] == ["echt.ch"] and res.candidates[0].segment == "A"
