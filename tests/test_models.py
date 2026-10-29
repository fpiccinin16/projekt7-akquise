from p7_akquise.models import Lead, has_real_domain, name_key, normalize_domain


def test_normalize():
    assert normalize_domain("https://WWW.Firma.ch/ueber-uns?x=1") == "firma.ch"
    assert normalize_domain("name:Muster AG") == "name:muster ag"


def test_name_key():
    k = name_key("Müller & Co. AG", "St. Gallen")
    assert k == "name:müller-co-ag-st-gallen" and not has_real_domain(k) and has_real_domain("a.ch")


def test_plz_float_to_text():
    assert Lead(domain="a.ch", firma="A", plz=9000.0).plz == "9000"
