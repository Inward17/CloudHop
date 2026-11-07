import json
import os

import httpx
import pytest

# Settings for every test, before the app module reads them.
os.environ.update({
    'ENCRYPTION_KEY': 'test-encryption-key',
    'PUBLIC_URL': 'http://localhost:5173',
    'ADMIN_TOKEN': 'test-admin-token',
    'DATABASE_PATH': ':memory:',
    'CLOUDHOP_SCHEDULER': 'off',
})
for name in ('GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET', 'GITHUB_APP_ID', 'GITHUB_APP_SLUG', 'GITHUB_APP_PRIVATE_KEY', 'ALLOWED_GITHUB_USERS', 'ENCRYPTION_KEY_PREVIOUS'):
    os.environ.pop(name, None)

from cloudhop import net  # noqa: E402


class FakeHosts:
    """Answers outside HTTP calls by matching "METHOD url-fragment" keys, first match wins; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def handler(self, request):
        body = request.content.decode('utf-8', 'replace') if request.content else None
        self.calls.append({'method': request.method, 'url': str(request.url), 'body': body, 'auth': request.headers.get('authorization'), 'headers': request.headers})
        for key, value in self.routes.items():
            method, fragment = key.split(' ', 1)
            if method == request.method and fragment in str(request.url):
                value = value(request) if callable(value) else value
                if isinstance(value, httpx.Response):
                    return value
                if isinstance(value, str):
                    return httpx.Response(200, text=value)
                return httpx.Response(200, json=value)
        return httpx.Response(404, json={'message': f'no route for {request.method} {request.url}'})

    def json(self, match):
        """Parsed JSON body of the first call whose URL contains `match`."""
        return json.loads(next(call for call in self.calls if match(call))['body'])


@pytest.fixture
def fake_hosts():
    def install(routes):
        hosts = FakeHosts(routes)
        net.use(httpx.MockTransport(hosts.handler))
        return hosts
    return install
