"""Pure unit tests: name/domain/country normalisation and taxonomy seed integrity."""

import pytest

from app.domain.normalization import normalize_country, normalize_name, normalize_website, registrable_domain
from app.modules.taxonomy.seed import flatten
from app.modules.taxonomy.seed_data import CERTIFICATIONS, EQUIPMENT, INDUSTRIES, SERVICES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Flowserve Corporation", "flowserve"),
        ("Flowserve Corp.", "flowserve"),
        ("FLOWSERVE", "flowserve"),
        ("Flowserve Pompes S.A.S.", "flowserve pompes"),
        ("Sulzer Pumps Ltd", "sulzer pumps"),
        ("KSB SE & Co. KGaA", "ksb"),
        ("Smith & Co", "smith"),
        ("Oil and Gas Pumps Ltd", "oil and gas pumps"),
        ("Jord Oil & Gas Systems B.V.", "jord oil and gas systems"),
        ("Société Générale de Pompes SA", "societe generale de pompes"),
        ("Group", "group"),  # a lone legal word is kept rather than producing an empty key
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.flowserve.com/en/pumps", "flowserve.com"),
        ("flowserve.com", "flowserve.com"),
        ("WWW.Sulzer.COM", "sulzer.com"),
        ("http://pumps.ksb.co.uk", "ksb.co.uk"),
        ("not a url", None),
        ("", None),
        (None, None),
        ("localhost", None),
    ],
)
def test_registrable_domain(raw: str | None, expected: str | None) -> None:
    assert registrable_domain(raw) == expected


def test_normalize_website() -> None:
    assert normalize_website("flowserve.com/") == "https://flowserve.com"
    assert normalize_website("http://WWW.Sulzer.com/en/") == "https://www.sulzer.com/en"
    assert normalize_website("nonsense") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USA", "US"),
        ("United Kingdom", "GB"),
        ("uk", "GB"),
        ("de", "DE"),
        ("Norway", "NO"),
        ("Atlantis", None),
    ],
)
def test_normalize_country(raw: str, expected: str | None) -> None:
    assert normalize_country(raw) == expected


@pytest.mark.parametrize("tree", [EQUIPMENT, SERVICES])
def test_taxonomy_codes_unique_and_parents_first(tree: tuple[object, ...]) -> None:
    seen: set[str] = set()
    for code, parent, _order, node in flatten(tree):  # type: ignore[arg-type]
        assert code not in seen, f"duplicate code {code}"
        assert parent is None or parent in seen, f"{code} listed before its parent"
        assert code.replace("_", "").replace(".", "").isalnum() and code == code.lower()
        assert node.name.strip()
        seen.add(code)


def test_seed_lookup_lists_are_unique() -> None:
    assert len({c[0] for c in CERTIFICATIONS}) == len(CERTIFICATIONS)
    assert len({i[0] for i in INDUSTRIES}) == len(INDUSTRIES)
