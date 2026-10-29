"""Deterministisches Scoring-Modell für projekt7 Leads (Schritt C6).

Grundsätze:
1. Die KI extrahiert, eine feste Regel rechnet deterministisch in Python.
2. Kein Beleg, keine Punkte: Jedes Kriterium braucht ein Zitat und eine URL, sonst 0 Punkte.
3. Ein K.o.-Kriterium führt unmittelbar zu Score 0 und Klasse C.
4. Schwellenwerte aus config/scoring_model.yaml steuern die Klassen A / B / C.
5. Die Modellversion wird mit jedem Score protokolliert (ISO 9001, 7.5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import yaml
from pydantic import BaseModel, Field

from p7_akquise.models import CriterionResult, LeadKlasse, ScoringModelConfig


class ScoringResult(BaseModel):
    """Ergebnis des deterministischen Scorings."""
    score: int = Field(ge=0, le=100)
    klasse: LeadKlasse
    teilscores: dict[str, int]
    begruendung: str
    is_ko: bool = False
    ko_kriterien_getriggert: list[str] = Field(default_factory=list)
    modell_version: str


def load_scoring_model(path: str | Path | None = None) -> ScoringModelConfig:
    """Lädt und validiert die Scoring-Modell-Konfiguration aus YAML."""
    if path is None:
        # Standardpfad relativ zum Projektverzeichnis
        base_dir = Path(__file__).resolve().parents[3]
        path = base_dir / "config" / "scoring_model.yaml"
    else:
        path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Scoring-Modell nicht gefunden unter: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return ScoringModelConfig.model_validate(data)


class ScoringEngine:
    """Deterministische Scoring-Engine nach Vorgaben von projekt7."""

    def __init__(self, config: ScoringModelConfig | None = None, config_path: str | Path | None = None) -> None:
        if config is not None:
            self.config = config
        else:
            self.config = load_scoring_model(config_path)

        self.version = self.config.version
        self.schwellen = self.config.schwellen  # z. B. {"A": 70, "B": 50}
        self.ko_kriterien = set(self.config.ko_kriterien)
        self.labels = self.config.labels or {}

    @staticmethod
    def _staged_points(points_def: dict[str, Any], wert: str | None) -> int:
        """Wählt die Punktestufe, deren Schlüssel im extrahierten Wert vorkommt (längster Treffer zuerst)."""
        val = (wert or "").strip().lower()
        for key in sorted((k for k in points_def if k != "andere"), key=len, reverse=True):
            if key.lower() in val:
                return int(points_def[key])
        return int(points_def.get("andere", 0))

    def determine_class(self, score: int) -> LeadKlasse:
        """Bestimmt die Lead-Klasse anhand der Schwellenwerte.
        
        A: Score >= Schwelle A (Standard: 70)
        B: Schwelle B <= Score < Schwelle A (Standard: 50..69)
        C: Score < Schwelle B (Standard: < 50)
        """
        a_threshold = self.schwellen.get("A", 70)
        b_threshold = self.schwellen.get("B", 50)

        if score >= a_threshold:
            return "A"
        elif score >= b_threshold:
            return "B"
        else:
            return "C"

    def score_lead(
        self,
        criteria_results: list[CriterionResult],
        ko_flags: dict[str, bool] | None = None,
    ) -> ScoringResult:
        """Berechnet deterministisch den Score, die Klasse und die Begründung.
        
        Args:
            criteria_results: Liste der vom LLM extrahierten Kriterienergebnisse.
            ko_flags: Optionale manuelle oder externe K.o.-Flags (z. B. aus Ausschlussliste).
        """
        teilscores: dict[str, int] = {}
        triggered_ko: list[str] = []

        # 1. K.o.-Prüfung aus externen Flags (z. B. Ausschlussliste)
        if ko_flags:
            for ko_key, is_triggered in ko_flags.items():
                if is_triggered and ko_key in self.ko_kriterien:
                    triggered_ko.append(ko_key)

        # 2. Extraktions-Ergebnisse mappen
        results_by_name = {cr.kriterium: cr for cr in criteria_results}

        # K.o.-Prüfung aus den extrahierten Kriterien
        for ko_name in self.ko_kriterien:
            cr = results_by_name.get(ko_name)
            if cr and cr.erfuellt is True:
                if ko_name not in triggered_ko:
                    triggered_ko.append(ko_name)

        # Wenn K.o. vorliegt: Sofortiger Abbruch mit Score 0
        if triggered_ko:
            begruendung = (
                f"Ausschluss durch K.o.-Kriterium: {', '.join(self.labels.get(k, k) for k in triggered_ko)}. "
                f"Lead wird mit Score 0 als Klasse C eingestuft."
            )
            return ScoringResult(
                score=0,
                klasse="C",
                teilscores={},
                begruendung=begruendung,
                is_ko=True,
                ko_kriterien_getriggert=triggered_ko,
                modell_version=self.version,
            )

        # 3. Kriterien berechnen gemäss Konfiguration
        # Flache Map aller Kriterien aus der YAML-Struktur (fit, bedarf, erreichbarkeit)
        cfg_kriterien: dict[str, Any] = {}
        for category, criteria_map in self.config.kriterien.items():
            if isinstance(criteria_map, dict):
                for crit_name, crit_def in criteria_map.items():
                    cfg_kriterien[crit_name] = crit_def

        total_score = 0
        erfuellte_highlights: list[str] = []
        fehlende_kriterien: list[str] = []

        for crit_name, crit_def in cfg_kriterien.items():
            cr = results_by_name.get(crit_name)

            # GRUNDSATZ 2: Kein Beleg, keine Punkte
            if not cr or not cr.belege or len(cr.belege) == 0:
                teilscores[crit_name] = 0
                fehlende_kriterien.append(crit_name)
                continue

            # Wenn nicht erfüllt oder unbestimmt: 0 Punkte
            if cr.erfuellt is not True:
                teilscores[crit_name] = 0
                fehlende_kriterien.append(crit_name)
                continue

            # Punkte ermitteln
            points_def = crit_def.get("punkte", 0)

            # Sonderfall: gestaffelte Punkte nach Wert (z. B. Region Ostschweiz / Deutschschweiz)
            if isinstance(points_def, dict):
                points = self._staged_points(points_def, cr.wert)
            else:
                points = int(points_def)

            teilscores[crit_name] = points
            total_score += points
            if points > 0:
                erfuellte_highlights.append(crit_name)

        # Deckelung auf 0-100 Punkte
        final_score = max(0, min(100, total_score))
        klasse = self.determine_class(final_score)

        # 4. Deterministische Begründung generieren (ohne LLM)
        begruendung = self._build_begruendung(
            score=final_score,
            klasse=klasse,
            teilscores=teilscores,
            erfuellte=erfuellte_highlights,
            fehlende=fehlende_kriterien,
        )

        return ScoringResult(
            score=final_score,
            klasse=klasse,
            teilscores=teilscores,
            begruendung=begruendung,
            is_ko=False,
            ko_kriterien_getriggert=[],
            modell_version=self.version,
        )

    def _build_begruendung(
        self,
        score: int,
        klasse: LeadKlasse,
        teilscores: dict[str, int],
        erfuellte: list[str],
        fehlende: list[str],
    ) -> str:
        """Erzeugt 2-3 prägnante Sätze auf Deutsch zur Begründung des Scores."""
        labels = self.labels

        top_treiber = [labels.get(k, k) for k in erfuellte]
        top_luecken = [labels.get(k, k) for k in fehlende]

        # Satz 1: Einstufung
        satz1 = f"Einstufung in Klasse {klasse} mit {score} von 100 Punkten (Modell v{self.version})."

        # Satz 2: Haupttreiber
        if top_treiber:
            treiber_text = ", ".join(top_treiber[:3])
            satz2 = f"Massgebliche Stärken: {treiber_text}."
        else:
            satz2 = "Es konnten keine verifizierten Kriterien belegt werden."

        # Satz 3: Potenzial / Lücken
        if klasse == "A":
            satz3 = "Hohe Passung zum Zielprofil Produktion und Handel; Prüfung durch den Key Account Manager empfohlen."
        elif klasse == "B":
            if top_luecken:
                satz3 = f"Solider Kandidat für Wiedervorlage; Optimierungspotenzial bei: {', '.join(top_luecken[:2])}."
            else:
                satz3 = "Solider Kandidat zur weiteren Beobachtung und Wiedervorlage."
        else:
            satz3 = "Kriterien reichen für eine aktive Akquise derzeit nicht aus (Klasse C)."

        return f"{satz1} {satz2} {satz3}"
