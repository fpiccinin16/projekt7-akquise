"""Pydantic v2 Datenmodelle für die projekt7 KI-Akquise Pipeline."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, field_validator


def normalize_domain(url_or_domain: str) -> str:
    """Normalisiert eine Domain oder URL auf die Kern-Domain.

    Beispiele:
        'https://www.firma.ch/ueber-uns?ref=1' -> 'firma.ch'
        'WWW.SUB.FIRMA.DE/' -> 'sub.firma.de'
        'firma.ch' -> 'firma.ch'
    Schlüssel ohne Website (aus importierten Adresslisten) beginnen mit 'name:' und bleiben unverändert.
    """
    if not url_or_domain:
        return ""

    raw = url_or_domain.strip()
    if raw.lower().startswith("name:"):
        return "name:" + raw[5:].strip().lower()

    raw = raw.lower()
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raw = "http://" + raw

    parsed = urlparse(raw)
    domain = parsed.netloc or parsed.path.split("/")[0]
    domain = domain.split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.strip("/")


def name_key(firma: str, ort: str | None = None) -> str:
    """Ersatzschlüssel für Adressen ohne Website: 'name:<firma>|<ort>'."""
    slug = re.sub(r"[^a-z0-9äöü]+", "-", f"{firma} {ort or ''}".lower()).strip("-")
    return f"name:{slug}"


def has_real_domain(domain: str) -> bool:
    return bool(domain) and not domain.startswith("name:")


class Evidence(BaseModel):
    """Beleg für ein Kriterium."""
    zitat: str = Field(..., max_length=300, description="Wörtlicher Auszug, max. 300 Zeichen")
    url: HttpUrl = Field(..., description="Quell-URL")

    @field_validator("zitat", mode="before")
    @classmethod
    def validate_zitat(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("Zitat muss ein String sein.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Zitat darf nicht leer sein.")
        if len(cleaned) > 300:
            return cleaned[:297] + "..."
        return cleaned


class CriterionResult(BaseModel):
    """Ergebnis der LLM-Extraktion für ein einzelnes Kriterium."""
    kriterium: str
    erfuellt: bool | None = Field(default=None, description="None = unbekannt -> 0 Punkte")
    wert: str | None = Field(default=None, description="z. B. 'ca. 45 MA', 'Ostschweiz'")
    belege: list[Evidence] = Field(default_factory=list, description="Leer -> 0 Punkte, egal was erfuellt sagt")


class Company(BaseModel):
    """Basis-Unternehmensdaten."""
    firma: str
    domain: str
    website: str | None = None
    ort: str | None = None
    land: str = "CH"
    branche: str | None = None
    groesse: str | None = None

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        return normalize_domain(v)


LeadStatus = Literal[
    "zu bewerten",   # importierte Adresse, noch nicht recherchiert
    "neu",
    "beobachten",
    "zur Prüfung",
    "freigegeben",
    "verworfen",
    "kontaktiert",
    "nachgefasst_1",
    "nachgefasst_2",
    "interesse",
    "termin",
    "kalt",
    "fehler",
]

LeadKlasse = Literal["A", "B", "C"]
# A = Produktion, B = Handel, C = andere Branche (nicht Zielsegment)
LeadSegment = Literal["A", "B", "C"]

KANAL_IMPORT = "Adressliste"
KANAL_WEB = "KI-Websuche"


class Lead(BaseModel):
    """Vollständiger Lead-Datensatz (ein Datensatz pro Firma, Schlüssel = Domain)."""
    lead_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    domain: str
    firma: str
    website: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    kanton: str | None = None
    land: str = "CH"
    segment: LeadSegment = "C"
    branche: str | None = None
    groesse: str | None = None
    kaufsignal: str | None = None
    angebot: str | None = None
    kontakt_name: str | None = None
    kontakt_rolle: str | None = None
    kontakt_email: str | None = None
    kontakt_tel: str | None = None
    score: int = Field(default=0, ge=0, le=100)
    klasse: LeadKlasse = "C"
    teilscores: dict[str, int] = Field(default_factory=dict)
    begruendung: str = ""
    quellen: list[str] = Field(default_factory=list)
    modell_version: str = "1.0.0"
    quelle_kanal: str = KANAL_WEB
    import_datei: str | None = None
    historie: str | None = Field(default=None, description="Frühere Kontakthistorie aus der importierten Liste (nur Info)")
    status: LeadStatus = "neu"
    freigabe: Literal["ja", "nein"] | None = None
    manuelle_freigabe: bool = Field(default=False, description="Freigabe durch Berater trotz Klasse B/C (Score unter Schwelle)")
    verwerfungsgrund: str | None = None
    kommentar_berater: str | None = None
    kontaktiert_am: str | None = None
    anzahl_nachfass: int = 0
    wiedervorlage_am: str | None = None
    mail_entwurf: str | None = None
    erstellt_am: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    aktualisiert_am: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        return normalize_domain(v)

    @field_validator("plz", mode="before")
    @classmethod
    def plz_as_text(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).strip()


class ExclusionEntry(BaseModel):
    """Eintrag auf der Ausschlussliste (Blatt Ausschluss)."""
    domain: str
    firma: str | None = None
    grund: str  # Bestandskunde / Wettbewerber / Opt-out
    eingetragen_am: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        return normalize_domain(v)


class ProtocolEntry(BaseModel):
    """Eintrag im Protokollblatt (Blatt Protokoll)."""
    lauf_id: str
    datum: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    modell_version: str = "1.0.0"
    anzahl_kandidaten: int = 0
    anzahl_import_bewertet: int = 0
    duplikate: int = 0
    anzahl_a: int = 0
    anzahl_b: int = 0
    anzahl_c: int = 0
    fehler: int = 0
    verbrauchte_credits: float = 0.0


class RejectedLead(BaseModel):
    """Verworfener Lead (Blatt Verworfen)."""
    domain: str
    firma: str | None = None
    grund: str
    verworfen_am: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        return normalize_domain(v)


class ScoringModelConfig(BaseModel):
    """Validierungsmodell für config/scoring_model.yaml."""
    version: str
    gueltig_ab: str
    freigegeben_durch: str
    schwellen: dict[str, int]
    ko_kriterien: list[str]
    kriterien: dict[str, Any]
    labels: dict[str, str] = Field(default_factory=dict)
