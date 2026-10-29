"""FastAPI-Backend für das Dashboard «projekt7 Lead-Cockpit»."""

from __future__ import annotations

import base64
import csv
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from p7_akquise.config import setting
from p7_akquise.models import Lead, RejectedLead, normalize_domain
from p7_akquise.steps.scoring import load_scoring_model
from p7_akquise.storage.base import LeadStore
from p7_akquise.storage.excel_store import ExcelStore

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Globale Store-Referenz (überschreibbar für Tests)
_GLOBAL_STORE: LeadStore | None = None


def get_lead_store() -> LeadStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        _GLOBAL_STORE = ExcelStore(setting("storage", "excel_path", "data/projekt7_Leads.xlsx"))
    return _GLOBAL_STORE


def set_lead_store(store: LeadStore) -> None:
    global _GLOBAL_STORE
    _GLOBAL_STORE = store


class FreigabePayload(BaseModel):
    entscheid: Literal["ja", "nein"]
    grund: str | None = None
    kommentar: str | None = None
    begruendung: str | None = Field(default=None, description="Pflicht bei Freigabe eines B- oder C-Leads (unter der Schwelle)")
    benutzer: str = "Berater"


class AntwortPayload(BaseModel):
    antwort: Literal["interesse", "termin", "spaeter", "absage"]
    wiedervorlage_tage: int = 90
    benutzer: str = "Berater"


class ImportPayload(BaseModel):
    dateiname: str
    inhalt_base64: str


class KontaktiertPayload(BaseModel):
    benutzer: str = "Berater"
    kontaktiert_am: str | None = None


