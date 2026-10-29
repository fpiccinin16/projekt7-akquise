"""Firecrawl API Client mit integriertem Credit-Tracking und Fehlerbehandlung."""

from __future__ import annotations

import os
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Bekannte Directory- und Aggregator-Domains, die ignoriert werden sollen
BLOCKED_DOMAINS = {
    "google.com",
    "google.ch",
    "bing.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "wikipedia.org",
    "local.ch",
    "search.ch",
    "gelbeseiten.de",
    "moneyhouse.ch",
    "zefix.ch",
    "kompass.com",
    "europages.ch",
    "wlw.de",
    "kununu.com",
    "de.wikipedia.org",
    "jobs.ch",
    "jobup.ch",
    "indeed.com",
    "ch.indeed.com",
    "easymonitoring.ch",
    "help.ch",
    "firmenwissen.ch",
    "northdata.de",
    "northdata.com",
    "tagblatt.ch",
    "srf.ch",
    "nzz.ch",
    "handelszeitung.ch",
    "swissfirms.ch",
    "projektsieben.ch",
}


class FirecrawlClient:
    """Wrapper um das Firecrawl SDK zur Websuche und Scraping mit Credit-Tracking."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("FIRECRAWL_API_KEY")
        self._app: Any = None
        self.credits_used: float = 0.0
        self.call_count: int = 0

    @property
    def app(self) -> Any:
        if self._app is None:
            if not self.api_key:
                raise ValueError("FIRECRAWL_API_KEY ist weder im Konstruktor noch in .env gesetzt.")
            from firecrawl import FirecrawlApp
            self._app = FirecrawlApp(api_key=self.api_key)
        return self._app

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Führt eine Firecrawl-Suche aus.
        
        Gibt eine Liste von Treffern mit url, title, description zurück.
        Schätzt/protokolliert 1 Credit pro Suchaufruf.
        """
        self.call_count += 1
        self.credits_used += 1.0  # Firecrawl Standard: 1 Credit pro Search

        for attempt in range(3):
            try:
                res = self.app.search(query, limit=limit)
                items: list[dict[str, Any]] = []

                # Unterstützung verschiedener SDK-Rückgabetypen (Objekt oder Dict)
                raw_items = []
                if hasattr(res, "web") and res.web:
                    raw_items = res.web
                elif isinstance(res, dict) and "data" in res:
                    raw_items = res["data"]
                elif isinstance(res, list):
                    raw_items = res

                for item in raw_items:
                    url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else "")
                    title = getattr(item, "title", None) or (item.get("title") if isinstance(item, dict) else "")
                    desc = getattr(item, "description", None) or (item.get("description") if isinstance(item, dict) else "")
                    if url:
                        items.append({"url": str(url), "title": str(title or ""), "description": str(desc or "")})

                return items
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "rate limit" in err_msg:
                    time.sleep(2 * (attempt + 1))
                    continue
                # Nicht-Rate-Limit-Fehler: leere Liste zurückgeben, kein Abbruch
                return []
        return []

    def scrape(
        self,
        url: str,
        formats: list[str] | None = None,
        only_main_content: bool = True,
    ) -> dict[str, Any]:
        """Scraped eine URL und gibt Markdown/Metadata zurück.
        
        Protokolliert 1 Credit pro Scrape.
        """
        if not url:
            return {}

        self.call_count += 1
        self.credits_used += 1.0

        formats = formats or ["markdown"]

        for attempt in range(3):
            try:
                res = self.app.scrape(url, formats=formats, only_main_content=only_main_content)
                markdown = ""
                metadata: dict[str, Any] = {}

                if hasattr(res, "markdown"):
                    markdown = res.markdown or ""
                elif isinstance(res, dict):
                    markdown = res.get("markdown", "")

                if hasattr(res, "metadata"):
                    metadata = res.metadata if isinstance(res.metadata, dict) else {}
                elif isinstance(res, dict) and "metadata" in res:
                    metadata = res["metadata"]

                return {
                    "markdown": markdown,
                    "metadata": metadata,
                    "url": url,
                }
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "rate limit" in err_msg:
                    time.sleep(2 * (attempt + 1))
                    continue
                return {"markdown": "", "metadata": {}, "url": url, "error": str(e)}

        return {"markdown": "", "metadata": {}, "url": url, "error": "Max retries reached"}
