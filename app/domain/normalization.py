"""Vendor name, website and country normalisation, used for duplicate detection and matching.

Pure functions, no I/O: tldextract runs with its bundled public-suffix snapshot (never fetches at runtime).
"""

import re
import unicodedata
from urllib.parse import urlsplit

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())

# Legal-form and filler words that don't distinguish companies ("Flowserve Corp." == "Flowserve").
LEGAL_SUFFIXES = frozenset(
    """
    inc incorporated corp corporation co company ltd limited llc llp lp plc pvt private pte pty
    gmbh mbh ag kg kgaa ug se sa sas sarl srl spa sl sociedad bv nv vof oy ab as asa aps a/s kk
    group holding holdings international intl industries enterprises
    """.split()
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """'Flowserve Pompes S.A.S.' -> 'flowserve pompes'. Stable key for exact and fuzzy matching."""
    text = strip_accents(name).lower().replace("&", " and ")
    # Collapse dotted abbreviations first so "S.A.S." becomes "sas" and is recognised as a legal suffix.
    text = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group(0).replace(".", ""), text)
    tokens = [t for t in _NON_ALNUM.split(text) if t]
    # Strip trailing legal forms, including an "and" that only joined them ("KSB SE & Co. KGaA" -> "ksb").
    while len(tokens) > 1 and (tokens[-1] in LEGAL_SUFFIXES or tokens[-1] == "and"):
        tokens.pop()
    return " ".join(tokens)


def registrable_domain(url_or_host: str | None) -> str | None:
    """'https://www.Flowserve.co.uk/pumps' -> 'flowserve.co.uk'. None if it isn't a usable public domain."""
    if not url_or_host:
        return None
    raw = url_or_host.strip()
    if not raw:
        return None
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
    if not host:
        return None
    domain = _extract(host).top_domain_under_public_suffix
    return domain.lower() if domain else None


def normalize_website(url: str | None) -> str | None:
    """Canonical https URL for display ('flowserve.com/' -> 'https://flowserve.com')."""
    if not url or not url.strip():
        return None
    raw = url.strip()
    parts = urlsplit(raw if "//" in raw else f"https://{raw}")
    if not parts.hostname or registrable_domain(parts.hostname) is None:
        return None
    path = parts.path.rstrip("/")
    return f"https://{parts.hostname.lower()}{path}"


# Common names and codes seen in vendor lists -> ISO 3166-1 alpha-2.
_COUNTRY_ALIASES = {
    "usa": "US",
    "us": "US",
    "united states": "US",
    "united states of america": "US",
    "america": "US",
    "uk": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "britain": "GB",
    "uae": "AE",
    "united arab emirates": "AE",
    "ksa": "SA",
    "saudi arabia": "SA",
    "kingdom of saudi arabia": "SA",
    "south korea": "KR",
    "korea": "KR",
    "republic of korea": "KR",
    "china": "CN",
    "prc": "CN",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "netherlands": "NL",
    "holland": "NL",
    "norway": "NO",
    "sweden": "SE",
    "denmark": "DK",
    "finland": "FI",
    "switzerland": "CH",
    "austria": "AT",
    "belgium": "BE",
    "poland": "PL",
    "japan": "JP",
    "india": "IN",
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "brazil": "BR",
    "mexico": "MX",
    "canada": "CA",
    "australia": "AU",
    "qatar": "QA",
    "oman": "OM",
    "kuwait": "KW",
    "bahrain": "BH",
    "egypt": "EG",
    "nigeria": "NG",
    "south africa": "ZA",
    "turkey": "TR",
    "turkiye": "TR",
    "russia": "RU",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "ireland": "IE",
    "portugal": "PT",
    "czech republic": "CZ",
    "czechia": "CZ",
    "israel": "IL",
    "thailand": "TH",
    "vietnam": "VN",
    "philippines": "PH",
    "new zealand": "NZ",
}
_ISO2 = re.compile(r"^[A-Za-z]{2}$")


def normalize_country(value: str | None) -> str | None:
    """'United Kingdom' / 'uk' / 'GB' -> 'GB'. None when unrecognised (never guesses)."""
    if not value or not value.strip():
        return None
    key = strip_accents(value).strip().lower().rstrip(".")
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    if _ISO2.match(key):
        return key.upper()
    return None


def country_names(code: str) -> set[str]:
    """Lower-case ways a country is written ("NO" -> {"no", "norway"}), for matching free-text region lists."""
    code = code.upper()
    return {code.lower()} | {name for name, c in _COUNTRY_ALIASES.items() if c == code}
