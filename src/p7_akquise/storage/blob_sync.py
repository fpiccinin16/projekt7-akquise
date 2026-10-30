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
import threading
from pathlib import Path

logger = logging.getLogger("p7_akquise.blob")

PREFIX = os.getenv("BLOB_PREFIX", "leads/projekt7_Leads")
KEEP_VERSIONS = 10

_BLOB_LOCK = threading.Lock()
_CURRENT_BLOB_URL: str | None = None


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


def has_remote_blobs() -> bool:
    try:
        from vercel.blob import sync
        return len(list(sync.query_blobs(prefix=PREFIX))) > 0
    except Exception:
        return False


def download_latest(target: Path) -> bool:
    """Lädt die neueste gültige Version nach `target`. Gibt False zurück, wenn noch keine existiert."""
    global _CURRENT_BLOB_URL
    with _BLOB_LOCK:
        try:
            from vercel.blob import sync
            versions = _versions()
            if not versions:
                logger.info("Noch keine Lead-Datei im Blob-Speicher.")
                return False

            # Suche die neueste Version, die nicht leer/beschädigt ist
            for candidate in versions:
                # Leere Vorlagen haben ca. 9 KB. Echtes Dokument hat > 20 KB.
                if candidate.size < 20000 and len(versions) > 1 and any(v.size >= 20000 for v in versions):
                    continue

                if target.exists() and _CURRENT_BLOB_URL == candidate.url:
                    return True

                result = sync.get_blob(candidate.url, access=_access())
                body = getattr(result, "body", None)
                if body is None and hasattr(result, "read"):
                    body = result.read()
                elif body is None and isinstance(result, (bytes, bytearray)):
                    body = bytes(result)

                if not body or len(body) < 100 or not body.startswith(b"PK"):
                    logger.warning("Version %s ist kein gültiges Excel-Archiv (%s Bytes). Überspringe.", candidate.pathname, len(body) if body else 0)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_target = target.with_name(f"{target.name}.{os.getpid()}.tmp")
                tmp_target.write_bytes(body)
                os.replace(tmp_target, target)
                _CURRENT_BLOB_URL = candidate.url
                return True

            raise RuntimeError(f"Keine gültige Excel-Version im Blob gefunden ({len(versions)} Versionen geprüft).")
        except Exception as e:
            logger.error("Fehler beim Herunterladen aus Blob: %s", e)
            raise


def upload(source: Path) -> None:
    global _CURRENT_BLOB_URL
    with _BLOB_LOCK:
        from vercel.blob import sync
        data = source.read_bytes()
        res = sync.put_blob(
            f"{PREFIX}.xlsx",
            data,
            access=_access(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            add_random_suffix=True,
        )
        if hasattr(res, "url"):
            _CURRENT_BLOB_URL = res.url
        try:
            old = _versions()[KEEP_VERSIONS:]
            if old:
                sync.delete_blob([b.url for b in old])
        except Exception as e:  # Aufräumen ist nicht kritisch
            logger.warning("Alte Blob-Versionen nicht gelöscht: %s", e)
