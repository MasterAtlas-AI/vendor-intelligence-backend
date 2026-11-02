"""Map extracted capability terms onto taxonomy codes: the local vocabulary first (free), the AI only for leftovers."""

import re

from app.ai.service import AiService, CallContext
from app.discovery.prompts import CLASSIFY
from app.discovery.schemas import Classification
from app.modules.imports.importer import Taxonomy
from app.modules.imports.pdf_extract import PhraseIndex

KINDS = ("equipment", "services", "certifications", "industries")


def taxonomy_text(tax: Taxonomy) -> str:
    lines = []
    for kind in KINDS:
        for code, labels in tax.labels.get(kind, []):
            lines.append(f"{kind} / {code}: {labels[0]}")
    return "\n".join(lines)


_PHRASES: dict[int, PhraseIndex] = {}


def _phrase_index(tax: Taxonomy) -> PhraseIndex:
    """The PDF importer's phrase matcher over this taxonomy, built once per taxonomy object."""
    index = _PHRASES.get(id(tax))
    if index is None:
        _PHRASES.clear()  # one taxonomy is loaded per run; don't keep old ones alive
        index = _PHRASES[id(tax)] = PhraseIndex(tax.labels)
    return index


def local_code(tax: Taxonomy, kind: str, *terms: str) -> str | None:
    """A code without any AI call: an exact name/synonym first, then a known phrase inside the text ("HX-2 BB2
    pump" -> BB2) when all phrases found point down a single branch of the tree (the deepest is used)."""
    vocab = {"equipment": tax.equipment, "services": tax.services, "certifications": tax.certifications,
             "industries": tax.industries}[kind]  # fmt: skip
    for term in terms:
        if term and (code := vocab.resolve(term)):
            return code
    found = {
        code for k, code, _, _ in _phrase_index(tax).find(" | ".join(t for t in terms if t)) if k == kind
    }
    deepest = max(found, key=lambda c: c.count("."), default=None)
    if deepest and all(deepest == c or deepest.startswith(c + ".") for c in found):
        return deepest
    return None


async def classify(
    pending: list[tuple[str, str]], tax: Taxonomy, ai: AiService, call: CallContext
) -> dict[tuple[str, str], str]:
    """(kind, term) -> code for terms the local vocabulary couldn't place. Unknown or invented codes are dropped."""
    unique = list(dict.fromkeys(pending))
    if not unique:
        return {}
    result = await ai.run(
        CLASSIFY,
        Classification,
        call,
        taxonomy=taxonomy_text(tax),
        terms=[f"{kind}: {term}" for kind, term in unique],
    )
    out: dict[tuple[str, str], str] = {}
    # The AI is asked to echo "kind: term"; accept it with or without the kind, in any case or punctuation.
    by_term: dict[str, tuple[str, str]] = {}
    for kind, term in unique:
        by_term[_norm(f"{kind}: {term}")] = (kind, term)
        by_term.setdefault(_norm(term), (kind, term))
    for item in result.output.terms:
        key = by_term.get(_norm(item.term))
        if key is None or not item.code:
            continue
        kind = key[0]
        code = re.sub(r"^\s*[a-z]+\s*[/:]\s*", "", item.code.strip())  # "equipment / pumps.x" -> "pumps.x"
        if (kind, code) in tax.ids:  # the AI may only pick existing codes of the right kind
            out[key] = code
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
