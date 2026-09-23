"""Validation for URLs whose contents may be fetched by the Runtime."""

from __future__ import annotations

import ipaddress

import httpx


PUBLIC_DNS_ENDPOINT = "https://cloudflare-dns.com/dns-query"
PUBLIC_DNS_TIMEOUT_SECONDS = 5.0
_NON_PUBLIC_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # Shared address space is not publicly routable.
        "192.0.0.0/24",
        "198.18.0.0/15",
        "240.0.0.0/4",
    )
)


def require_public_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in allowed_schemes:
        schemes = ", ".join(sorted(allowed_schemes))
        raise ValueError(f"URL scheme must be one of: {schemes}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain credentials")
    try:
        host = parsed.hostname
        _ = parsed.port  # Validate an explicitly supplied port.
    except ValueError as error:
        raise ValueError(f"invalid URL: {url!r}") from error
    if not host:
        raise ValueError("URL must contain a host")

    for address in _resolve_public_addresses(host):
        ip = ipaddress.ip_address(address)
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global or any(
            ip in network for network in _NON_PUBLIC_NETWORKS
        ):
            raise ValueError(f"URL host '{host}' resolves to a non-public address")


def _resolve_public_addresses(host: str) -> tuple[str, ...]:
    """Resolve through public DNS instead of the host's intercepted resolver."""
    # A proxy may point names at reserved fake-IP ranges; literals keep the blocklist.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return (str(literal),)

    addresses: list[str] = []
    try:
        with httpx.Client(
            timeout=httpx.Timeout(PUBLIC_DNS_TIMEOUT_SECONDS),
        ) as client:
            for record_type in ("A", "AAAA"):
                response = client.get(
                    PUBLIC_DNS_ENDPOINT,
                    params={"name": host, "type": record_type},
                    headers={"accept": "application/dns-json"},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("Status") != 0:
                    continue
                answers = payload.get("Answer", [])
                if not isinstance(answers, list):
                    continue
                for answer in answers:
                    if not isinstance(answer, dict):
                        continue
                    value = answer.get("data")
                    if not isinstance(value, str):
                        continue
                    try:
                        address = ipaddress.ip_address(value)
                    except ValueError:
                        continue
                    if answer.get("type") == 1 and address.version == 4:
                        addresses.append(str(address))
                    elif answer.get("type") == 28 and address.version == 6:
                        addresses.append(str(address))
    except (httpx.HTTPError, ValueError) as error:
        raise ValueError(f"could not resolve URL host '{host}'") from error

    if not addresses:
        raise ValueError(f"could not resolve URL host '{host}'")
    return tuple(dict.fromkeys(addresses))


__all__ = ["require_public_url"]
