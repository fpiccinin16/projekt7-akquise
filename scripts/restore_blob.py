"""Lead-Datei aus einer früheren Version im Vercel-Blob-Speicher wiederherstellen.

Der Speicher behält die letzten 10 Versionen (leads/projekt7_Leads-<zufall>.xlsx).
Dieses Skript zeigt alle Versionen mit der Anzahl Leads und macht eine gewählte Version
wieder zur neuesten. Es löscht nichts.

Aufruf (im Projektordner, mit dem Token aus Vercel → Storage → Blob → .env.local):
    set BLOB_READ_WRITE_TOKEN=vercel_blob_rw_...        (Windows)
    python scripts/restore_blob.py                      # Versionen anzeigen
    python scripts/restore_blob.py --restore 3          # Version Nr. 3 wiederherstellen
    python scripts/restore_blob.py --download 3         # Version Nr. 3 nur herunterladen

Optional: BLOB_PREFIX (Standard leads/projekt7_Leads), BLOB_ACCESS (Standard private).
"""

from __future__ import annotations

import argparse
import io
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(".env.local")
    load_dotenv(".env")
except Exception:
    pass

PREFIX = os.getenv("BLOB_PREFIX", "leads/projekt7_Leads")
ACCESS = os.getenv("BLOB_ACCESS", "private").lower()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def versions():
    from vercel.blob import sync
    items = list(sync.query_blobs(prefix=PREFIX))
    items.sort(key=lambda b: str(b.uploaded_at), reverse=True)
    return items


def download(blob) -> bytes:
    from vercel.blob import sync
    result = sync.get_blob(blob.url, access=ACCESS)
    body = getattr(result, "body", None)
    if body is None and hasattr(result, "read"):
        body = result.read()
    elif body is None and isinstance(result, (bytes, bytearray)):
        body = bytes(result)
    return body or b""


def count_leads(data: bytes) -> str:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        ws = wb["Leads"]
        n = sum(1 for row in ws.iter_rows(min_row=2, max_col=2, values_only=True) if row and row[1])
        wb.close()
        return str(n)
    except Exception as e:  # beschädigte oder leere Datei
        return f"nicht lesbar ({e.__class__.__name__})"


def main() -> None:
    if not (os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN")):
        sys.exit("BLOB_READ_WRITE_TOKEN ist nicht gesetzt.")
    p = argparse.ArgumentParser()
    p.add_argument("--restore", type=int, help="Nummer der Version, die wieder die neueste werden soll")
    p.add_argument("--download", type=int, help="Nummer der Version, die lokal gespeichert wird")
    args = p.parse_args()

    items = versions()
    if not items:
        sys.exit(f"Keine Versionen unter '{PREFIX}' gefunden. BLOB_PREFIX prüfen.")

    print(f"{'Nr':>3}  {'Hochgeladen':<26} {'Grösse':>10}  Leads")
    cache: dict[int, bytes] = {}
    for i, b in enumerate(items, start=1):
        data = download(b)
        cache[i] = data
        print(f"{i:>3}  {str(b.uploaded_at)[:25]:<26} {len(data):>10}  {count_leads(data)}")

    nr = args.restore or args.download
    if not nr:
        print("\nWähle die Version mit der erwarteten Anzahl Leads (vor dem Import) und starte erneut mit --restore <Nr>.")
        return
    if nr not in cache:
        sys.exit(f"Version {nr} gibt es nicht.")

    data = cache[nr]
    backup = f"projekt7_Leads_Version{nr}.xlsx"
    with open(backup, "wb") as fh:
        fh.write(data)
    print(f"\nVersion {nr} lokal gespeichert: {backup}")

    if args.restore:
        from vercel.blob import sync
        sync.put_blob(f"{PREFIX}.xlsx", data, access=ACCESS, content_type=XLSX, add_random_suffix=True)
        print(f"Version {nr} ist jetzt die neueste. Dashboard neu laden.")


if __name__ == "__main__":
    main()
