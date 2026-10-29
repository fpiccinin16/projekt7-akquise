"""Import bestehender Adresslisten (Excel oder CSV) als Leads zur Neubewertung.

- Spalten werden über Synonyme automatisch zugeordnet (Firma, Website, Ort, Kontakt, E-Mail …).
- Jede Firma wird als Lead mit Status «zu bewerten» angelegt und im nächsten Lauf neu recherchiert
  und bewertet. Frühere Bemerkungen und Häkchen aus der Liste bleiben als «Historie» sichtbar.
- Fehlt die Website, wird sie aus der E-Mail-Domain abgeleitet oder später per Websuche ermittelt.
- Durchgestrichene Zeilen (bisherige Praxis für «kein Interesse») werden in der Historie vermerkt.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Any

import openpyxl
from pydantic import BaseModel, Field

from p7_akquise.models import KANAL_IMPORT, Lead, has_real_domain, name_key, normalize_domain
from p7_akquise.storage.base import LeadStore

logger = logging.getLogger("p7_akquise.importer")

FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "bluewin.ch", "gmx.ch", "gmx.net", "gmx.de", "hotmail.com", "hotmail.ch",
    "outlook.com", "live.com", "yahoo.com", "yahoo.de", "icloud.com", "me.com", "sunrise.ch", "hispeed.ch",
    "swissonline.ch", "proton.me", "protonmail.com", "web.de", "t-online.de", "freesurf.ch", "datazug.ch",
}

# Feld -> Synonyme (normalisiert: Kleinbuchstaben, nur a-z0-9äöü)
FIELD_SYNONYMS: dict[str, list[str]] = {
    "firma": ["firma", "firmenname", "unternehmen", "unternehmensname", "company", "organisation", "organization",
              "kunde", "kundenname", "name1", "firmabezeichnung", "betrieb"],
    "website": ["website", "webseite", "homepage", "url", "internet", "www", "web", "internetadresse", "domain"],
    "strasse": ["strasse", "straße", "adresse", "address", "strassehausnummer", "strassenr", "adresszeile1"],
    "plz": ["plz", "postleitzahl", "zip", "postcode", "npa"],
    "ort": ["ort", "stadt", "city", "ortschaft", "gemeinde", "standort", "sitz"],
    "plz_ort": ["plzort", "plzundort", "plzort1"],
    "kanton": ["kanton", "canton", "region"],
    "branche": ["branche", "industry", "sektor", "noga", "tätigkeit", "taetigkeit"],
    "groesse": ["mitarbeitende", "mitarbeiter", "ma", "anzahlmitarbeiter", "anzahlmitarbeitende", "employees",
                "grösse", "groesse", "unternehmensgrösse", "fte"],
    "kontakt_name": ["kontakt", "kontaktperson", "ansprechperson", "ansprechpartner", "kontaktname", "name",
                     "person", "entscheider"],
    "vorname": ["vorname", "firstname"],
    "nachname": ["nachname", "lastname", "familienname"],
    "anrede": ["anrede", "salutation"],
    "kontakt_rolle": ["funktion", "position", "rolle", "titel", "jobtitle", "job"],
    "kontakt_email": ["email", "emailadresse", "mail", "emailkontakt"],
    "kontakt_tel": ["telefon", "tel", "phone", "telefonnummer", "telnr", "mobile", "natel", "direktwahl"],
    "historie": ["bemerkung", "bemerkungen", "kommentar", "notiz", "notizen", "status", "interesse", "ergebnis",
                 "angeschrieben", "versendet", "versand", "telefoniert", "rückmeldung", "rueckmeldung", "remarks"],
}


def _norm(header: Any) -> str:
    return re.sub(r"[^a-z0-9äöü]", "", str(header or "").lower())


def map_columns(headers: list[Any]) -> dict[str, list[int]]:
    """Ordnet Spaltenindizes den Lead-Feldern zu. Historie darf mehrere Spalten haben."""
    mapping: dict[str, list[int]] = {}
    used: set[int] = set()
    normed = [_norm(h) for h in headers]
    # 1. exakte Treffer, 2. Teiltreffer
    for exact in (True, False):
        for field, syns in FIELD_SYNONYMS.items():
            if field in mapping and field != "historie":
                continue
            for idx, h in enumerate(normed):
                if not h or idx in used:
                    continue
                hit = h in syns if exact else any(len(s) >= 4 and s in h for s in syns)
                if hit:
                    mapping.setdefault(field, []).append(idx)
                    used.add(idx)
                    if field != "historie":
                        break
    return mapping


def _detect_header_row(rows: list[list[Any]]) -> int:
    best_idx, best_hits = 0, -1
    for i, row in enumerate(rows[:10]):
        hits = len(map_columns(row))
        if hits > best_hits:
            best_idx, best_hits = i, hits
    return best_idx


def _cell(row: list[Any], idx: int) -> str:
    if idx >= len(row) or row[idx] is None:
        return ""
    val = row[idx]
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    if isinstance(val, bool):
        return "ja" if val else ""
    return str(val).strip()


def read_rows(content: bytes, filename: str) -> tuple[list[list[Any]], list[bool]]:
    """Liest Zeilen aus .xlsx/.xlsm oder .csv. Gibt Zeilen und je Zeile ein Durchgestrichen-Flag zurück."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.worksheets[0]
        rows: list[list[Any]] = []
        struck: list[bool] = []
        for row in ws.iter_rows():
            rows.append([c.value for c in row])
            struck.append(any(getattr(c.font, "strike", False) and c.value not in (None, "") for c in row))
        wb.close()
        return rows, struck
    if name.endswith((".csv", ".txt")):
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text or ""
        sample = text[:4000]
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
        return rows, [False] * len(rows)
    raise ValueError("Nur .xlsx, .xlsm oder .csv werden unterstützt (alte .xls-Dateien bitte als .xlsx speichern).")


