"""One shared HTTP client for every outside call. Tests swap it for one with a fake transport."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

client = httpx.AsyncClient(timeout=30, headers={'User-Agent': 'cloudhop'})
# Fake transports in tests answer made-up hosts that do not resolve, so the address check is off there.
check_addresses = True


def use(transport):
    """Tests: route all outside calls through `transport` (an httpx.MockTransport)."""
    global client, check_addresses
    client = httpx.AsyncClient(timeout=30, headers={'User-Agent': 'cloudhop'}, transport=transport)
    check_addresses = False


class UnsafeAddress(Exception):
    """The address is not a public internet host."""


async def check_public(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise UnsafeAddress(url)
    if not check_addresses:
        return
    host = parts.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            found = await asyncio.get_running_loop().getaddrinfo(host, parts.port or None, type=socket.SOCK_STREAM)
        except OSError as error:
            raise UnsafeAddress(url) from error
        addresses = [ipaddress.ip_address(info[4][0].split('%')[0]) for info in found]
    if not addresses or not all(address.is_global for address in addresses):
        raise UnsafeAddress(url)


async def public_get(url, timeout=15, max_redirects=5):
    """GET for addresses that users or pipelines supply (site URLs). Refuses private, loopback and cloud metadata
    addresses, on the first request and on every redirect.

    ponytail: checks the name, then httpx resolves it again, so a DNS answer that changes in between (rebinding)
    is not caught. Pin the checked IP in the connection if Cloudhop ever runs next to sensitive internal services.
    """
    for _ in range(max_redirects + 1):
        await check_public(url)
        response = await client.get(url, follow_redirects=False, timeout=timeout)
        location = response.headers.get('location')
        if not (response.is_redirect and location):
            return response
        url = urljoin(url, location)
    raise UnsafeAddress('Too many redirects')
