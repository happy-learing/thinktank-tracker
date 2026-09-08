from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class KnownSource:
    institution_key: str
    homepage_url: str
    adapter_key: str
    ordered_by_date: bool = True


KNOWN_SOURCES: dict[str, KnownSource] = {
    "iss.europa.eu": KnownSource(
        "euiss", "https://www.iss.europa.eu/", "euiss"
    ),
    "ceps.eu": KnownSource(
        "ceps", "https://www.ceps.eu/", "ceps"
    ),
    "martenscentre.eu": KnownSource(
        "martens", "https://www.martenscentre.eu/", "martens"
    ),
    "egmontinstitute.be": KnownSource(
        "egmont", "https://egmontinstitute.be/", "egmont"
    ),
    "epc.eu": KnownSource(
        "epc", "https://www.epc.eu/", "epc"
    ),
    "realinstitutoelcano.org": KnownSource(
        "elcano", "https://www.realinstitutoelcano.org/", "elcano"
    ),
    "ecfr.eu": KnownSource(
        "ecfr", "https://ecfr.eu/", "ecfr"
    ),
}


def normalized_hostname(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname.removeprefix("www.")


def known_source_for(url: str) -> KnownSource | None:
    hostname = normalized_hostname(url)
    direct = KNOWN_SOURCES.get(hostname)
    if direct:
        return direct
    for domain, source in KNOWN_SOURCES.items():
        if hostname.endswith(f".{domain}"):
            return source
    return None