def _website_from(website: str, email: str) -> str | None:
    w = website.strip()
    if w and w.lower() not in ("-", "n/a", "keine"):
        return w if w.lower().startswith("http") else f"https://{w.lstrip('/')}"
    if "@" in email:
        dom = normalize_domain(email.split("@", 1)[1])
        if dom and dom not in FREE_MAIL_DOMAINS:
            return f"https://{dom}"
    return None


class ImportResult(BaseModel):
    datei: str
    zeilen_gelesen: int = 0
    neu: int = 0
    aktualisiert: int = 0
    ohne_firma: int = 0
    ausgeschlossen: int = 0
    ohne_website: int = 0
    durchgestrichen: int = 0
    spalten: dict[str, list[str]] = Field(default_factory=dict)
    nicht_zugeordnet: list[str] = Field(default_factory=list)


def parse_leads(content: bytes, filename: str) -> tuple[list[Lead], ImportResult]:
    """Wandelt eine Adressliste in Leads mit Status «zu bewerten» um (ohne zu speichern)."""
    rows, struck = read_rows(content, filename)
    result = ImportResult(datei=filename)
    if not rows:
        return [], result

    h_idx = _detect_header_row(rows)
    headers = rows[h_idx]
    mapping = map_columns(headers)
    if "firma" not in mapping:
        raise ValueError("Keine Spalte für den Firmennamen gefunden (z. B. «Firma», «Unternehmen»).")

    result.spalten = {f: [str(headers[i]) for i in idxs] for f, idxs in mapping.items()}
    mapped = {i for idxs in mapping.values() for i in idxs}
    result.nicht_zugeordnet = [str(h) for i, h in enumerate(headers) if h not in (None, "") and i not in mapped]

    def get(row: list[Any], field: str) -> str:
        return " ".join(v for v in (_cell(row, i) for i in mapping.get(field, [])) if v).strip()

    leads: dict[str, Lead] = {}
    heute = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for r_idx in range(h_idx + 1, len(rows)):
        row = rows[r_idx]
        if not any(v not in (None, "") for v in row):
            continue
        result.zeilen_gelesen += 1
        firma = get(row, "firma")
        if not firma:
            result.ohne_firma += 1
            continue

        plz, ort = get(row, "plz"), get(row, "ort")
        if not ort and mapping.get("plz_ort"):
            m = re.match(r"^\s*(\d{4,5})\s+(.+)$", get(row, "plz_ort"))
            if m:
                plz, ort = m.group(1), m.group(2)
            else:
                ort = get(row, "plz_ort")

        kontakt = get(row, "kontakt_name") or " ".join(x for x in (get(row, "vorname"), get(row, "nachname")) if x)
        anrede = get(row, "anrede")
        if kontakt and anrede and anrede.lower() in ("herr", "frau"):
            kontakt = f"{anrede} {kontakt}"
        email = get(row, "kontakt_email")
        website = _website_from(get(row, "website"), email)

        historie_teile = []
        for i in mapping.get("historie", []):
            val = _cell(row, i)
            if val:
                historie_teile.append(f"{headers[i]}: {val}")
        if r_idx < len(struck) and struck[r_idx]:
            historie_teile.append("in der Liste durchgestrichen (früher: kein Interesse)")
            result.durchgestrichen += 1

        domain = normalize_domain(website) if website else name_key(firma, ort)
        if not website:
            result.ohne_website += 1

        lead = Lead(
            domain=domain,
            firma=firma,
            website=website,
            strasse=get(row, "strasse") or None,
            plz=plz or None,
            ort=ort or None,
            kanton=get(row, "kanton") or None,
            branche=get(row, "branche") or None,
            groesse=get(row, "groesse") or None,
            kontakt_name=kontakt or None,
            kontakt_rolle=get(row, "kontakt_rolle") or None,
            kontakt_email=email or None,
            kontakt_tel=get(row, "kontakt_tel") or None,
            quelle_kanal=KANAL_IMPORT,
            import_datei=f"{filename} ({heute})",
            historie=" | ".join(historie_teile) or None,
            status="zu bewerten",
            begruendung="Importiert, noch nicht bewertet.",
        )
        if domain in leads:
            # Mehrere Kontakte derselben Firma: Historie zusammenführen, ersten Kontakt behalten
            prev = leads[domain]
            if lead.historie and lead.historie not in (prev.historie or ""):
                prev.historie = " | ".join(x for x in (prev.historie, lead.historie) if x)
            continue
        leads[domain] = lead

    return list(leads.values()), result


