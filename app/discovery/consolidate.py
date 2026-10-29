"""From raw web-search findings to a ranked, de-duplicated list of candidate companies."""

from dataclasses import dataclass, field

from app.ai.ports import Citation
from app.discovery.schemas import FoundCompany
from app.domain.normalization import normalize_country, normalize_name, normalize_website, registrable_domain

# Sites that list or write about suppliers rather than being one. Their pages are useless as a vendor's website.
NOT_VENDOR_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "instagram.com", "wikipedia.org",
    "alibaba.com", "aliexpress.com", "made-in-china.com", "indiamart.com", "tradeindia.com", "globalsources.com",
    "thomasnet.com", "kompass.com", "europages.com", "directindustry.com", "zoominfo.com", "dnb.com", "crunchbase.com",
    "bloomberg.com", "reuters.com", "offshore-technology.com", "offshore-mag.com", "rigzone.com", "oilandgasiq.com",
    "amazon.com", "ebay.com", "yellowpages.com", "yelp.com", "glassdoor.com", "indeed.com", "google.com",
    "pumpsandsystems.com", "worldpumps.com", "processingmagazine.com", "hydrocarbonprocessing.com", "energy-pedia.com",
}  # fmt: skip
VENDOR_KINDS = {"manufacturer", "service_provider", "distributor"}


@dataclass
class Candidate:
    name: str
    website: str | None
    domain: str | None
    country: str | None
    kinds: set[str] = field(default_factory=set)
    relevance: float = 0.0
    mentions: int = 0
    why: str = ""
    sources: list[Citation] = field(default_factory=list)
    discarded: str | None = None

    @property
    def score(self) -> float:
        # Relevance first; companies found by several queries are likelier to be real, established suppliers.
        return self.relevance + 0.05 * min(self.mentions - 1, 4)


def consolidate(
    findings: list[tuple[list[FoundCompany], list[Citation]]],
    *,
    limit: int,
    allow_distributors: bool = False,
) -> tuple[list[Candidate], list[Candidate]]:
    """(kept candidates, best first, at most `limit`; discarded ones with the reason)."""
    by_key: dict[str, Candidate] = {}
    for companies, citations in findings:
        for found in companies:
            name = " ".join(found.name.split())[:300]
            if not normalize_name(name):
                continue
            website = normalize_website(found.website) if found.website else None
            domain = registrable_domain(website) if website else None
            key = f"d:{domain}" if domain else f"n:{normalize_name(name)}"
            cand = by_key.get(key)
            if cand is None:
                # A name-only finding may be the same company as one with a website.
                cand = next(
                    (c for c in by_key.values() if normalize_name(c.name) == normalize_name(name)), None
                )
            if cand is None:
                cand = by_key[key] = Candidate(name, website, domain, None)
            elif not cand.website and website:
                by_key.pop(f"n:{normalize_name(cand.name)}", None)
                cand.website, cand.domain = website, domain
                by_key[key] = cand
            cand.mentions += 1
            cand.kinds.add(found.kind)
            if max(0.0, min(1.0, found.relevance)) > cand.relevance:
                cand.relevance, cand.why = max(0.0, min(1.0, found.relevance)), found.why[:300]
            cand.country = cand.country or normalize_country(found.country or "")
            if found.evidence_url and domain and registrable_domain(found.evidence_url) == domain:
                if all(src.url != found.evidence_url for src in cand.sources):
                    cand.sources.insert(0, Citation(found.evidence_url, "Matching product page"))
            for c in citations:
                if (
                    domain
                    and registrable_domain(c.url) == domain
                    and all(s.url != c.url for s in cand.sources)
                ):
                    cand.sources.append(c)

    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    for cand in by_key.values():
        if cand.domain in NOT_VENDOR_DOMAINS:
            cand.discarded = f"{cand.domain} lists suppliers but isn't one"
        elif not cand.kinds & VENDOR_KINDS:
            cand.discarded = "Not a supplier (directory, marketplace or news site)"
        elif not allow_distributors and cand.kinds == {"distributor"}:
            cand.discarded = "Distributor only (the request is for manufacturers and service providers)"
        elif cand.relevance < 0.3:
            cand.discarded = "Low relevance to the request"
        (dropped if cand.discarded else kept).append(cand)
    kept.sort(key=lambda c: c.score, reverse=True)
    for cand in kept[limit:]:
        cand.discarded = f"Beyond the first {limit} candidates"
    return kept[:limit], dropped + kept[limit:]
