from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）)\]}]+", re.IGNORECASE)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def extract_urls(text: str) -> list[str]:
    return [match.rstrip(".,;:") for match in URL_RE.findall(text or "")]


def canonicalize_url(url: str, *, preserve_trailing_slash: bool = False) -> str:
    split = urlsplit(url.strip())
    scheme = split.scheme.lower() or "https"
    hostname = (split.hostname or "").lower()
    port = split.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", split.path or "/")
    if path != "/" and not preserve_trailing_slash:
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query_pairs)), ""))
