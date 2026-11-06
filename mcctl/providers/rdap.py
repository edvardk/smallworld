"""Domain expiry over RDAP (design §3/§6: `dns.zone`), no API key.

The IANA bootstrap file (RFC 9224, https://data.iana.org/rdap/dns.json) maps TLDs to RDAP base URLs; the
domain object (RFC 9083) lists `events`, of which `eventAction: expiration` is the registration's end.
Registries may redirect to a registrar's server; redirects are followed (HTTPS only, no credentials here).
"""

from __future__ import annotations

import datetime

from mcctl import http
from mcctl.providers import ProviderError, parse_date

BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
MAX_REDIRECTS = 3


class RdapError(ProviderError):
    pass


def base_url(domain: str, bootstrap: object) -> str:
    """The RDAP base URL for the domain's longest matching suffix in the bootstrap registry."""
    services = bootstrap.get("services") if isinstance(bootstrap, dict) else None
    best: tuple[int, str] | None = None
    for service in services if isinstance(services, list) else []:
        if not (isinstance(service, list) and len(service) >= 2):
            continue
        tlds, urls = service[0], service[1]
        https = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
        for tld in tlds if isinstance(tlds, list) else []:
            if isinstance(tld, str) and https and (domain == tld or domain.endswith("." + tld)):
                if best is None or len(tld) > best[0]:
                    best = (len(tld), https[0])
    if best is None:
        raise RdapError(f"no RDAP service for {domain} in the IANA bootstrap registry")
    return best[1] if best[1].endswith("/") else best[1] + "/"


def _get(transport: http.Transport, url: str) -> http.Response:
    for _ in range(MAX_REDIRECTS + 1):
        resp = http.get(transport, url, {"Accept": "application/rdap+json"})
        location = resp.header("location")
        if resp.status in (301, 302, 303, 307, 308) and location:
            if not location.startswith("https://"):
                raise RdapError(f"RDAP redirect to a non-HTTPS URL ({http.where(location)})")
            url = location
            continue
        return resp
    raise RdapError("too many RDAP redirects")


def expiration(domain: str, transport: http.Transport) -> datetime.date | None:
    """The registration's expiry date, or None if the registry doesn't publish one."""
    boot = http.get(transport, BOOTSTRAP)
    if boot.status != 200:
        raise RdapError(f"IANA RDAP bootstrap: HTTP {boot.status}")
    resp = _get(transport, base_url(domain, boot.json()) + "domain/" + domain)
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict):
        raise RdapError(f"RDAP lookup of {domain}: HTTP {resp.status}")
    for event in data.get("events") or []:
        if isinstance(event, dict) and event.get("eventAction") == "expiration":
            return parse_date(event.get("eventDate"))
    return None
