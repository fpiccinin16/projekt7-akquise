# projekt7 KI-Akquise (Leadgenerierung Produktion und Handel)

Automatisierte Leadgenerierung für das Team **Produktion und Handel** der projekt7 AG, aufgebaut wie das SYMA-Akquise-System:
vorhandene Adresslisten importieren und neu bewerten, neue Zielfirmen per KI-Websuche finden, deterministisch bewerten,
im Dashboard prüfen und die Mail-Sequenz (Erstmail, Nachfass 1 und 2 im Wochentakt) begleiten.

Zielprofil (aus dem Interview zur LeadRise-Kampagne): Produktions- und Handelsunternehmen, Ost- bzw. Deutschschweiz,
deutschsprachig, 20 bis 100 Mitarbeitende, Ansprechperson Geschäftsführung, Inhaber, Leitung Finanzen oder Leitung Operations.

---

## Grundsätze

1. **Die KI extrahiert, eine feste Regel rechnet.** Claude liest die Firmenwebsite und liefert Fakten mit Zitat und Quelle. Der Score (0–100, Klassen A/B/C) wird in Python aus `config/scoring_model.yaml` berechnet.
2. **Kein Beleg, keine Punkte.** Jedes Zitat wird gegen den geladenen Seitentext geprüft; erfundene Zitate geben 0 Punkte.
3. **K.o.-Kriterien:** Bestandskunde, Wettbewerber (ERP-Anbieter, Abacus-Partner), ausserhalb Deutschschweiz → Score 0.
4. **Schwellen:** A ≥ 70 (Prüfung durch KAM), B 50–69 (Wiedervorlage nach 28 Tagen), C < 50 (verworfen, im Dashboard sichtbar).
5. **Kein automatischer Mailversand.** Entwürfe werden erzeugt, der Key Account Manager versendet selbst (UWG Art. 3 Abs. 1 lit. o).
6. **Audit-Trail:** Jeder Lead speichert die Modellversion; jeder Lauf wird im Blatt «Protokoll» festgehalten.
7. **Speicher:** Excel-Arbeitsmappe `data/projekt7_Leads.xlsx`, auf Vercel und in GitHub Actions gemeinsam über Vercel Blob.

## Ablauf eines Laufs (`python -m p7_akquise.pipeline`)

| Schritt | Was passiert |
| --- | --- |
| 1 | Freigaben/Verwerfungen aus der Excel-Datei übernehmen |
| 1b | Mail-Entwürfe für freigegebene Leads, Nachfass 1 nach 5 Werktagen, Nachfass 2 nach 10, danach «kalt» |
| 2 | Importierte Adressen («zu bewerten») recherchieren: Website ermitteln (falls fehlend), Seiten lesen, bewerten |
| 3 | Neue Zielfirmen per KI-Websuche (Suchbegriffe je Segment in `config/segmente.yaml`) |
| 4 | Abgleich gegen Bestand, Ausschlussliste und Verworfen; fällige B-Leads werden neu bewertet |
| 5/6 | Recherche (bis 5 Seiten inkl. Karriere-Seite), Belegprüfung, Scoring |
| 7 | Speichern und Protokoll |
| 8 | Wochenübersicht per Mail (optional, SMTP) |

## Adressliste importieren

Im Dashboard über **«Adressliste importieren»** (oder per API `POST /api/import`).

- Formate: `.xlsx`, `.xlsm`, `.csv` (Semikolon oder Komma). Vorlage: `GET /api/import/vorlage.xlsx`.
- Spalten werden automatisch erkannt (Firma, Website, Strasse, PLZ, Ort, Kanton, Branche, Mitarbeitende, Anrede, Vorname, Nachname, Funktion, E-Mail, Telefon, Bemerkung, Angeschrieben …). Vor dem Import zeigt eine Vorschau die Zuordnung.
- Alle Firmen werden **neu bewertet** (Status «zu bewerten»). Bemerkungen, Häkchen und durchgestrichene Zeilen bleiben als **Historie** sichtbar.
- Fehlt die Website, wird sie aus der E-Mail-Domain abgeleitet oder im Lauf per Websuche ermittelt. Ergibt sich dabei eine Dublette, werden die Datensätze zusammengeführt.
- Firmen auf der Ausschlussliste werden übersprungen. Bereits vorhandene Firmen werden ergänzt, nicht überschrieben.
- Pro Lauf werden höchstens `import_bewertungen_pro_lauf` Adressen bewertet (Standard 60). Grosse Listen laufen über mehrere Läufe oder einen manuellen Lauf mit höherem Wert.

## Dashboard «projekt7 Lead-Cockpit»

KPIs (A-Leads zur Prüfung, Adressen zu bewerten, fällige Nachfass-Mails, Termine), Filter nach Status, Klasse, Segment und Herkunft,
Detailansicht mit Teilscores, Quellen, Historie und Ansprechperson, Freigabe/Verwerfen mit Pflichtgrund, Mail-Entwurf zum Kopieren,
Rückmeldungen «Termin vereinbart», «Interesse», «Später interessant» (Wiedervorlage 90 Tage) und «Absage», CSV-Export.

```bash
pip install -e ".[dev]"
STORAGE_BACKEND=local uvicorn p7_akquise.dashboard.api:app --reload --port 8000
```

## Lauf ausführen

```bash
python -m p7_akquise.pipeline --dry-run                          # Testlauf ohne Schreiben
python -m p7_akquise.pipeline --max-kandidaten 0 --max-import 200  # nur importierte Adressen bewerten
python -m p7_akquise.pipeline                                    # regulärer Lauf
```

## Betrieb (wie SYMA)

- **GitHub Actions** (`.github/workflows/weekly.yml`): jeden Montag 06:00 Zürich, manuell mit Dry-Run, `max_kandidaten`, `max_import`.
  Secrets: `ANTHROPIC_API_KEY`, `BLOB_READ_WRITE_TOKEN`, optional `DIGEST_TO`, `SMTP_*`; Variable `DASHBOARD_URL`.
- **Vercel**: `vercel.json` und `api/index.py` stellen das Dashboard bereit; Blob-Speicher mit dem Projekt verbinden.
  Für den Knopf «Lauf starten»: `GITHUB_TOKEN` und `GITHUB_REPO` setzen.

## Konfiguration

| Datei | Inhalt |
| --- | --- |
| `config/scoring_model.yaml` | Kriterien, Punkte, K.o., Schwellen, Labels. **Entwurf, Freigabe durch projekt7 ausstehend.** |
| `config/segmente.yaml` | Segmente A Produktion, B Handel, Suchbegriffe, Kantone der Zielregion |
| `config/settings.yaml` | Limits pro Lauf, Nachfass-Takt, Speicherpfad |
| `prompts/extraction.md` | Verbindlicher Extraktionsprompt |

## Tests

```bash
python -m pytest -q
```

## Offene Punkte vor dem Pilot

- Scoring-Modell mit projekt7 abstimmen und freigeben (Gewichte, Grössenbereich, Zielregion).
- Ausschlussliste befüllen (Bestandskunden, laufende Offerten, Opt-outs).
- Absendername und Signatur in `steps/drafts.py` (`ABSENDER`) setzen.
- Klären, ob frühere Absagen aus der Adressliste erneut angeschrieben werden dürfen (Historie im Dashboard prüfen).
