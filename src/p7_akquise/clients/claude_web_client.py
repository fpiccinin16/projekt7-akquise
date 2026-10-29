"""Web-Client auf Basis der Claude-API (Websuche und Seitenabruf als Server-Tools).

Ersetzt Firecrawl mit derselben Schnittstelle (search, scrape, credits_used, call_count),
damit Discover- und Research-Schritt unverändert bleiben. Es wird nur ANTHROPIC_API_KEY benötigt.

Kosten (Stand Okt. 2026, ohne Gewähr): Websuche 10 USD pro 1000 Suchen plus Tokens,
Seitenabruf ohne Zusatzgebühr, nur Tokens. Für das reine Abrufen wird ein günstiges Modell verwendet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("p7_akquise.web")

WEB_SEARCH_TOOL = os.getenv("CLAUDE_WEB_SEARCH_TOOL", "web_search_20250305")
WEB_FETCH_TOOL = os.getenv("CLAUDE_WEB_FETCH_TOOL", "web_fetch_20250910")
WEB_FETCH_BETA = os.getenv("CLAUDE_WEB_FETCH_BETA", "web-fetch-2025-09-10")
WEB_MODEL = os.getenv("CLAUDE_WEB_MODEL", "claude-haiku-4-5-20251001")
MAX_CONTENT_TOKENS = int(os.getenv("CLAUDE_WEB_MAX_CONTENT_TOKENS", "12000"))


def _as_dict(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


class ClaudeWebClient:
    """Websuche und Seitenabruf über die Server-Tools der Claude-API."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or WEB_MODEL
        self._client: Any = None
        self.credits_used: float = 0.0  # Anzahl Web-Aktionen (Suchen + Abrufe), für das Protokoll
        self.call_count: int = 0
        self.search_count: int = 0
        self.fetch_count: int = 0

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise ValueError("ANTHROPIC_API_KEY ist nicht gesetzt.")
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # ------------------------------------------------------------------ intern
    def _create(self, tools: list[dict[str, Any]], prompt: str, max_tokens: int = 256, beta: bool = False) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "tools": tools,
            "messages": [{"role": "user", "content": prompt}],
        }
        for attempt in range(3):
            try:
                if beta:
                    try:
                        resp = self.client.beta.messages.create(betas=[WEB_FETCH_BETA], **kwargs)
                    except Exception as e:  # Beta-Header nicht (mehr) nötig/erlaubt -> GA-Aufruf
                        if "beta" in str(e).lower():
                            resp = self.client.messages.create(**kwargs)
                        else:
                            raise
                else:
                    resp = self.client.messages.create(**kwargs)
                return [_as_dict(b) for b in resp.content]
            except Exception as e:
                msg = str(e).lower()
                if "429" in msg or "rate" in msg or "overloaded" in msg or "529" in msg:
                    time.sleep(5 * (attempt + 1))
                    continue
                logger.warning("Claude-Webaufruf fehlgeschlagen: %s", e)
                return []
        return []

    @staticmethod
    def _text_of(blocks: list[dict[str, Any]]) -> str:
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    # --------------------------------------------------------------- öffentlich
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Websuche. Gibt eine Liste von {url, title, description} zurück."""
        self.call_count += 1
        self.search_count += 1
        self.credits_used += 1.0
        blocks = self._create(
            tools=[{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 1,
                    "user_location": {"type": "approximate", "country": "CH"}}],
            prompt=f"Führe genau eine Websuche aus nach: {query}\nAntworte danach nur mit OK.",
            max_tokens=64,
        )
        items: list[dict[str, Any]] = []
        for b in blocks:
            if b.get("type") != "web_search_tool_result":
                continue
            content = b.get("content")
            if not isinstance(content, list):
                continue
            for r in content:
                if isinstance(r, dict) and r.get("url"):
                    items.append({"url": str(r["url"]), "title": str(r.get("title") or ""), "description": ""})
        return items[:limit]

    def scrape(self, url: str, formats: list[str] | None = None, only_main_content: bool = True) -> dict[str, Any]:
        """Ruft eine Seite ab und gibt deren Text als 'markdown' zurück (gleiches Format wie Firecrawl)."""
        if not url:
            return {}
        self.call_count += 1
        self.fetch_count += 1
        self.credits_used += 1.0
        blocks = self._create(
            tools=[{"type": WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": 1, "max_content_tokens": MAX_CONTENT_TOKENS}],
            prompt=f"Rufe genau diese Seite mit dem Werkzeug web_fetch ab: {url}\nAntworte danach nur mit OK.",
            max_tokens=64,
            beta=True,
        )
        for b in blocks:
            if b.get("type") != "web_fetch_tool_result":
                continue
            content = b.get("content") or {}
            if content.get("type") == "web_fetch_result":
                doc = content.get("content") or {}
                source = doc.get("source") or {}
                data = source.get("data") or ""
                if source.get("type") == "base64":  # z. B. PDF: nicht als Text verwertbar
                    data = ""
                return {"markdown": data, "metadata": {"title": doc.get("title")}, "url": content.get("url") or url}
            return {"markdown": "", "metadata": {}, "url": url, "error": content.get("error_code", "fetch_error")}
        return {"markdown": "", "metadata": {}, "url": url, "error": "kein Abrufergebnis"}

    def find_companies(self, query: str, max_items: int = 20) -> list[dict[str, str]]:
        """Websuche nach Zielfirmen. Gibt [{firma, website, ort}] konkreter Unternehmen zurück."""
        self.call_count += 1
        self.search_count += 1
        self.credits_used += 2.0
        prompt = (
            f"Suche im Web nach: {query}\n"
            "Nenne konkrete Unternehmen (Produktions- oder Handelsfirmen mit Sitz in der Deutschschweiz), "
            "die in den Suchergebnissen vorkommen. Nicht aufnehmen: Verzeichnisse, Jobportale, Medien, Verbände, "
            "Behörden, Software- oder ERP-Anbieter.\n"
            "Gib für jede Firma die offizielle Firmenwebsite an, wie sie in den Suchergebnissen steht.\n"
            f"Maximal {max_items} Einträge. Antworte ausschliesslich mit JSON: "
            '[{"firma": "…", "website": "https://…", "ort": "…"}]'
        )
        blocks = self._create(
            tools=[{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 5,
                    "user_location": {"type": "approximate", "country": "CH"}}],
            prompt=prompt,
            max_tokens=2000,
        )
        text = self._text_of(blocks)
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            logger.warning("Keine Firmenliste für Suche '%s' erhalten.", query)
            return []
        try:
            data = json.loads(match.group(0))
        except Exception:
            logger.warning("Firmenliste für '%s' nicht als JSON lesbar.", query)
            return []
        result = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("firma"):
                result.append({
                    "firma": str(item["firma"]).strip(),
                    "website": str(item.get("website") or "").strip(),
                    "ort": str(item.get("ort") or "").strip(),
                })
        return result[:max_items]


def get_default_web_client() -> Any:
    """Wählt den Web-Client: WEB_PROVIDER=firecrawl nutzt Firecrawl, sonst Claude (Standard)."""
    if os.getenv("WEB_PROVIDER", "claude").lower() == "firecrawl":
        from p7_akquise.clients.firecrawl_client import FirecrawlClient
        return FirecrawlClient()
    return ClaudeWebClient()
