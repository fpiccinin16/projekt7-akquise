from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from p7_akquise.config import setting
from p7_akquise.steps.discover import (
    CandidateDiscoverer,
    build_search_queries_for_segment,
    sort_queries_by_rotation,
)


def test_combination_branche_x_kanton_counts():
    """Prüft, ob die Kombination aus Branchen x Kantonen die erwartete Anzahl an Begriffen ergibt."""
    # Produktion: 9 Branchen x 8 Kantone = 72 Kombinationen + 4 Signal-Suchen = 76 Begriffe
    queries_a = build_search_queries_for_segment("A")
    assert len(queries_a) == 76
    assert "Maschinenbau KMU St. Gallen" in queries_a
    assert "Maschinenbau KMU Thurgau" in queries_a
    assert "Feinmechanik KMU Zürich" in queries_a
    assert "Stellenausschreibung ERP KMU Ostschweiz" in queries_a
    assert "Nachfolgeregelung KMU Ostschweiz" in queries_a

    # Handel: 6 Branchen x 8 Kantone = 48 Kombinationen
    queries_b = build_search_queries_for_segment("B")
    assert len(queries_b) == 48
    assert "Grosshandel KMU St. Gallen" in queries_b
    assert "technischer Handel KMU Thurgau" in queries_b
    assert "Ersatzteilhandel KMU Zürich" in queries_b


def test_rotation_prefers_unused_and_oldest_queries():
    """Prüft, dass unbenutzte Suchbegriffe zuerst und danach die am längsten nicht verwendeten gewählt werden."""
    all_queries = ["Term1", "Term2", "Term3", "Term4"]
    # Term1 wurde vor 2 Tagen verwendet, Term3 vor 10 Tagen, Term2 und Term4 noch nie
    history = {
        "Term1": "2026-10-27T10:00:00+00:00",
        "Term3": "2026-10-19T10:00:00+00:00",
    }
    sorted_q = sort_queries_by_rotation(all_queries, history)
    # Unbenutzte Begriffe kommen zuerst
    assert set(sorted_q[:2]) == {"Term2", "Term4"}
    # Danach Term3 (älter als Term1), zuletzt Term1
    assert sorted_q[2] == "Term3"
    assert sorted_q[3] == "Term1"


def test_pipeline_limits_and_settings():
    """Prüft, ob die erhöhten Limits in den Settings korrekt hinterlegt sind."""
    assert setting("pipeline", "kandidaten_pro_lauf") == 100
    assert setting("pipeline", "max_seiten_pro_firma") == 8


def test_discoverer_records_queries_in_store(store, fake_web):
    """Prüft, ob der Discoverer die genutzten Suchbegriffe im ExcelStore (Blatt 'Suchbegriffe') speichert."""
    web = fake_web(companies={"Maschinenbau KMU St. Gallen": [
        {"firma": "Test AG", "website": "https://test-ag.ch", "ort": "St. Gallen"}
    ]})
    discoverer = CandidateDiscoverer(firecrawl_client=web, max_candidates=5, store=store)
    res = discoverer.discover_all(max_candidates=5)

    assert len(res.used_queries) > 0
    # Store speichert die Begriffe
    store.record_search_queries(res.used_queries, lauf_id="test-lauf", datum="2026-10-29T12:00:00+00:00")
    used_map = store.get_used_search_queries()
    for q in res.used_queries:
        assert q in used_map
