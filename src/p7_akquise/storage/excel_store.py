"""ExcelStore-Implementierung für projekt7 Leads mittels openpyxl.

Verwaltet die Excel-Arbeitsmappe projekt7_Leads.xlsx mit den 4 Blättern:
- Leads: Haupttabelle inkl. Dropdown-Datenvalidierung
- Ausschluss: Liste ausgeschlossener Domains (Bestandskunden, Wettbewerber etc.)
- Protokoll: Logbuch der Pipeline-Läufe
- Verworfen: Ausgeschiedene Leads (C-Leads und abgelehnte Leads)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from p7_akquise.models import (
    ExclusionEntry,
    Lead,
    ProtocolEntry,
    RejectedLead,
    normalize_domain,
)
from p7_akquise.storage.base import LeadStore

logger = logging.getLogger("p7_akquise.excel")

LEADS_COLUMNS = [
    ("lead_id", "Lead ID", 36),
    ("domain", "Domain (Schlüssel)", 24),
    ("firma", "Unternehmen", 28),
    ("website", "Website", 28),
    ("strasse", "Strasse", 22),
    ("plz", "PLZ", 8),
    ("ort", "Ort", 18),
    ("kanton", "Kanton", 8),
    ("land", "Land", 8),
    ("segment", "Segment (A=Produktion, B=Handel, C=andere)", 16),
    ("branche", "Branche", 20),
    ("groesse", "Grösse", 16),
    ("kaufsignal", "Signal", 30),
    ("angebot", "Vorgeschlagener Einstieg", 26),
    ("kontakt_name", "Kontakt Name", 22),
    ("kontakt_rolle", "Kontakt Rolle", 22),
    ("kontakt_email", "Kontakt E-Mail", 26),
    ("kontakt_tel", "Kontakt Telefon", 18),
    ("score", "Score (0-100)", 12),
    ("klasse", "Klasse (A/B/C)", 12),
    ("teilscores", "Teilscores (JSON)", 30),
    ("begruendung", "Begründung", 45),
    ("quellen", "Quellen-URLs", 35),
    ("modell_version", "Modellversion", 12),
    ("quelle_kanal", "Kanal", 16),
    ("import_datei", "Importdatei", 24),
    ("historie", "Historie (aus Adressliste)", 35),
    ("status", "Status", 14),
    ("freigabe", "Freigabe (ja/nein)", 14),
    ("verwerfungsgrund", "Verwerfungsgrund", 22),
    ("kommentar_berater", "Kommentar KAM", 35),
    ("kontaktiert_am", "Kontaktiert am", 14),
    ("anzahl_nachfass", "Anzahl Nachfass", 12),
    ("wiedervorlage_am", "Wiedervorlage am", 14),
    ("mail_entwurf", "Mail-Entwurf", 45),
    ("erstellt_am", "Erstellt am", 20),
    ("aktualisiert_am", "Aktualisiert am", 20),
    ("manuelle_freigabe", "Manuelle Freigabe (trotz Score)", 18),
]

AUSSCHLUSS_COLUMNS = [
    ("domain", "Domain", 25),
    ("firma", "Unternehmen", 28),
    ("grund", "Grund (Bestandskunde / Wettbewerber / Opt-out)", 35),
    ("eingetragen_am", "Eingetragen am", 18),
]

PROTOKOLL_COLUMNS = [
    ("lauf_id", "Lauf-ID", 36),
    ("datum", "Datum / Zeit", 22),
    ("modell_version", "Modellversion", 14),
    ("anzahl_kandidaten", "Kandidaten Websuche", 14),
    ("anzahl_import_bewertet", "Importierte bewertet", 14),
    ("duplikate", "Duplikate", 14),
    ("anzahl_a", "Klasse A", 12),
    ("anzahl_b", "Klasse B", 12),
    ("anzahl_c", "Klasse C", 12),
    ("fehler", "Fehler", 12),
    ("verbrauchte_credits", "Credits", 14),
]

VERWORFEN_COLUMNS = [
    ("domain", "Domain", 25),
    ("firma", "Unternehmen", 28),
    ("grund", "Grund", 30),
    ("verworfen_am", "Verworfen am", 18),
]

SUCHBEGRIFFE_COLUMNS = [
    ("suchbegriff", "Suchbegriff", 35),
    ("letzter_lauf_id", "Letzter Lauf-ID", 36),
    ("zuletzt_verwendet_am", "Zuletzt verwendet am", 22),
    ("anzahl_verwendungen", "Anzahl Verwendungen", 18),
]

# Farben Kopfzeile
HEADER_FILL = PatternFill(start_color="1F2A44", end_color="1F2A44", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
BORDER_THIN = Border(
    left=Side(style="thin", color="D3D3D3"),
    right=Side(style="thin", color="D3D3D3"),
    top=Side(style="thin", color="D3D3D3"),
    bottom=Side(style="thin", color="D3D3D3"),
)


VERWERFUNGSGRUENDE = [
    "falsche Branche",
    "zu klein",
    "zu gross",
    "kein Bedarf",
    "Bestandskunde",
    "Wettbewerber",
    "hat Abacus",
    "Kontakt fehlt",
    "Sonstiges",
]


class ExcelStore(LeadStore):
    """Excel-basierter LeadStore mit openpyxl."""

    def __init__(self, file_path: str | Path = "data/projekt7_Leads.xlsx") -> None:
        self.file_path = Path(file_path)
        from p7_akquise.storage import blob_sync
        self._blob = blob_sync.blob_enabled()
        if self._blob:
            # Gemeinsamer Speicher (Vercel Blob): lokale Datei ist nur eine Arbeitskopie
            self.file_path = Path("/tmp" if os.name != "nt" else os.getenv("TEMP", ".")) / self.file_path.name
            try:
                if not blob_sync.download_latest(self.file_path) and Path(file_path).exists() and not self.file_path.exists():
                    import shutil
                    shutil.copy2(Path(file_path), self.file_path)  # Startbestand einmalig übernehmen
            except Exception as e:
                logger.error("Blob-Speicher nicht erreichbar: %s", e)
                raise
        elif os.getenv("VERCEL"):
            # Auf Vercel ist das Dateisystem read-only, ausser in /tmp
            tmp_path = Path("/tmp") / self.file_path.name
            if not tmp_path.exists():
                candidates = [
                    self.file_path,
                    Path("/var/task") / self.file_path,
                    Path(__file__).resolve().parents[3] / "data" / self.file_path.name,
                ]
                import shutil
                for cand in candidates:
                    if cand.exists():
                        shutil.copy2(cand, tmp_path)
                        break
            if tmp_path.exists():
                self.file_path = tmp_path
        self.init_store()

    def _ensure_parent_dir(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def init_store(self) -> None:
        """Erstellt die Arbeitsmappe mit allen Blättern und Validierungen, falls nicht existent."""
        self._ensure_parent_dir()
        if self.file_path.exists():
            return

        wb = openpyxl.Workbook()
        # Default-Blatt umbenennen zu 'Leads'
        ws_leads = wb.active
        ws_leads.title = "Leads"

        # Weitere Blätter anlegen
        ws_ausschluss = wb.create_sheet(title="Ausschluss")
        ws_protokoll = wb.create_sheet(title="Protokoll")
        ws_verworfen = wb.create_sheet(title="Verworfen")
        ws_suchbegriffe = wb.create_sheet(title="Suchbegriffe")

        # Header schreiben und formatieren
        self._setup_sheet_headers(ws_leads, LEADS_COLUMNS)
        self._setup_sheet_headers(ws_ausschluss, AUSSCHLUSS_COLUMNS)
        self._setup_sheet_headers(ws_protokoll, PROTOKOLL_COLUMNS)
        self._setup_sheet_headers(ws_verworfen, VERWORFEN_COLUMNS)
        self._setup_sheet_headers(ws_suchbegriffe, SUCHBEGRIFFE_COLUMNS)

        # Datenvalidierung (Dropdowns) auf Blatt 'Leads' einrichten
        self._setup_data_validation(ws_leads)

        self._save(wb)
        wb.close()

    def _setup_sheet_headers(self, ws: Any, col_defs: list[tuple[str, str, int]]) -> None:
        """Setzt Spaltenüberschriften, Breiten und Styling."""
        for col_idx, (col_key, col_title, col_width) in enumerate(col_defs, start=1):
            cell = ws.cell(row=1, column=col_idx, value=col_title)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER_THIN

            col_letter = get_column_letter(col_idx)
            ws.column_dimensions[col_letter].width = col_width

        ws.row_dimensions[1].height = 26

    def _setup_data_validation(self, ws: Any) -> None:
        """Richtet Dropdowns für freigabe und verwerfungsgrund ein."""
        # Freigabe-Spalte ermitteln (Index 26 in LEADS_COLUMNS)
        freigabe_col_idx = [k for k, _, _ in LEADS_COLUMNS].index("freigabe") + 1
        freigabe_col_letter = get_column_letter(freigabe_col_idx)

        # Verwerfungsgrund-Spalte ermitteln (Index 27)
        grund_col_idx = [k for k, _, _ in LEADS_COLUMNS].index("verwerfungsgrund") + 1
        grund_col_letter = get_column_letter(grund_col_idx)

        # Dropdown Freigabe: ja / nein
        dv_freigabe = DataValidation(
            type="list",
            formula1='"ja,nein"',
            allow_blank=True,
            showDropDown=False,
            errorTitle="Ungültige Eingabe",
            error="Bitte wählen Sie 'ja' oder 'nein'.",
        )
        ws.add_data_validation(dv_freigabe)
        dv_freigabe.add(f"{freigabe_col_letter}2:{freigabe_col_letter}1000")

        # Dropdown Verwerfungsgrund
        gruende = VERWERFUNGSGRUENDE
        dv_grund = DataValidation(
            type="list",
            formula1=f'"{",".join(gruende)}"',
            allow_blank=True,
            showDropDown=False,
            errorTitle="Ungültige Eingabe",
            error="Bitte wählen Sie einen gültigen Verwerfungsgrund.",
        )
        ws.add_data_validation(dv_grund)
        dv_grund.add(f"{grund_col_letter}2:{grund_col_letter}1000")

    def _save(self, wb: openpyxl.Workbook) -> None:
        wb.save(self.file_path)
        if getattr(self, "_blob", False):
            from p7_akquise.storage import blob_sync
            blob_sync.upload(self.file_path)

    def _load_workbook(self) -> openpyxl.Workbook:
        if getattr(self, "_blob", False):
            from p7_akquise.storage import blob_sync
            try:
                blob_sync.download_latest(self.file_path)
            except Exception as e:
                logger.warning("Neueste Blob-Version nicht geladen, arbeite mit lokaler Kopie: %s", e)
        self.init_store()
        try:
            wb = openpyxl.load_workbook(self.file_path)
        except Exception as e:
            logger.warning("Beschädigte Arbeitskopie (%s). Stelle aus Vorlage wieder her.", e)
            candidates = [
                Path("data/projekt7_Leads.xlsx"),
                Path("/var/task/data/projekt7_Leads.xlsx"),
                Path(__file__).resolve().parents[3] / "data" / "projekt7_Leads.xlsx",
            ]
            recovered = False
            for cand in candidates:
                if cand.exists():
                    import shutil
                    shutil.copy2(cand, self.file_path)
                    recovered = True
                    break
            if not recovered:
                self.file_path.unlink(missing_ok=True)
                self.init_store()
            wb = openpyxl.load_workbook(self.file_path)

        # Bestehende Dateien um neu hinzugekommene Spalten ergänzen (Kopfzeile)
        ws = wb["Leads"]
        for col_idx, (_, header, _w) in enumerate(LEADS_COLUMNS, start=1):
            if not ws.cell(row=1, column=col_idx).value:
                ws.cell(row=1, column=col_idx, value=header)
        return wb

    def _lead_to_row_dict(self, lead: Lead) -> dict[str, Any]:
        """Konvertiert ein Lead-Objekt in ein zeilenbasiertes Dict für die Excel-Spalten."""
        data = lead.model_dump()
        row: dict[str, Any] = {}
        for key, _, _ in LEADS_COLUMNS:
            val = data.get(key)
            if key == "domain":
                val = normalize_domain(lead.domain)
            elif key == "teilscores":
                val = json.dumps(lead.teilscores, ensure_ascii=False) if lead.teilscores else "{}"
            elif key == "quellen":
                val = "\n".join(lead.quellen) if lead.quellen else ""
            elif key == "manuelle_freigabe":
                val = "ja" if lead.manuelle_freigabe else ""
            elif key in ("score", "anzahl_nachfass"):
                val = int(val or 0)
            elif val is None:
                val = ""
            row[key] = val
        return row

    def _row_to_lead(self, row_values: list[Any], keys: list[str]) -> Lead:
        """Erzeugt ein Lead-Objekt aus Zeilendaten."""
        data: dict[str, Any] = {}
        for idx, key in enumerate(keys):
            val = row_values[idx] if idx < len(row_values) else None
            if key == "teilscores":
                if isinstance(val, str) and val.strip():
                    try:
                        data[key] = json.loads(val)
                    except Exception:
                        data[key] = {}
                else:
                    data[key] = {}
            elif key == "quellen":
                if isinstance(val, str) and val.strip():
                    data[key] = [q.strip() for q in val.split("\n") if q.strip()]
                else:
                    data[key] = []
            elif key == "manuelle_freigabe":
                data[key] = str(val).strip().lower() in ("ja", "true", "1") if val is not None else False
            elif key == "begruendung":
                data[key] = str(val) if val is not None else ""
            elif key in ("score", "anzahl_nachfass"):
                if val is not None:
                    try:
                        data[key] = int(val)
                    except Exception:
                        data[key] = 0
                else:
                    data[key] = 0
            else:
                data[key] = val if val not in (None, "") else None

        if not data.get("land"):
            data["land"] = "CH"
        if not data.get("segment"):
            data["segment"] = "C"
        if not data.get("klasse"):
            data["klasse"] = "C"
        if not data.get("status"):
            data["status"] = "neu"
        if not data.get("modell_version"):
            data["modell_version"] = "1.0.0"
        if not data.get("quelle_kanal"):
            data["quelle_kanal"] = "KI-Websuche"
        for k in ("erstellt_am", "aktualisiert_am", "lead_id"):
            if not data.get(k):
                data.pop(k, None)
        return Lead.model_validate(data)

    def upsert_lead(self, lead: Lead) -> None:
        """Aktualisiert oder erstellt einen Lead per Domain."""
        self.upsert_leads([lead])

    def upsert_leads(self, leads: list[Lead]) -> int:
        """Aktualisiert oder erstellt mehrere Leads."""
        if not leads:
            return 0

        wb = self._load_workbook()
        ws = wb["Leads"]

        keys = [k for k, _, _ in LEADS_COLUMNS]
        domain_col_idx = keys.index("domain") + 1
        id_col_idx = keys.index("lead_id") + 1

        # Bestehende Zeilen indizieren (per Domain und per Lead-ID, damit sich die Domain
        # eines importierten Leads nach der Website-Ermittlung ändern darf)
        existing_rows: dict[str, int] = {}
        rows_by_id: dict[str, int] = {}
        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=domain_col_idx).value
            if val:
                norm = normalize_domain(str(val))
                existing_rows[norm] = row_idx
                lid = ws.cell(row=row_idx, column=id_col_idx).value
                if lid:
                    rows_by_id[str(lid)] = row_idx

        # Manuell gepflegte Spalten, die beim Update nicht ungewollt geleert werden dürfen
        manual_cols = {"freigabe", "verwerfungsgrund", "kommentar_berater", "kontaktiert_am", "mail_entwurf", "manuelle_freigabe"}

        processed = 0
        for lead in leads:
            norm_domain = normalize_domain(lead.domain)
            lead.domain = norm_domain
            row_dict = self._lead_to_row_dict(lead)

            target_row = rows_by_id.get(lead.lead_id) or existing_rows.get(norm_domain)
            if target_row:
                # Update bestehende Zeile
                for col_idx, key in enumerate(keys, start=1):
                    new_val = row_dict.get(key)
                    # Manuelle Berater-Felder schützen, falls neuer Wert leer ist
                    if key in manual_cols and not new_val:
                        existing_val = ws.cell(row=target_row, column=col_idx).value
                        if existing_val:
                            continue
                    ws.cell(row=target_row, column=col_idx, value=new_val)
            else:
                # Neue Zeile anhängen
                target_row = ws.max_row + 1
                for col_idx, key in enumerate(keys, start=1):
                    ws.cell(row=target_row, column=col_idx, value=row_dict.get(key))
                existing_rows[norm_domain] = target_row
                rows_by_id[lead.lead_id] = target_row

            processed += 1

        self._save(wb)
        wb.close()
        return processed

    def get_lead_by_domain(self, domain: str) -> Lead | None:
        """Gibt den Lead zur gesuchten Domain zurück."""
        norm_search = normalize_domain(domain)
        wb = self._load_workbook()
        ws = wb["Leads"]

        keys = [k for k, _, _ in LEADS_COLUMNS]
        domain_col_idx = keys.index("domain") + 1

        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=domain_col_idx).value
            if val and normalize_domain(str(val)) == norm_search:
                row_vals = [ws.cell(row=row_idx, column=c).value for c in range(1, len(keys) + 1)]
                wb.close()
                return self._row_to_lead(row_vals, keys)

        wb.close()
        return None

    def get_all_leads(self, filters: dict[str, Any] | None = None) -> list[Lead]:
        """Gibt alle Leads zurück, optional gefiltert."""
        wb = self._load_workbook()
        ws = wb["Leads"]

        keys = [k for k, _, _ in LEADS_COLUMNS]
        leads: list[Lead] = []

        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=keys.index("domain") + 1).value
            if not val:
                continue
            row_vals = [ws.cell(row=row_idx, column=c).value for c in range(1, len(keys) + 1)]
            try:
                lead = self._row_to_lead(row_vals, keys)
                if filters:
                    match = True
                    for f_key, f_val in filters.items():
                        if getattr(lead, f_key, None) != f_val:
                            match = False
                            break
                    if not match:
                        continue
                leads.append(lead)
            except Exception:
                continue

        wb.close()
        return leads

    def get_exclusion_list(self) -> list[ExclusionEntry]:
        """Liest die Ausschlussliste aus dem Blatt 'Ausschluss'."""
        wb = self._load_workbook()
        ws = wb["Ausschluss"]
        entries: list[ExclusionEntry] = []

        for row_idx in range(2, ws.max_row + 1):
            dom = ws.cell(row=row_idx, column=1).value
            if not dom:
                continue
            firma = ws.cell(row=row_idx, column=2).value
            grund = ws.cell(row=row_idx, column=3).value or "Ausschluss"
            eingetragen = ws.cell(row=row_idx, column=4).value or ""
            entries.append(
                ExclusionEntry(
                    domain=str(dom),
                    firma=str(firma) if firma else None,
                    grund=str(grund),
                    eingetragen_am=str(eingetragen),
                )
            )

        wb.close()
        return entries

    def is_excluded(self, domain: str) -> bool:
        """Prüft, ob eine Domain ausgeschlossen ist."""
        norm_search = normalize_domain(domain)
        for entry in self.get_exclusion_list():
            if normalize_domain(entry.domain) == norm_search:
                return True
        return False

    def add_exclusion(self, entry: ExclusionEntry) -> None:
        """Fügt einen Eintrag zur Ausschlussliste hinzu."""
        wb = self._load_workbook()
        ws = wb["Ausschluss"]
        target_row = ws.max_row + 1
        ws.cell(row=target_row, column=1, value=normalize_domain(entry.domain))
        ws.cell(row=target_row, column=2, value=entry.firma or "")
        ws.cell(row=target_row, column=3, value=entry.grund)
        ws.cell(row=target_row, column=4, value=entry.eingetragen_am)
        self._save(wb)
        wb.close()

    def add_protocol_entry(self, entry: ProtocolEntry) -> None:
        """Schreibt einen Lauf-Protokolleintrag."""
        wb = self._load_workbook()
        ws = wb["Protokoll"]
        target_row = ws.max_row + 1
        keys = [k for k, _, _ in PROTOKOLL_COLUMNS]
        row_dict = entry.model_dump()
        for col_idx, key in enumerate(keys, start=1):
            ws.cell(row=target_row, column=col_idx, value=row_dict.get(key))
        self._save(wb)
        wb.close()

    def get_protocol_entries(self) -> list[ProtocolEntry]:
        """Liest alle Lauf-Protokolleinträge ein."""
        wb = self._load_workbook()
        ws = wb["Protokoll"]
        keys = [k for k, _, _ in PROTOKOLL_COLUMNS]
        entries: list[ProtocolEntry] = []

        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=1).value
            if not val:
                continue
            data = {keys[c - 1]: ws.cell(row=row_idx, column=c).value for c in range(1, len(keys) + 1)}
            try:
                entries.append(ProtocolEntry.model_validate(data))
            except Exception:
                continue

        wb.close()
        return entries

    def _ensure_suchbegriffe_sheet(self, wb: openpyxl.Workbook) -> Any:
        if "Suchbegriffe" not in wb.sheetnames:
            ws = wb.create_sheet(title="Suchbegriffe")
            self._setup_sheet_headers(ws, SUCHBEGRIFFE_COLUMNS)
            return ws
        return wb["Suchbegriffe"]

    def record_search_queries(self, queries: list[str], lauf_id: str, datum: str | None = None) -> None:
        """Speichert verwendete Suchbegriffe mit Datum für die Rotation."""
        if not queries:
            return
        dt = datum or datetime.now(timezone.utc).isoformat()
        wb = self._load_workbook()
        ws = self._ensure_suchbegriffe_sheet(wb)

        # Vorhandene Begriffe einlesen
        row_map: dict[str, int] = {}
        for row_idx in range(2, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=1).value
            if val:
                row_map[str(val).strip()] = row_idx

        for q in queries:
            q_clean = q.strip()
            if not q_clean:
                continue
            if q_clean in row_map:
                r = row_map[q_clean]
                ws.cell(row=r, column=2, value=lauf_id)
                ws.cell(row=r, column=3, value=dt)
                current_cnt = ws.cell(row=r, column=4).value or 0
                try:
                    current_cnt = int(current_cnt)
                except Exception:
                    current_cnt = 0
                ws.cell(row=r, column=4, value=current_cnt + 1)
            else:
                target_row = ws.max_row + 1
                ws.cell(row=target_row, column=1, value=q_clean)
                ws.cell(row=target_row, column=2, value=lauf_id)
                ws.cell(row=target_row, column=3, value=dt)
                ws.cell(row=target_row, column=4, value=1)
                row_map[q_clean] = target_row

        self._save(wb)
        wb.close()

    def get_used_search_queries(self) -> dict[str, str]:
        """Gibt {suchbegriff: letztes_datum_iso} zurück."""
        wb = self._load_workbook()
        if "Suchbegriffe" not in wb.sheetnames:
            wb.close()
            return {}
        ws = wb["Suchbegriffe"]
        out: dict[str, str] = {}
        for row_idx in range(2, ws.max_row + 1):
            term = ws.cell(row=row_idx, column=1).value
            dt = ws.cell(row=row_idx, column=3).value
            if term:
                out[str(term).strip()] = str(dt or "")
        wb.close()
        return out

    def add_rejected_lead(self, rejected: RejectedLead) -> None:
        """Fügt einen Lead ins Blatt 'Verworfen' ein."""
        wb = self._load_workbook()
        ws = wb["Verworfen"]
        target_row = ws.max_row + 1
        ws.cell(row=target_row, column=1, value=normalize_domain(rejected.domain))
        ws.cell(row=target_row, column=2, value=rejected.firma or "")
        ws.cell(row=target_row, column=3, value=rejected.grund)
        ws.cell(row=target_row, column=4, value=rejected.verworfen_am)
        self._save(wb)
        wb.close()

    def get_rejected_leads(self) -> list[RejectedLead]:
        """Liest alle verworfenen Leads ein."""
        wb = self._load_workbook()
        ws = wb["Verworfen"]
        entries: list[RejectedLead] = []

        for row_idx in range(2, ws.max_row + 1):
            dom = ws.cell(row=row_idx, column=1).value
            if not dom:
                continue
            firma = ws.cell(row=row_idx, column=2).value
            grund = ws.cell(row=row_idx, column=3).value or "C-Lead"
            datum = ws.cell(row=row_idx, column=4).value or ""
            entries.append(
                RejectedLead(
                    domain=str(dom),
                    firma=str(firma) if firma else None,
                    grund=str(grund),
                    verworfen_am=str(datum),
                )
            )

        wb.close()
        return entries

    def update_feedback(
        self,
        domain: str,
        freigabe: str,
        verwerfungsgrund: str | None = None,
        kommentar_berater: str | None = None,
    ) -> Lead | None:
        """Aktualisiert die Berater-Rückmeldung (freigabe ja/nein) in Excel."""
        lead = self.get_lead_by_domain(domain)
        if not lead:
            return None

        clean_freigabe = freigabe.strip().lower()
        if clean_freigabe not in ("ja", "nein"):
            raise ValueError("Freigabe muss 'ja' oder 'nein' sein.")

        lead.freigabe = clean_freigabe  # type: ignore
        lead.verwerfungsgrund = verwerfungsgrund
        lead.kommentar_berater = kommentar_berater
        lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()

        if clean_freigabe == "ja":
            lead.status = "freigegeben"
            lead.wiedervorlage_am = None
            if lead.klasse != "A":
                lead.manuelle_freigabe = True
        elif clean_freigabe == "nein":
            lead.status = "verworfen"
            # Auch ins Blatt Verworfen eintragen
            self.add_rejected_lead(
                RejectedLead(
                    domain=lead.domain,
                    firma=lead.firma,
                    grund=verwerfungsgrund or "Verworfen durch Berater",
                )
            )

        self.upsert_lead(lead)
        return lead

    def remove_lead(self, lead_id: str) -> bool:
        """Entfernt eine Zeile ohne Eintrag ins Blatt «Verworfen» (z. B. Dublette nach Website-Ermittlung)."""
        wb = self._load_workbook()
        ws = wb["Leads"]
        id_idx = [k for k, _, _ in LEADS_COLUMNS].index("lead_id") + 1
        for row_idx in range(2, ws.max_row + 1):
            if str(ws.cell(row=row_idx, column=id_idx).value or "") == lead_id:
                ws.delete_rows(row_idx)
                self._save(wb)
                wb.close()
                return True
        wb.close()
        return False

    def delete_lead(self, domain_or_id: str) -> bool:
        """Löscht einen Lead aus dem Blatt 'Leads' und trägt ihn als verworfen ein."""
        target_norm = normalize_domain(domain_or_id)
        wb = self._load_workbook()
        ws = wb["Leads"]

        keys = [k for k, _, _ in LEADS_COLUMNS]
        lead_id_idx = keys.index("lead_id") + 1
        domain_idx = keys.index("domain") + 1
        firma_idx = keys.index("firma") + 1

        row_to_delete = None
        deleted_domain = ""
        deleted_firma = ""

        for row_idx in range(2, ws.max_row + 1):
            val_id = ws.cell(row=row_idx, column=lead_id_idx).value
            val_dom = ws.cell(row=row_idx, column=domain_idx).value
            if not val_dom and not val_id:
                continue

            if str(val_id) == domain_or_id or (val_dom and normalize_domain(str(val_dom)) == target_norm):
                row_to_delete = row_idx
                deleted_domain = str(val_dom) if val_dom else domain_or_id
                deleted_firma = str(ws.cell(row=row_idx, column=firma_idx).value or "")
                break

        if row_to_delete:
            ws.delete_rows(row_to_delete)
            # Ins Blatt 'Verworfen' eintragen, damit nicht erneut recherchiert
            ws_verworfen = wb["Verworfen"]
            target_v_row = ws_verworfen.max_row + 1
            ws_verworfen.cell(row=target_v_row, column=1, value=normalize_domain(deleted_domain))
            ws_verworfen.cell(row=target_v_row, column=2, value=deleted_firma)
            ws_verworfen.cell(row=target_v_row, column=3, value="Manuell gelöscht (irrelevant)")
            ws_verworfen.cell(row=target_v_row, column=4, value=datetime.now(timezone.utc).strftime("%Y-%m-%d"))

            self._save(wb)
            wb.close()
            return True

        wb.close()
        return False
