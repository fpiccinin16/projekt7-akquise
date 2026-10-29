import io

import openpyxl
import pytest
from openpyxl.styles import Font

from p7_akquise.models import ExclusionEntry, Lead
from p7_akquise.steps.importer import import_address_list, leads_to_evaluate, map_columns, parse_leads


def make_xlsx(rows, strike_row=None, title_rows=0):
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(title_rows):
        ws.append(["Adressliste Kuhn 2025"])
    for r in rows:
        ws.append(r)
    if strike_row is not None:
        for c in ws[strike_row + title_rows]:
            c.font = Font(strike=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [
    ["Firmenname", "Homepage", "PLZ", "Ort", "Vorname", "Nachname", "Funktion", "E-Mail", "Angeschrieben", "Bemerkung"],
    ["Muster Metallbau AG", "www.muster-metallbau.ch", 9000, "St. Gallen", "Max", "Muster", "Geschäftsführer", "max@muster-metallbau.ch", "ja", "kein Interesse"],
    ["Beispiel Handel GmbH", None, "8500", "Frauenfeld", "Anna", "Beispiel", "Leiterin Finanzen", "anna@beispiel-handel.ch", "ja", "in einem Jahr wieder"],
    ["Ohne Web AG", "", "9400", "Rorschach", None, None, None, "info@gmail.com", None, None],
    [None, "www.leer.ch", None, None, None, None, None, None, None, None],
]


def test_map_columns_synonyms():
    m = map_columns(ROWS[0])
    assert m["firma"] == [0] and m["website"] == [1] and m["plz"] == [2] and m["ort"] == [3]
    assert m["vorname"] == [4] and m["nachname"] == [5] and m["kontakt_rolle"] == [6]
    assert m["kontakt_email"] == [7] and m["historie"] == [8, 9]


def test_parse_xlsx_with_title_row_and_strike():
    leads, res = parse_leads(make_xlsx(ROWS, strike_row=2, title_rows=1), "liste.xlsx")
    assert res.zeilen_gelesen == 4 and res.ohne_firma == 1
    by = {l.firma: l for l in leads}
    m = by["Muster Metallbau AG"]
    assert m.domain == "muster-metallbau.ch" and m.plz == "9000" and m.kontakt_name == "Max Muster"
    assert "Bemerkung: kein Interesse" in m.historie and "durchgestrichen" in m.historie
    assert m.status == "zu bewerten" and m.quelle_kanal == "Adressliste"
    # Website aus E-Mail-Domain abgeleitet
    assert by["Beispiel Handel GmbH"].domain == "beispiel-handel.ch"
    # Gratis-Mail: keine Website, Ersatzschlüssel
    assert by["Ohne Web AG"].domain.startswith("name:") and res.ohne_website == 1


def test_parse_csv_semicolon():
    csv = "Firma;Website;PLZ Ort;Kontakt;Tel\nAlpha AG;alpha.ch;9000 St. Gallen;Herr Beat Alpha;071 111 11 11\n".encode("cp1252")
    leads, _ = parse_leads(csv, "a.csv")
    assert leads[0].plz == "9000" and leads[0].ort == "St. Gallen" and leads[0].kontakt_tel == "071 111 11 11"


def test_missing_firma_column_raises():
    with pytest.raises(ValueError):
        parse_leads(b"Website;Ort\nx.ch;Wil\n", "x.csv")


def test_import_into_store_merges_and_respects_exclusion(store):
    store.add_exclusion(ExclusionEntry(domain="beispiel-handel.ch", grund="Bestandskunde"))
    store.upsert_lead(Lead(domain="muster-metallbau.ch", firma="Muster Metallbau AG", status="verworfen", klasse="C"))
    res = import_address_list(make_xlsx(ROWS), "liste.xlsx", store)
    assert res.neu == 1 and res.aktualisiert == 1 and res.ausgeschlossen == 1
    m = store.get_lead_by_domain("muster-metallbau.ch")
    assert m.status == "zu bewerten" and "Adressliste" in m.quelle_kanal and m.kontakt_name == "Max Muster"
    pending = leads_to_evaluate(store, 10)
    assert [l.firma for l in pending][-1] == "Ohne Web AG"  # ohne Website zuletzt