def create_app(store: LeadStore | None = None) -> FastAPI:
    if store is not None:
        set_lead_store(store)

    app = FastAPI(
        title="projekt7 Lead-Cockpit",
        description="Prüfung, Freigabe und Nachverfolgung von Leads für das Team Produktion und Handel",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 1. KPIs
    @app.get("/api/kpis")
    def get_kpis(store: LeadStore = Depends(get_lead_store)) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        a_leads_zur_pruefung = sum(1 for l in all_leads if l.klasse == "A" and l.status == "zur Prüfung")
        
        # Fällige Nachfassaktionen: kontaktiert vor >= 5 Tagen ohne Folgestatus
        faellige_nachfass = 0
        for l in all_leads:
            if l.status == "kontaktiert" and l.kontaktiert_am:
                try:
                    k_date = datetime.strptime(l.kontaktiert_am[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if (now - k_date).days >= 5 and l.anzahl_nachfass < 2:
                        faellige_nachfass += 1
                except Exception:
                    pass

        # Freigabequote der A-Leads
        freigegeben_count = sum(1 for l in all_leads if l.klasse == "A" and l.status in ("freigegeben", "kontaktiert", "interesse"))
        verworfen_count = sum(1 for l in all_leads if l.klasse == "A" and l.status == "verworfen")
        total_geprueft = freigegeben_count + verworfen_count
        freigabequote = round((freigegeben_count / total_geprueft) * 100, 1) if total_geprueft > 0 else 0.0

        # Leads neu diese Woche
        neu_diese_woche = 0
        for l in all_leads:
            try:
                c_date = datetime.fromisoformat(l.erstellt_am)
                if c_date >= week_ago:
                    neu_diese_woche += 1
            except Exception:
                pass

        # Letzter Lauf
        protocols = store.get_protocol_entries()
        last_run = protocols[-1] if protocols else None

        return {
            "a_leads_zur_pruefung": a_leads_zur_pruefung,
            "faellige_nachfass": faellige_nachfass,
            "freigabequote_prozent": freigabequote,
            "leads_gesamt": len(all_leads),
            "leads_neu_diese_woche": neu_diese_woche,
            "manuelle_freigaben": sum(1 for l in all_leads if l.manuelle_freigabe),
            "modell_version": load_scoring_model().version,
            "zu_bewerten": sum(1 for l in all_leads if l.status == "zu bewerten"),
            "aus_adressliste": sum(1 for l in all_leads if "Adressliste" in (l.quelle_kanal or "")),
            "termine": sum(1 for l in all_leads if l.status == "termin"),
            "fehler": sum(1 for l in all_leads if l.status == "fehler"),
            "letzter_lauf": last_run.model_dump() if last_run else None,
        }

    # 2. Leads Liste mit Filtern
    @app.get("/api/leads")
    def list_leads(
        search: str | None = Query(None, description="Freitextsuche nach Firma, Ort, Branche, Kontakt"),
        land: str | None = Query(None),
        segment: str | None = Query(None),
        klasse: str | None = Query(None),
        status: str | None = Query(None),
        kanal: str | None = Query(None, description="Adressliste oder KI-Websuche"),
        store: LeadStore = Depends(get_lead_store),
    ) -> list[dict[str, Any]]:
        leads = store.get_all_leads()

        filtered: list[Lead] = []
        for l in leads:
            if land and l.land != land:
                continue
            if segment and l.segment != segment:
                continue
            if klasse and l.klasse != klasse:
                continue
            if status and l.status != status:
                continue
            if kanal and kanal.lower() not in (l.quelle_kanal or "").lower():
                continue
            if search:
                s_term = search.lower()
                text_to_search = f"{l.firma} {l.domain} {l.ort or ''} {l.branche or ''} {l.kontakt_name or ''} {l.kaufsignal or ''} {l.historie or ''}".lower()
                if s_term not in text_to_search:
                    continue

            filtered.append(l)

        # Standard-Sortierung: Score absteigend
        filtered.sort(key=lambda x: (x.status != "zu bewerten", x.score), reverse=True)
        return [l.model_dump() for l in filtered]

    # 3. Lead Detail
    @app.get("/api/leads/{lead_id}")
    def get_lead_detail(lead_id: str, store: LeadStore = Depends(get_lead_store)) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        found = None
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                found = l
                break

        if not found:
            raise HTTPException(status_code=404, detail=f"Lead '{lead_id}' nicht gefunden.")

        # Kriterienkatalog mit Max-Punkten laden
        cfg = load_scoring_model()
        criteria_catalog = {}
        for cat, crit_map in cfg.kriterien.items():
            if isinstance(crit_map, dict):
                for c_name, c_def in crit_map.items():
                    p = c_def.get("punkte", 0)
                    max_p = max(p.values()) if isinstance(p, dict) else int(p)
                    criteria_catalog[c_name] = {
                        "kategorie": cat,
                        "max_punkte": max_p,
                        "label": (cfg.labels or {}).get(c_name, c_name),
                    }

        result = found.model_dump()
        result["kriterien_katalog"] = criteria_catalog
        return result

    # 4. Freigabe / Verwerfen
    @app.post("/api/leads/{lead_id}/freigabe")
    def freigabe_action(
        lead_id: str,
        payload: FreigabePayload,
        store: LeadStore = Depends(get_lead_store),
    ) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        lead = None
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                lead = l
                break

        if not lead:
            raise HTTPException(status_code=404, detail="Lead nicht gefunden.")

        if payload.entscheid == "nein" and not (payload.grund and payload.grund.strip()):
            raise HTTPException(
                status_code=422,
                detail="Ein Verwerfungsgrund ist bei Ablehnung zwingend erforderlich.",
            )

        unter_schwelle = lead.klasse != "A"
        kommentar = f"{payload.kommentar or ''} (durch {payload.benutzer})".strip()

        if payload.entscheid == "ja" and unter_schwelle:
            if lead.status not in ("zur Prüfung", "beobachten", "verworfen"):
                raise HTTPException(status_code=409, detail=f"Lead mit Status '{lead.status}' kann nicht freigegeben werden.")
            if not (payload.begruendung and payload.begruendung.strip()):
                raise HTTPException(
                    status_code=422,
                    detail="Für die Freigabe eines Leads unter der Schwelle (Klasse B/C) ist eine Begründung erforderlich.",
                )
            kommentar = (
                f"Manuelle Freigabe trotz Klasse {lead.klasse} (Score {lead.score}): "
                f"{payload.begruendung.strip()} | {kommentar}"
            )

        updated = store.update_feedback(
            domain=lead.domain,
            freigabe=payload.entscheid,
            verwerfungsgrund=payload.grund,
            kommentar_berater=kommentar,
        )

        if updated and payload.entscheid == "ja" and unter_schwelle and not updated.manuelle_freigabe:
            # Für Stores, die das Flag nicht selbst setzen (z. B. Pipedrive)
            updated.manuelle_freigabe = True
            updated.wiedervorlage_am = None
            store.upsert_lead(updated)

        return {"success": True, "lead": updated.model_dump() if updated else None}

    # 5. Als kontaktiert markieren
    @app.post("/api/leads/{lead_id}/kontaktiert")
    def kontaktiert_action(
        lead_id: str,
        payload: KontaktiertPayload,
        store: LeadStore = Depends(get_lead_store),
    ) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        lead = None
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                lead = l
                break

        if not lead:
            raise HTTPException(status_code=404, detail="Lead nicht gefunden.")

        lead.status = "kontaktiert"
        lead.kontaktiert_am = payload.kontaktiert_am or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lead.kommentar_berater = f"{lead.kommentar_berater or ''} | Als kontaktiert markiert von {payload.benutzer}".strip(" |")
        lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()
        store.upsert_lead(lead)

        return {"success": True, "lead": lead.model_dump()}

    # 6. Antwort erfassen
    @app.post("/api/leads/{lead_id}/antwort")
    def antwort_action(
        lead_id: str,
        payload: AntwortPayload,
        store: LeadStore = Depends(get_lead_store),
    ) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        lead = None
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                lead = l
                break

        if not lead:
            raise HTTPException(status_code=404, detail="Lead nicht gefunden.")

        notiz = {
            "interesse": "Interesse gemeldet",
            "termin": "Termin vereinbart, Übergabe an Sales-Cycle",
            "spaeter": "Später interessant, Wiedervorlage",
            "absage": "Absage erhalten",
        }[payload.antwort]
        if payload.antwort == "interesse":
            lead.status = "interesse"
        elif payload.antwort == "termin":
            lead.status = "termin"
            lead.wiedervorlage_am = None
        elif payload.antwort == "spaeter":
            lead.status = "beobachten"
            lead.wiedervorlage_am = (datetime.now(timezone.utc) + timedelta(days=payload.wiedervorlage_tage)).strftime("%Y-%m-%d")
        else:
            lead.status = "kalt"
        lead.kommentar_berater = f"{lead.kommentar_berater or ''} | {notiz} ({payload.benutzer})".strip(" |")
        lead.aktualisiert_am = datetime.now(timezone.utc).isoformat()
        store.upsert_lead(lead)
        return {"success": True, "lead": lead.model_dump()}

    # 6b. Lead löschen (irrelevant)
    @app.delete("/api/leads/{lead_id}")
    def delete_lead(lead_id: str, store: LeadStore = Depends(get_lead_store)) -> dict[str, Any]:
        success = store.delete_lead(lead_id)
        if not success:
            raise HTTPException(status_code=404, detail="Lead nicht gefunden.")
        return {"success": True, "message": "Lead erfolgreich gelöscht und auf 'Verworfen' gesetzt."}

    # 7. CSV-Export (UTF-8 mit BOM, Semikolon-Trennzeichen)
    @app.get("/api/export.csv")
    def export_csv(store: LeadStore = Depends(get_lead_store)) -> Response:
        leads = store.get_all_leads()

        output = io.StringIO()
        # UTF-8 BOM für Excel
        output.write("\ufeff")

        writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        cols = [
            ("Lead ID", "lead_id"), ("Firma", "firma"), ("Domain", "domain"), ("Website", "website"),
            ("Strasse", "strasse"), ("PLZ", "plz"), ("Ort", "ort"), ("Kanton", "kanton"), ("Land", "land"),
            ("Segment", "segment"), ("Branche", "branche"), ("Grösse", "groesse"), ("Signal", "kaufsignal"),
            ("Einstieg", "angebot"), ("Kontakt Name", "kontakt_name"), ("Kontakt Rolle", "kontakt_rolle"),
            ("Kontakt E-Mail", "kontakt_email"), ("Kontakt Telefon", "kontakt_tel"), ("Score", "score"),
            ("Klasse", "klasse"), ("Status", "status"), ("Kanal", "quelle_kanal"), ("Historie", "historie"),
            ("Freigabe", "freigabe"), ("Verwerfungsgrund", "verwerfungsgrund"), ("Kommentar KAM", "kommentar_berater"),
            ("Kontaktiert am", "kontaktiert_am"), ("Wiedervorlage am", "wiedervorlage_am"),
            ("Manuelle Freigabe", "manuelle_freigabe"), ("Modellversion", "modell_version"), ("Erstellt am", "erstellt_am"),
        ]
        seg_labels = {"A": "Produktion", "B": "Handel", "C": "andere Branche"}
        writer.writerow([c[0] for c in cols])
        for l in leads:
            d = l.model_dump()
            row = []
            for _, key in cols:
                v = d.get(key)
                if key == "segment":
                    v = seg_labels.get(str(v), v)
                elif key == "manuelle_freigabe":
                    v = "ja" if v else ""
                row.append("" if v is None else v)
            writer.writerow(row)

        csv_content = output.getvalue().encode("utf-8")
        filename = f"projekt7_Leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # 8. Wochenlauf starten (Admin-Trigger / GitHub Actions Dispatch)
    @app.post("/api/runs")
    def trigger_run(
        background_tasks: BackgroundTasks,
        x_role: str | None = Header(default="admin"),
        store: LeadStore = Depends(get_lead_store),
    ) -> dict[str, Any]:
        if x_role and x_role.lower() != "admin":
            raise HTTPException(status_code=403, detail="Nur Administratoren können einen neuen Lauf starten.")

        github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_PAT")
        github_repo = os.getenv("GITHUB_REPO", "fpiccinin16/projekt7-akquise")

        # 1. Bevorzugt: GitHub Actions Workflow via API anstossen
        if github_token:
            import requests
            url = f"https://api.github.com/repos/{github_repo}/actions/workflows/weekly.yml/dispatches"
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            payload = {
                "ref": "main",
                "inputs": {
                    "dry_run": "false",
                    "max_kandidaten": str(setting("pipeline", "kandidaten_pro_lauf", 40)),
                    "max_import": str(setting("pipeline", "import_bewertungen_pro_lauf", 60)),
                },
            }
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                if resp.status_code in (200, 204):
                    return {
                        "success": True,
                        "mode": "github_actions",
                        "message": "Wochenlauf auf GitHub Actions erfolgreich angestossen! Die Recherche läuft jetzt auf GitHub und aktualisiert das Dashboard automatisch.",
                        "actions_url": f"https://github.com/{github_repo}/actions",
                    }
                else:
                    return {
                        "success": False,
                        "mode": "github_actions_failed",
                        "message": f"GitHub Actions API meldet Fehler {resp.status_code}: {resp.text}",
                        "actions_url": f"https://github.com/{github_repo}/actions",
                    }
            except Exception as e:
                return {
                    "success": False,
                    "mode": "github_actions_error",
                    "message": f"Netzwerkfehler beim Anstossen von GitHub Actions: {e}",
                    "actions_url": f"https://github.com/{github_repo}/actions",
                }

        # 2. Lokaler Modus / Fallback
        if not os.getenv("VERCEL"):
            from p7_akquise.pipeline import AkquisePipeline
            pipeline = AkquisePipeline(store=store)
            background_tasks.add_task(pipeline.run_weekly)
            return {"success": True, "mode": "local", "message": "Wochenlauf im lokalen Hintergrund gestartet."}

        # 3. Auf Vercel ohne GITHUB_TOKEN: Benutzer direkt zu GitHub Actions leiten
        return {
            "success": True,
            "mode": "redirect_to_github",
            "message": "Auf Vercel wird der Wochenlauf direkt über GitHub Actions ausgeführt (kein 15s-Serverless-Timeout).",
            "actions_url": f"https://github.com/{github_repo}/actions/workflows/weekly.yml",
        }

    # 9. Adressliste importieren (Excel/CSV als Base64, damit kein Multipart nötig ist)
    @app.post("/api/import")
    def import_liste(payload: ImportPayload, store: LeadStore = Depends(get_lead_store)) -> dict[str, Any]:
        from p7_akquise.steps.importer import import_address_list
        try:
            content = base64.b64decode(payload.inhalt_base64.split(",")[-1])
        except Exception:
            raise HTTPException(status_code=400, detail="Datei konnte nicht gelesen werden.")
        if len(content) > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Datei ist grösser als 4 MB. Bitte aufteilen.")
        try:
            result = import_address_list(content, payload.dateiname, store)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"success": True, "ergebnis": result.model_dump()}

    @app.post("/api/import/vorschau")
    def import_vorschau(payload: ImportPayload) -> dict[str, Any]:
        from p7_akquise.steps.importer import parse_leads
        try:
            content = base64.b64decode(payload.inhalt_base64.split(",")[-1])
            leads, result = parse_leads(content, payload.dateiname)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            raise HTTPException(status_code=400, detail="Datei konnte nicht gelesen werden.")
        return {
            "ergebnis": result.model_dump(),
            "beispiele": [
                {k: getattr(l, k) for k in ("firma", "website", "ort", "kontakt_name", "kontakt_email", "historie")}
                for l in leads[:5]
            ],
            "firmen": len(leads),
        }

    @app.get("/api/import/vorlage.xlsx")
    def import_vorlage() -> Response:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Adressen"
        ws.append(["Firma", "Website", "Strasse", "PLZ", "Ort", "Kanton", "Branche", "Mitarbeitende",
                   "Anrede", "Vorname", "Nachname", "Funktion", "E-Mail", "Telefon", "Angeschrieben", "Bemerkung"])
        ws.append(["Muster Metallbau AG", "www.muster-metallbau.ch", "Industriestrasse 1", "9000", "St. Gallen", "SG",
                   "Metallbau", "45", "Herr", "Max", "Muster", "Geschäftsführer", "max.muster@muster-metallbau.ch",
                   "071 000 00 00", "ja", "Kein Interesse, in einem Jahr wieder"])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(
            content=buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=projekt7_Adressliste_Vorlage.xlsx"},
        )

    # Statische Frontend-Dateien bereitstellen
    def get_index_html() -> str:
        candidates = [
            STATIC_DIR / "index.html",
            Path(__file__).resolve().parent / "static" / "index.html",
            Path("src/p7_akquise/dashboard/static/index.html"),
            Path("/var/task/src/p7_akquise/dashboard/static/index.html"),
        ]
        for c in candidates:
            if c.exists():
                return c.read_text(encoding="utf-8")
        return "<h1>projekt7 Lead-Cockpit</h1>"

    @app.get("/", response_class=HTMLResponse)
    def serve_index() -> HTMLResponse:
        return HTMLResponse(content=get_index_html())

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()
