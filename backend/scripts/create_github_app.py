"""Creates Cloudhop's GitHub App in one click, with GitHub's App Manifest flow, and saves its settings.

Run from backend/:  python scripts/create_github_app.py [--org ORGANIZATION]
Then open the printed address. It sends GitHub a ready-made app description; you review it and click
"Create GitHub App" on github.com. GitHub hands back a one-time code, which this script exchanges for the
app's id, keys and secrets, and writes them to backend/.env (gitignored). Restart the backend afterwards.

Only listens on 127.0.0.1, stops after one use, and checks a random state value against forged callbacks.
"""
import argparse
import html
import json
import re
import secrets
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

ENV_FILE = Path(__file__).resolve().parent.parent / '.env'
PORT = 8799


def read_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            key, sep, value = line.partition('=')
            if sep and key.strip() and not key.startswith('#'):
                values[key.strip()] = value.strip().strip('"')
    return values


def write_env(updates):
    """Replaces or appends keys in backend/.env, leaving every other line as it was."""
    lines = ENV_FILE.read_text(encoding='utf-8').splitlines() if ENV_FILE.exists() else []
    pending = dict(updates)
    out = []
    for line in lines:
        key = line.partition('=')[0].strip()
        if key in pending:
            out.append(f'{key}={pending.pop(key)}')
        else:
            out.append(line)
    out += [f'{key}={value}' for key, value in pending.items()]
    ENV_FILE.write_text('\n'.join(out) + '\n', encoding='utf-8')


def manifest(public_url):
    return {
        'name': f'Cloudhop local {secrets.token_hex(3)}',  # App names are unique across GitHub.
        'url': public_url,
        'description': 'Deploys repos you pick to your own cloud, with approval for every action. Local testing install.',
        'redirect_url': f'http://127.0.0.1:{PORT}/done',
        'callback_urls': [f'{public_url}/api/auth/callback'],
        'setup_url': f'{public_url}/api/github/setup',
        'setup_on_update': True,
        'request_oauth_on_install': False,
        'public': False,
        # Webhooks are not used; GitHub still requires an address.
        'hook_attributes': {'url': 'https://example.com/cloudhop-webhooks-unused', 'active': False},
        'default_permissions': {'contents': 'write', 'pull_requests': 'write', 'actions': 'write', 'workflows': 'write', 'metadata': 'read'},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--org', help='create the app under this GitHub organization instead of your personal account')
    args = parser.parse_args()
    if args.org and not re.fullmatch(r'[A-Za-z0-9-]{1,39}', args.org):
        parser.error('Organization names use letters, digits and hyphens')
    public_url = read_env().get('PUBLIC_URL', 'http://localhost:5173').rstrip('/')
    state = secrets.token_urlsafe(24)
    target = f'https://github.com/organizations/{quote(args.org)}/settings/apps/new' if args.org else 'https://github.com/settings/apps/new'
    done = {}

    class Handler(BaseHTTPRequestHandler):
        def page(self, status, body):
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'<!doctype html><meta charset="utf-8"><title>Cloudhop GitHub App</title><body style="font-family:system-ui;max-width:640px;margin:48px auto;line-height:1.5">{body}'.encode())

        def do_GET(self):  # noqa: N802
            url = urlsplit(self.path)
            if url.path == '/':
                self.page(200, f'''<h1>Create Cloudhop's GitHub App</h1>
<p>This sends GitHub a ready-made app description. On GitHub, check it and click <b>Create GitHub App</b>.</p>
<form method="post" action="{html.escape(target)}?state={html.escape(state)}">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest(public_url)))}">
<button style="font-size:16px;padding:10px 18px">Continue to GitHub</button></form>''')
                return
            if url.path != '/done':
                self.page(404, 'Not found')
                return
            query = parse_qs(url.query)
            if query.get('state', [''])[0] != state or not query.get('code'):
                self.page(400, '<h1>That link is not from this setup.</h1><p>Start again from the address the script printed.</p>')
                return
            request = urllib.request.Request(f"https://api.github.com/app-manifests/{quote(query['code'][0])}/conversions", method='POST',
                                             headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'cloudhop'})
            with urllib.request.urlopen(request, timeout=30) as response:
                app = json.load(response)
            write_env({
                'GITHUB_APP_ID': str(app['id']),
                'GITHUB_APP_SLUG': app['slug'],
                'GITHUB_CLIENT_ID': app['client_id'],
                'GITHUB_CLIENT_SECRET': app['client_secret'],
                # One line in .env; the backend turns \n back into line breaks.
                'GITHUB_APP_PRIVATE_KEY': '"' + app['pem'].strip().replace('\n', '\\n') + '"',
            })
            done['app'] = app
            self.page(200, f'''<h1>Done: {html.escape(app['name'])} is created.</h1>
<p>Its settings are saved in backend/.env. Restart the backend, then use <b>Continue with GitHub</b> to sign in and
<b>Install on GitHub</b> on the Accounts page to pick repos.</p><p>You can close this tab.</p>''')

        def log_message(self, *_):
            pass

    server = HTTPServer(('127.0.0.1', PORT), Handler)
    start = f'http://127.0.0.1:{PORT}/'
    print(f'Open {start} to create the GitHub App (callback {public_url}/api/auth/callback).', flush=True)
    webbrowser.open(start)
    while 'app' not in done:
        server.handle_request()
    app = done['app']
    print(f"Created {app['name']} (id {app['id']}, {app.get('html_url', '')}). Saved to {ENV_FILE}. Restart the backend.", flush=True)


if __name__ == '__main__':
    main()
