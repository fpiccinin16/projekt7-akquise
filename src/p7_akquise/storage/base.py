"""Abstraktes Interface LeadStore für Speicher-Backends (Excel, später CRM)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from p7_akquise.models import ExclusionEntry, Lead, ProtocolEntry, RejectedLead


class LeadStore(ABC):
    """Abstrakte Basisklasse für alle Lead-Speicher."""

    @abstractmethod
    def init_store(self) -> None:
        """Initialisiert den Speicher (Tabellen, Blätter, Schemata falls nicht vorhanden)."""
        pass

    @abstractmethod
    def upsert_lead(self, lead: Lead) -> None:
        """Fügt einen Lead ein oder aktualisiert ihn anhand der normalisierten Domain."""
        pass

    @abstractmethod
    def upsert_leads(self, leads: list[Lead]) -> int:
        """Fügt mehrere Leads ein oder aktualisiert sie. Gibt die Anzahl verarbeiteter Leads zurück."""
        pass

    @abstractmethod
    def get_lead_by_domain(self, domain: str) -> Lead | None:
        """Gibt den Lead zur Domain zurück, oder None wenn nicht vorhanden."""
        pass

    @abstractmethod
    def get_all_leads(self, filters: dict[str, Any] | None = None) -> list[Lead]:
        """Gibt alle Leads zurück, optional gefiltert nach Status, Klasse etc."""
        pass

    @abstractmethod
    def get_exclusion_list(self) -> list[ExclusionEntry]:
        """Liest die vollständige Ausschlussliste ein."""
        pass

    @abstractmethod
    def is_excluded(self, domain: str) -> bool:
        """Prüft, ob eine Domain auf der Ausschlussliste steht."""
        pass

    @abstractmethod
    def add_exclusion(self, entry: ExclusionEntry) -> None:
        """Fügt einen Eintrag zur Ausschlussliste hinzu."""
        pass

    @abstractmethod
    def add_protocol_entry(self, entry: ProtocolEntry) -> None:
        """Schreibt einen Lauf-Protokolleintrag."""
        pass

    @abstractmethod
    def get_protocol_entries(self) -> list[ProtocolEntry]:
        """Liest alle Lauf-Protokolleinträge ein."""
        pass

    def record_search_queries(self, queries: list[str], lauf_id: str, datum: str | None = None) -> None:
        """Speichert verwendete Suchbegriffe mit Datum für die Rotation."""
        pass

    def get_used_search_queries(self) -> dict[str, str]:
        """Gibt {suchbegriff: letztes_datum_iso} zurück."""
        return {}

    @abstractmethod
    def add_rejected_lead(self, rejected: RejectedLead) -> None:
        """Fügt einen verworfenen Lead zur Liste hinzu."""
        pass

    @abstractmethod
    def get_rejected_leads(self) -> list[RejectedLead]:
        """Liest alle verworfenen Leads ein."""
        pass

    @abstractmethod
    def update_feedback(
        self,
        domain: str,
        freigabe: str,
        verwerfungsgrund: str | None = None,
        kommentar_berater: str | None = None,
    ) -> Lead | None:
        """Aktualisiert das Berater-Feedback für einen Lead."""
        pass

    def remove_lead(self, lead_id: str) -> bool:
        """Entfernt einen Lead ohne Verwerfungs-Eintrag (optional je Backend)."""
        raise NotImplementedError

    @abstractmethod
    def delete_lead(self, domain_or_id: str) -> bool:
        """Löscht einen Lead aus dem aktiven Bestand und trägt ihn als verworfen ein."""
        pass
