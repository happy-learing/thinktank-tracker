from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class KnownSource:
    publication_url: str
    adapter_key: str
    ordered_by_date: bool = True


KNOWN_SOURCES: dict[str, KnownSource] = {
    "iss.europa.eu": KnownSource(
        "https://www.iss.europa.eu/publications", "euiss"
    ),
    "ceps.eu": KnownSource(
        "https://www.ceps.eu/ceps-publications/", "ceps"
    ),
    "martenscentre.eu": KnownSource(
        "https://www.martenscentre.eu/publication/", "martens"
    ),
    "egmontinstitute.be": KnownSource(
        "https://egmontinstitute.be/publications/", "egmont"
    ),
    "epc.eu": KnownSource(
        "https://www.epc.eu/publications/", "epc"
    ),
    "realinstitutoelcano.org": KnownSource(
        "https://www.realinstitutoelcano.org/en/publications/", "elcano"
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
