from p7_akquise.models import CriterionResult, Evidence
from p7_akquise.steps.scoring import ScoringEngine

URL = "https://firma.ch/ueber-uns"


def crit(name, wert=None, ok=True, beleg=True):
    belege = [Evidence(zitat="Beleg im Text", url=URL)] if beleg else []
    return CriterionResult(kriterium=name, erfuellt=ok, wert=wert, belege=belege)


def full_set(region="Ostschweiz"):
    return [
        crit("segment_produktion_handel", "Produktion"),
        crit("groesse_20_100", "ca. 45 MA"),
        crit("region", region),
        crit("deutschsprachig"),
        crit("erp_signal"),
        crit("wachstum_signal"),
        crit("prozesskomplexitaet"),
        crit("ansprechperson_zielrolle"),
        crit("direkter_kontaktweg"),
    ]


def test_model_sums_to_100():
    res = ScoringEngine().score_lead(full_set())
    assert res.score == 100
    assert res.klasse == "A"


def test_region_staged_points():
    eng = ScoringEngine()
    assert eng.score_lead(full_set("Deutschschweiz")).teilscores["region"] == 7
    assert eng.score_lead(full_set("Ostschweiz (SG)")).teilscores["region"] == 10
    assert eng.score_lead(full_set("andere")).teilscores["region"] == 0


def test_no_evidence_no_points():
    res = ScoringEngine().score_lead([crit("segment_produktion_handel", "Produktion", beleg=False)])
    assert res.score == 0
    assert res.teilscores["segment_produktion_handel"] == 0


def test_ko_criterion_forces_zero():
    res = ScoringEngine().score_lead(full_set() + [crit("wettbewerber")])
    assert res.score == 0 and res.klasse == "C" and res.is_ko
    assert "Wettbewerber" in res.begruendung


def test_thresholds():
    eng = ScoringEngine()
    assert eng.determine_class(70) == "A"
    assert eng.determine_class(69) == "B"
    assert eng.determine_class(50) == "B"
    assert eng.determine_class(49) == "C"


def test_begruendung_uses_labels():
    res = ScoringEngine().score_lead(full_set())
    assert "Produktions- oder Handelsunternehmen" in res.begruendung
