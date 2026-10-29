"""Speicher-Schnittstellen und Implementierungen."""

from p7_akquise.storage.base import LeadStore
from p7_akquise.storage.excel_store import ExcelStore

__all__ = ["LeadStore", "ExcelStore"]
