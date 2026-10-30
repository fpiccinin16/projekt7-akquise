"""FastAPI-Backend für das Dashboard «projekt7 Lead-Cockpit»."""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from p7_akquise.config import setting
from p7_akquise.models import AenderungEntry, Lead, RejectedLead, normalize_domain
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


class PatchLeadPayload(BaseModel):
    felder: dict[str, Any]
    aktualisiert_am: str | None = None
    benutzer: str = "KAM"
    grund: str | None = None


class SperreAufhebenPayload(BaseModel):
    feld: str


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

    # 1b. Zähler für Filter (unabhängig von gesetzten Filtern)
    @app.get("/api/counts")
    def get_counts(store: LeadStore = Depends(get_lead_store)) -> dict[str, Any]:
        leads = store.get_all_leads()
        status_c: dict[str, int] = {}
        klasse_c: dict[str, int] = {}
        segment_c: dict[str, int] = {}
        kanal_c: dict[str, int] = {}
        for l in leads:
            s = l.status or "neu"
            status_c[s] = status_c.get(s, 0) + 1
            k = l.klasse or "C"
            klasse_c[k] = klasse_c.get(k, 0) + 1
            seg = l.segment or "C"
            segment_c[seg] = segment_c.get(seg, 0) + 1
            kan = "Adressliste" if "Adressliste" in (l.quelle_kanal or "") else "KI-Websuche"
            kanal_c[kan] = kanal_c.get(kan, 0) + 1
        return {
            "total": len(leads),
            "status": status_c,
            "klasse": klasse_c,
            "segment": segment_c,
            "quelle_kanal": kanal_c,
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

    # 6c. Lead bearbeiten (PATCH)
    @app.patch("/api/leads/{lead_id}")
    def patch_lead(
        lead_id: str,
        payload: PatchLeadPayload,
        store: LeadStore = Depends(get_lead_store),
    ) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        lead = None
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                lead = l
                break

        if not lead:
            raise HTTPException(status_code=404, detail=f"Lead '{lead_id}' nicht gefunden.")

        # Konfliktschutz: weicht aktualisiert_am vom gespeicherten Wert ab -> 409
        if payload.aktualisiert_am and lead.aktualisiert_am:
            if payload.aktualisiert_am.strip() != lead.aktualisiert_am.strip():
                raise HTTPException(
                    status_code=409,
                    detail={"message": "Lead wurde inzwischen geändert.", "lead": lead.model_dump()},
                )

        editable_fields = {
            "firma", "website", "strasse", "plz", "ort", "kanton", "land", "segment", "branche", "groesse",
            "kaufsignal", "angebot", "kontakt_name", "kontakt_rolle", "kontakt_email", "kontakt_tel",
            "teilscores", "score", "klasse", "begruendung", "quellen", "status", "wiedervorlage_am",
            "kontaktiert_am", "anzahl_nachfass", "mail_entwurf", "kommentar_berater", "historie",
            "freigabe", "verwerfungsgrund",
        }
        readonly_fields = {
            "lead_id", "domain", "modell_version", "quelle_kanal", "import_datei", "erstellt_am", "aktualisiert_am",
            "gesperrte_felder", "score_manuell",
        }

        for f in payload.felder:
            if f in readonly_fields:
                raise HTTPException(status_code=422, detail=f"Feld '{f}' ist schreibgeschützt.")
            if f not in editable_fields:
                raise HTTPException(status_code=422, detail=f"Feld '{f}' ist nicht bearbeitbar.")

        # Validierung
        if "kontakt_email" in payload.felder:
            em = str(payload.felder["kontakt_email"] or "").strip()
            if em and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
                raise HTTPException(status_code=422, detail="Ungültiges E-Mail-Format (kontakt_email)")

        if "website" in payload.felder:
            web = str(payload.felder["website"] or "").strip()
            if web and not (web.startswith("http://") or web.startswith("https://")) and "." not in web:
                raise HTTPException(status_code=422, detail="Ungültige URL (website)")

        if "plz" in payload.felder:
            plz = str(payload.felder["plz"] or "").strip()
            if plz and not re.match(r"^\d{4,5}$", plz):
                raise HTTPException(status_code=422, detail="PLZ muss 4 bis 5 Ziffern enthalten (plz)")

        if "segment" in payload.felder:
            seg = payload.felder["segment"]
            if seg not in ("A", "B", "C"):
                raise HTTPException(status_code=422, detail="Segment muss A, B oder C sein (segment)")

        if "klasse" in payload.felder:
            kl = payload.felder["klasse"]
            if kl not in ("A", "B", "C"):
                raise HTTPException(status_code=422, detail="Klasse muss A, B oder C sein (klasse)")

        if "score" in payload.felder:
            try:
                sc = int(payload.felder["score"])
                if sc < 0 or sc > 100:
                    raise ValueError
            except Exception:
                raise HTTPException(status_code=422, detail="Score muss eine Zahl zwischen 0 und 100 sein (score)")

        if "status" in payload.felder:
            st = payload.felder["status"]
            valid_st = {
                "neu", "zu bewerten", "zur Prüfung", "freigegeben", "beobachten",
                "verworfen", "kontaktiert", "nachgefasst_1", "nachgefasst_2",
                "interesse", "termin", "kalt", "fehler"
            }
            if st not in valid_st:
                raise HTTPException(status_code=422, detail="Ungültiger Status (status)")

        if "wiedervorlage_am" in payload.felder:
            w = payload.felder["wiedervorlage_am"]
            if w:
                try:
                    datetime.strptime(str(w).strip()[:10], "%Y-%m-%d")
                except Exception:
                    raise HTTPException(status_code=422, detail="Datum muss im Format YYYY-MM-DD sein (wiedervorlage_am)")

        if "kontaktiert_am" in payload.felder:
            k = payload.felder["kontaktiert_am"]
            if k:
                try:
                    datetime.strptime(str(k).strip()[:10], "%Y-%m-%d")
                except Exception:
                    raise HTTPException(status_code=422, detail="Datum muss im Format YYYY-MM-DD sein (kontaktiert_am)")

        if "teilscores" in payload.felder:
            ts = payload.felder["teilscores"]
            if not isinstance(ts, dict):
                raise HTTPException(status_code=422, detail="Teilscores muss ein JSON-Objekt sein (teilscores)")
            cfg = load_scoring_model()
            max_catalog: dict[str, int] = {}
            for _cat, c_map in cfg.kriterien.items():
                if isinstance(c_map, dict):
                    for cn, cd in c_map.items():
                        pts = cd.get("punkte", 0)
                        max_p = max(pts.values()) if isinstance(pts, dict) else int(pts)
                        max_catalog[cn] = max_p
            for k_name, val_p in ts.items():
                try:
                    p_int = int(val_p)
                except Exception:
                    raise HTTPException(status_code=422, detail=f"Teilscore für '{k_name}' muss eine Zahl sein (teilscores)")
                if k_name in max_catalog:
                    max_limit = max_catalog[k_name]
                    if p_int < 0 or p_int > max_limit:
                        raise HTTPException(status_code=422, detail=f"Teilscore für '{k_name}' darf maximal {max_limit} Punkte betragen (teilscores)")
                elif p_int < 0 or p_int > 100:
                    raise HTTPException(status_code=422, detail=f"Ungültiger Teilscore für '{k_name}' (teilscores)")

        # Status 'verworfen' ohne verwerfungsgrund
        new_status = payload.felder.get("status", lead.status)
        new_grund = payload.felder.get("verwerfungsgrund", lead.verwerfungsgrund)
        if new_status == "verworfen" and not (new_grund and str(new_grund).strip()):
            raise HTTPException(status_code=422, detail="Verwerfungsgrund ist Pflicht beim Status 'verworfen' (verwerfungsgrund)")

        # score oder klasse direkt geändert -> grund ist Pflicht, score_manuell = True
        if "score" in payload.felder or "klasse" in payload.felder:
            if not (payload.grund and str(payload.grund).strip()):
                raise HTTPException(status_code=422, detail="Begründung (grund) ist Pflicht bei manueller Änderung von Score oder Klasse (grund)")
            lead.score_manuell = True

        # website geändert -> domain neu normalisieren; existiert Domain bei anderem Lead -> 409
        if "website" in payload.felder:
            new_web = str(payload.felder["website"] or "").strip()
            new_dom = normalize_domain(new_web)
            if new_dom and new_dom != normalize_domain(lead.domain):
                for other in all_leads:
                    if other.lead_id != lead.lead_id and normalize_domain(other.domain) == new_dom:
                        raise HTTPException(status_code=409, detail=f"Dublette: {other.firma or other.domain}")
                lead.website = new_web
                lead.domain = new_dom

        # teilscores geändert -> score als Summe neu berechnen, klasse über ScoringEngine, score_manuell = True
        if "teilscores" in payload.felder:
            new_ts = {str(k): int(v) for k, v in payload.felder["teilscores"].items()}
            lead.teilscores = new_ts
            from p7_akquise.steps.scoring import ScoringEngine
            engine = ScoringEngine()
            new_calc_score = sum(new_ts.values())
            lead.score = new_calc_score
            lead.klasse = engine.determine_class(new_calc_score)
            lead.score_manuell = True

        now_iso = datetime.now(timezone.utc).isoformat()
        for field, new_val in payload.felder.items():
            old_val = getattr(lead, field, None)
            alt_str = json.dumps(old_val, ensure_ascii=False) if isinstance(old_val, (dict, list)) else ("" if old_val is None else str(old_val))
            neu_str = json.dumps(new_val, ensure_ascii=False) if isinstance(new_val, (dict, list)) else ("" if new_val is None else str(new_val))
            if alt_str != neu_str:
                if field not in lead.gesperrte_felder:
                    lead.gesperrte_felder.append(field)
                store.add_change_entry(AenderungEntry(
                    lead_id=lead.lead_id,
                    firma=lead.firma,
                    feld=field,
                    alt=alt_str,
                    neu=neu_str,
                    benutzer=payload.benutzer or "KAM",
                    zeitpunkt=now_iso,
                    grund=payload.grund,
                ))
                if field not in ("website", "teilscores"):
                    setattr(lead, field, new_val)

        lead.aktualisiert_am = now_iso
        lead._force_manual_update = True
        store.upsert_lead(lead)
        return {"success": True, "lead": lead.model_dump()}

    # 6d. Neu bewerten (POST)
    @app.post("/api/leads/{lead_id}/neu-bewerten")
    def neu_bewerten(lead_id: str, store: LeadStore = Depends(get_lead_store)) -> dict[str, Any]:
        all_leads = store.get_all_leads()
        lead = None
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                lead = l
                break
        if not lead:
            raise HTTPException(status_code=404, detail="Lead nicht gefunden.")

        old_status = lead.status
        lead.status = "zu bewerten"
        bewertung_fields = {"score", "klasse", "teilscores", "begruendung"}
        lead.gesperrte_felder = [f for f in lead.gesperrte_felder if f not in bewertung_fields]
        lead.score_manuell = False
        now_iso = datetime.now(timezone.utc).isoformat()
        store.add_change_entry(AenderungEntry(
            lead_id=lead.lead_id,
            firma=lead.firma,
            feld="status",
            alt=old_status,
            neu="zu bewerten",
            benutzer="KAM",
            zeitpunkt=now_iso,
            grund="Neu bewerten angestossen",
        ))
        lead.aktualisiert_am = now_iso
        lead._force_manual_update = True
        store.upsert_lead(lead)
        return {"success": True, "lead": lead.model_dump()}

    # 6e. Sperre aufheben (POST)
    @app.post("/api/leads/{lead_id}/sperre-aufheben")
    def sperre_aufheben(
        lead_id: str,
        payload: SperreAufhebenPayload,
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

        if payload.feld in lead.gesperrte_felder:
            lead.gesperrte_felder.remove(payload.feld)
        if payload.feld in ("score", "klasse", "teilscores"):
            if not any(f in lead.gesperrte_felder for f in ("score", "klasse", "teilscores")):
                lead.score_manuell = False

        now_iso = datetime.now(timezone.utc).isoformat()
        store.add_change_entry(AenderungEntry(
            lead_id=lead.lead_id,
            firma=lead.firma,
            feld=f"sperre:{payload.feld}",
            alt="gesperrt",
            neu="freigegeben",
            benutzer="KAM",
            zeitpunkt=now_iso,
            grund="Sperre aufgehoben",
        ))
        lead.aktualisiert_am = now_iso
        lead._force_manual_update = True
        store.upsert_lead(lead)
        return {"success": True, "lead": lead.model_dump()}

    # 6f. Änderungsverlauf eines Leads (GET)
    @app.get("/api/leads/{lead_id}/aenderungen")
    def list_aenderungen(lead_id: str, store: LeadStore = Depends(get_lead_store)) -> list[dict[str, Any]]:
        all_leads = store.get_all_leads()
        actual_lead_id = lead_id
        for l in all_leads:
            if l.lead_id == lead_id or normalize_domain(l.domain) == normalize_domain(lead_id):
                actual_lead_id = l.lead_id
                break
        entries = store.get_change_entries(actual_lead_id)
        entries.sort(key=lambda x: x.zeitpunkt, reverse=True)
        return [e.model_dump() for e in entries]

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
            ("Klasse", "klasse"), ("Score manuell", "score_manuell"), ("Gesperrte Felder", "gesperrte_felder"),
            ("Status", "status"), ("Kanal", "quelle_kanal"), ("Historie", "historie"),
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
                elif key == "score_manuell":
                    v = "ja" if v else "nein"
                elif key == "gesperrte_felder":
                    v = ", ".join(v) if isinstance(v, list) else (str(v) if v else "")
                row.append("" if v is None else v)
            writer.writerow(row)

        csv_content = output.getvalue().encode("utf-8")
        filename = f"projekt7_Leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # 7b. Neuer Export aller Änderungen (GET /api/aenderungen.csv)
    @app.get("/api/aenderungen.csv")
    def export_aenderungen_csv(store: LeadStore = Depends(get_lead_store)) -> Response:
        entries = store.get_change_entries()
        entries.sort(key=lambda x: x.zeitpunkt, reverse=True)

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        cols = ["Lead ID", "Unternehmen", "Feld", "Alter Wert", "Neuer Wert", "Benutzer", "Zeitpunkt", "Grund"]
        writer.writerow(cols)
        for e in entries:
            writer.writerow([
                e.lead_id,
                e.firma or "",
                e.feld,
                e.alt or "",
                e.neu or "",
                e.benutzer,
                e.zeitpunkt,
                e.grund or "",
            ])

        csv_content = output.getvalue().encode("utf-8")
        filename = f"projekt7_Aenderungen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
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
