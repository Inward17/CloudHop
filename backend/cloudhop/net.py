"""One shared HTTP client for every outside call. Tests swap it for one with a fake transport."""
import httpx

client = httpx.AsyncClient(timeout=30, headers={'User-Agent': 'cloudhop'})


def use(transport):
    """Tests: route all outside calls through `transport` (an httpx.MockTransport)."""
    global client
    client = httpx.AsyncClient(timeout=30, headers={'User-Agent': 'cloudhop'}, transport=transport)
