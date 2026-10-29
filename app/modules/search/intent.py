"""Query understanding: turn "API 610 BB2 pumps for FPSO in Norway" into taxonomy codes, standards, countries and the
remaining keywords.

The local parser is instant and free: it reuses the PDF importer's phrase matcher over the taxonomy (names and
synonyms). The AI parser copes with free-form wording ("big barrel pumps for seawater injection offshore") and may only
return codes that exist; anything else it suggests is dropped.
"""

import re

from app.ai.prompts import DATA_RULE, Prompt, register
from app.ai.service import AiService, CallContext
from app.discovery.classify import _phrase_index, taxonomy_text
from app.domain.normalization import normalize_country
from app.modules.imports.importer import Taxonomy
from app.modules.search.schemas import SearchIntent

STANDARD = re.compile(
    r"\b(API|ISO|ASME|ANSI|NORSOK|IEC|IECEx|ATEX|NFPA|EN|DNV|DNVGL|ABS|PED|TEMA|NACE|BS|DIN|IEEE|UL)"
    r"[\s-]?(\d[\w.:/-]*|[A-Z]{1,3}-?\d[\w.:/-]*)",
    re.IGNORECASE,
)
# Kinds as named in Taxonomy.labels -> SearchIntent fields.
FIELDS = {
    "equipment": "equipment_categories",
    "services": "service_categories",
    "industries": "industries",
    "certifications": "certifications",
}
STOPWORDS = {"for", "the", "and", "with", "in", "of", "a", "an", "to", "on", "or", "from", "suppliers", "supplier",
             "vendors", "vendor", "manufacturers", "manufacturer", "companies", "company", "find", "show", "me",
             "who", "make", "makes", "suitable", "based", "located"}  # fmt: skip

PARSE = register(
    Prompt(
        name="search_parse",
        version=1,
        reasoning_effort="low",
        instructions=f"""You turn a procurement search query for industrial vendors into structured filters.
- Use only codes from the taxonomy given; pick the most specific code that the query clearly asks for, and nothing
  the query doesn't ask for.
- standards: engineering standards named or clearly implied (e.g. "API 610", "NORSOK M-650").
- countries: ISO 3166 alpha-2 codes for countries the query names as vendor locations.
- keywords: the important words not captured by the codes (materials, sizes, applications), else empty.
{DATA_RULE}""",
        template="Taxonomy (kind / code: name):\n{taxonomy}\n\nQuery:\n{query}",
    )
)


def _deepest(codes: set[str]) -> list[str]:
    """Drops codes whose descendant was also found ("pumps" and "pumps.centrifugal.api610.bb2" -> just the latter)."""
    return sorted(c for c in codes if not any(o != c and o.startswith(c + ".") for o in codes))


def parse_local(query: str, tax: Taxonomy) -> SearchIntent:
    found: dict[str, set[str]] = {k: set() for k in FIELDS}
    spans: list[tuple[int, int]] = []
    for kind, code, start, end in _phrase_index(tax).find(query):
        if kind in found:
            found[kind].add(code)
            spans.append((start, end))
    standards = []
    for m in STANDARD.finditer(query):
        standards.append(f"{m.group(1).upper()} {m.group(2).upper()}")
        spans.append(m.span())
    countries: list[str] = []
    country_words: set[str] = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z .'-]{2,30}", query):
        # Whole multi-word names first ("United Arab Emirates"), then single words; two-letter codes only in capitals.
        words = m.group(0).split()
        for size in (3, 2, 1):
            for i in range(len(words) - size + 1):
                phrase = " ".join(words[i : i + size])
                if len(phrase) > 3 and (iso := normalize_country(phrase)):
                    country_words.update(w.lower() for w in words[i : i + size])
                    if iso not in countries:
                        countries.append(iso)
    countries += [
        w for w in re.findall(r"\b[A-Z]{2}\b", query) if normalize_country(w) and w not in countries
    ]
    left = query
    for start, end in sorted(spans, reverse=True):
        left = left[:start] + " " + left[end:]
    keywords = " ".join(
        w
        for w in re.findall(r"[\w.+-]+", left)
        if w.lower() not in STOPWORDS and w.lower() not in country_words and len(w) > 1
    )
    return SearchIntent(
        **{FIELDS[k]: _deepest(v) for k, v in found.items()},
        standards=list(dict.fromkeys(standards)),
        countries=countries,
        keywords=keywords,
    )


def clean(intent: SearchIntent, tax: Taxonomy) -> SearchIntent:
    """Keeps only codes that exist (the AI, or a user editing chips, may send others) and valid countries."""
    kinds = {v: k for k, v in FIELDS.items()}
    values: dict[str, list[str]] = {}
    for field, kind in kinds.items():
        values[field] = _deepest({c for c in getattr(intent, field) if (kind, c) in tax.ids})[:10]
    countries = [c for c in (normalize_country(x) for x in intent.countries) if c]
    return SearchIntent(
        **values,
        standards=[s.strip()[:40] for s in intent.standards if s.strip()][:10],
        countries=list(dict.fromkeys(countries))[:20],
        keywords=intent.keywords.strip()[:300],
    )


async def parse_ai(query: str, tax: Taxonomy, ai: AiService, call: CallContext) -> SearchIntent:
    result = await ai.run(PARSE, SearchIntent, call, taxonomy=taxonomy_text(tax), query=query)
    return clean(result.output, tax)
