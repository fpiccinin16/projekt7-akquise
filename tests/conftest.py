import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p7_akquise.storage.excel_store import ExcelStore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_blob(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.delenv("VERCEL", raising=False)


@pytest.fixture
def store(tmp_path):
    return ExcelStore(tmp_path / "leads.xlsx")


class FakeWeb:
    """Web-Client-Attrappe: Seiten und Suchtreffer aus Dictionaries."""

    def __init__(self, pages=None, search_results=None, companies=None):
        self.pages = pages or {}
        self.search_results = search_results or {}
        self.companies = companies or {}
        self.credits_used = 0.0
        self.searches = []

    def scrape(self, url, **_):
        self.credits_used += 1
        return {"markdown": self.pages.get(url.rstrip("/"), "")}

    def search(self, query, limit=10):
        self.credits_used += 1
        self.searches.append(query)
        for key, res in self.search_results.items():
            if key.lower() in query.lower():
                return res[:limit]
        return []

    def find_companies(self, query, max_items=10):
        self.credits_used += 1
        return self.companies.get(query, [])[:max_items]


class FakeLLM:
    def __init__(self, response=None, by_domain=None):
        self.response = response or {}
        self.by_domain = by_domain or {}

    def generate_json(self, prompt, system_prompt=None):
        for dom, resp in self.by_domain.items():
            if dom in prompt:
                return resp
        return self.response


@pytest.fixture
def fake_web():
    return FakeWeb


@pytest.fixture
def fake_llm():
    return FakeLLM
