"""Synchronisiert die Excel-Datei mit einem Vercel-Blob-Speicher.

Damit nutzen Dashboard (Vercel) und Wochenlauf (GitHub Actions) dieselbe Datei.

- Jede Speicherung lädt eine neue Version mit eindeutigem Namen hoch (leads/projekt7_Leads-<zufall>.xlsx).
  So gibt es keine veralteten Zwischenspeicher-Kopien nach einem Überschreiben.
- Beim Lesen wird immer die neueste Version geholt. Die letzten 10 Versionen bleiben als Sicherung.

Aktiv, sobald Zugangsdaten vorhanden sind:
- auf Vercel automatisch (OIDC + BLOB_STORE_ID, wenn der Speicher mit dem Projekt verbunden ist)
- in GitHub Actions über das Secret BLOB_READ_WRITE_TOKEN
BLOB_ACCESS = private (Standard) oder public, je nach Einstellung des Speichers.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("p7_akquise.blob")

PREFIX = os.getenv("BLOB_PREFIX", "leads/projekt7_Leads")
KEEP_VERSIONS = 10


def blob_enabled() -> bool:
    if os.getenv("STORAGE_BACKEND", "").lower() == "local":
        return False
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN") or os.getenv("BLOB_STORE_ID"))


def _access() -> str:
    return os.getenv("BLOB_ACCESS", "private").lower()


def _versions():
    from vercel.blob import sync
    items = list(sync.query_blobs(prefix=PREFIX))
    items.sort(key=lambda b: str(b.uploaded_at), reverse=True)
    return items


def download_latest(target: Path) -> bool:
    """Lädt die neueste Version nach `target`. Gibt False zurück, wenn noch keine existiert."""
    try:
        from vercel.blob import sync
        versions = _versions()
        if not versions:
            logger.info("Noch keine Lead-Datei im Blob-Speicher.")
            return False
        latest = versions[0]
        result = sync.get_blob(latest.url, access=_access())
        body = getattr(result, "body", None)
        if body is None and hasattr(result, "read"):
            body = result.read()
        elif body is None and isinstance(result, (bytes, bytearray)):
            body = bytes(result)

        if not body or len(body) < 100 or not body.startswith(b"PK"):
            logger.warning("Heruntergeladene Datei ist kein gültiges Excel-Archiv (%s Bytes). Überspringe.", len(body) if body else 0)
            return False

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return True
    except Exception as e:
        logger.warning("Fehler beim Herunterladen aus Blob: %s", e)
        return False


def upload(source: Path) -> None:
    from vercel.blob import sync
    data = source.read_bytes()
    sync.put_blob(
        f"{PREFIX}.xlsx",
        data,
        access=_access(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        add_random_suffix=True,
    )
    try:
        old = _versions()[KEEP_VERSIONS:]
        if old:
            sync.delete_blob([b.url for b in old])
    except Exception as e:  # Aufräumen ist nicht kritisch
        logger.warning("Alte Blob-Versionen nicht gelöscht: %s", e)
