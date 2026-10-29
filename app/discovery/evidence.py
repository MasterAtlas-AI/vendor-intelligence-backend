"""The evidence rule: a fact extracted by the AI is kept only if it cites a page we actually fetched (or that web search
returned) and its quote really appears in that page. This stops invented facts and facts injected by page content
that doesn't exist where it is claimed to be."""

import re
from dataclasses import dataclass

from app.crawler.urls import normalize_url
from app.discovery.schemas import FactEvidence

QUOTE_MIN_MATCH = 24  # characters of a quote that must appear verbatim (after whitespace/case folding)


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class EvidenceCheck:
    """Pages the pipeline retrieved, by normalised URL."""

    pages: dict[str, str]  # url -> text
    titles: dict[str, str]

    @classmethod
    def build(cls, pages: list[tuple[str, str, str]]) -> "EvidenceCheck":
        """pages: (url, title, text) triples; several URLs (requested/final) may point at the same text."""
        texts: dict[str, str] = {}
        titles: dict[str, str] = {}
        for url, title, text in pages:
            if (key := normalize_url(url)) is not None:
                texts[key] = _fold(text)
                titles[key] = title
        return cls(texts, titles)

    def verify(self, evidence: list[FactEvidence]) -> list[dict[str, str]]:
        """Evidence entries that hold up, as proposal evidence dicts (empty list = drop the fact)."""
        out: list[dict[str, str]] = []
        for ev in evidence:
            key = normalize_url(ev.url)
            if key is None or key not in self.pages:
                continue
            quote = _fold(ev.quote)
            if len(quote) < 8:
                continue
            probe = quote if len(quote) <= QUOTE_MIN_MATCH else quote[:QUOTE_MIN_MATCH]
            text = self.pages[key]
            if probe not in text and quote[-QUOTE_MIN_MATCH:] not in text:
                continue
            if all(o["url"] != key for o in out):
                out.append({"kind": "web", "url": key, "title": self.titles.get(key, "")[:300],
                            "snippet": ev.quote.strip()[:2000]})  # fmt: skip
        return out