def import_address_list(content: bytes, filename: str, store: LeadStore) -> ImportResult:
    """Importiert eine Adressliste in den LeadStore.

    Bereits vorhandene Firmen werden nicht überschrieben, sondern um Importdaten ergänzt und
    erneut auf «zu bewerten» gesetzt, sofern sie noch nicht kontaktiert wurden.
    """
    leads, result = parse_leads(content, filename)
    excluded = {normalize_domain(e.domain) for e in store.get_exclusion_list()}
    existing = {normalize_domain(l.domain): l for l in store.get_all_leads()}

    to_save: list[Lead] = []
    for lead in leads:
        if lead.domain in excluded:
            result.ausgeschlossen += 1
            continue
        old = existing.get(lead.domain)
        if old is None:
            to_save.append(lead)
            result.neu += 1
            continue
        # Bestehenden Datensatz ergänzen, nicht überschreiben
        for field in ("strasse", "plz", "ort", "kanton", "kontakt_name", "kontakt_rolle", "kontakt_email", "kontakt_tel", "website"):
            if not getattr(old, field) and getattr(lead, field):
                setattr(old, field, getattr(lead, field))
        if lead.historie and lead.historie not in (old.historie or ""):
            old.historie = " | ".join(x for x in (old.historie, lead.historie) if x)
        old.import_datei = lead.import_datei
        if old.quelle_kanal != KANAL_IMPORT:
            old.quelle_kanal = f"{old.quelle_kanal} + {KANAL_IMPORT}"
        if old.status in ("neu", "beobachten", "verworfen", "fehler", "zu bewerten") and old.freigabe != "nein":
            old.status = "zu bewerten"
        old.aktualisiert_am = datetime.now(timezone.utc).isoformat()
        to_save.append(old)
        result.aktualisiert += 1

    if to_save:
        store.upsert_leads(to_save)
    logger.info(
        "Import %s: %d Zeilen, %d neu, %d ergänzt, %d ausgeschlossen, %d ohne Website",
        filename, result.zeilen_gelesen, result.neu, result.aktualisiert, result.ausgeschlossen, result.ohne_website,
    )
    return result


def leads_to_evaluate(store: LeadStore, limit: int) -> list[Lead]:
    """Importierte Adressen, die im nächsten Lauf bewertet werden (mit Website zuerst)."""
    pending = [l for l in store.get_all_leads() if l.status == "zu bewerten"]
    pending.sort(key=lambda l: (not has_real_domain(l.domain), l.erstellt_am))
    return pending[: max(0, limit)]
