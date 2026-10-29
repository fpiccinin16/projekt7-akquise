"""Vereinheitlichter LLM-Client für Gemini und Anthropic Claude mit JSON-Validierung."""

from __future__ import annotations

import json
import os
import re
from typing import Any, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """Abstrakter/Vereinheitlichter LLM-Client mit strukturierter JSON-Ausgabe."""

    def __init__(self, provider: str = "auto", model: str | None = None) -> None:
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        env_provider = os.getenv("LLM_PROVIDER")
        if provider == "auto":
            if env_provider:
                self.provider = env_provider.lower()
            elif self.anthropic_key:
                self.provider = "anthropic"
            elif self.gemini_key:
                self.provider = "gemini"
            else:
                self.provider = "none"
        else:
            self.provider = provider.lower()

        if self.provider == "gemini":
            self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        elif self.provider == "anthropic":
            self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
        else:
            self.model = model or "mock"

    def generate_json(self, prompt: str, system_prompt: str | None = None) -> dict[str, Any]:
        """Sendet einen Prompt an das gewählte LLM und parst die strukturierte JSON-Ausgabe."""
        if self.provider == "gemini":
            return self._call_gemini(prompt, system_prompt)
        elif self.provider == "anthropic":
            return self._call_anthropic(prompt, system_prompt)
        else:
            raise ValueError(f"Kein gültiger LLM-Provider konfiguriert (aktuell: {self.provider}).")

    def _call_gemini(self, prompt: str, system_prompt: str | None) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.gemini_key)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=system_prompt,
            temperature=0.1,
        )
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        text = response.text or "{}"
        return self._extract_json(text)

    def _call_anthropic(self, prompt: str, system_prompt: str | None) -> dict[str, Any]:
        import anthropic

        client = anthropic.Anthropic(api_key=self.anthropic_key)
        messages = [{"role": "user", "content": prompt}]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": messages,
            # Kein "temperature": Das Anthropic-SDK ab Version 2 akzeptiert den Parameter nicht mehr.
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = client.messages.create(**kwargs)
        text_content = ""
        for block in response.content:
            if hasattr(block, "text"):
                text_content += block.text

        return self._extract_json(text_content)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Extrahiert ein JSON-Objekt aus Text (auch mit Markdown-Fences)."""
        cleaned = text.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if match:
            cleaned = match.group(1).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            # Fallback: Suche erste geschweifte Klammer
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(cleaned[start : end + 1])
            raise ValueError(f"Konnte kein valides JSON aus LLM-Antwort extrahieren: {text[:200]}")
