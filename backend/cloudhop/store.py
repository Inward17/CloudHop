"""Secret handling, browser-safe row shapes and metric math."""
import base64
import hashlib
import json
import math
import os
import re
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .timeutil import js_round


def sha256(text):
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def _aes(secret):
    return AESGCM(hashlib.sha256(secret.encode()).digest())


def encrypt(plain, secret, bound):
    """`bound` is the row id, passed as AES-GCM additional data: a ciphertext copied into another row will not decrypt."""
    iv = os.urandom(12)
    data = _aes(secret).encrypt(iv, plain.encode(), bound.encode())
    return f'{base64.b64encode(iv).decode()}.{base64.b64encode(data).decode()}'


def decrypt(stored, secret, bound):
    iv, data = (base64.b64decode(part) for part in stored.split('.'))
    return _aes(secret).decrypt(iv, data, bound.encode()).decode()


def decrypt_with_env(env, stored, bound):
    """Key rotation: the new key is ENCRYPTION_KEY and the old one ENCRYPTION_KEY_PREVIOUS.

    New secrets use the new key; old ones still decrypt until they are saved again.
    """
    keys = [key for key in (env.get('ENCRYPTION_KEY'), env.get('ENCRYPTION_KEY_PREVIOUS')) if key]
    if not keys:
        raise RuntimeError('No encryption key configured')
    for index, key in enumerate(keys):
        try:
            return decrypt(stored, key, bound)
        except Exception:
            if index == len(keys) - 1:
                raise
    raise AssertionError('unreachable')


def random_token():
    return re.sub(r'[^A-Za-z0-9]', '', base64.b64encode(secrets.token_bytes(32)).decode())


def public_project(row):
    """Never includes report_hash, so rows are safe to send to the browser."""
    return {
        'id': row['id'], 'provider': row.get('provider') or 'github', 'repo': row['repo'], 'accountId': row['account_id'], 'clouds': json.loads(row['clouds']),
        'active': row['active'], 'previous': row['previous'], 'url': row['url'], 'prUrl': row['pr_url'], 'portability': row['portability'], 'createdAt': row['created_at'],
    }


def public_user(row):
    return {
        'id': row['id'], 'login': row['login'], 'name': row['name'], 'email': row['email'], 'avatarUrl': row['avatar_url'],
        'viaGithub': row['github_id'] is not None, 'createdAt': row['created_at'], 'lastLoginAt': row['last_login_at'],
    }


def public_account(row):
    """Never includes secret_enc."""
    return {'id': row['id'], 'kind': row['kind'], 'label': row['label'], 'data': json.loads(row['data']), 'createdAt': row['created_at']}


def uptime_summary(checks):
    up = sum(1 for check in checks if check['ok'])
    ok_ms = [check['ms'] for check in checks if check['ok'] and check['ms'] is not None]
    return {
        'checks': len(checks),
        'uptimePercent': (up / len(checks)) * 100 if checks else None,
        'averageMs': js_round(sum(ok_ms) / len(ok_ms)) if ok_ms else None,
        'last': checks[-1] if checks else None,
    }


def summarize_checks(checks, since, now, bucket_count):
    """Buckets raw uptime checks for charts, and finds outages as runs of consecutive failed checks."""
    size = (now - since) / bucket_count
    buckets = [{'start': js_round(since + index * size), 'checks': 0, 'up': 0, 'msTotal': 0, 'msCount': 0, 'maxMs': None} for index in range(bucket_count)]
    statuses = {}
    incidents = []
    open_incident = None
    for check in checks:
        index = min(bucket_count - 1, math.floor((check['at'] - since) / size))
        bucket = buckets[index] if index >= 0 else None
        if bucket:
            bucket['checks'] += 1
            if check['ok']:
                bucket['up'] += 1
            if check['ok'] and check['ms'] is not None:
                bucket['msTotal'] += check['ms']
                bucket['msCount'] += 1
                bucket['maxMs'] = max(bucket['maxMs'] or 0, check['ms'])
        key = 'No response' if check['status'] is None else str(check['status'])
        statuses[key] = statuses.get(key, 0) + 1
        if not check['ok']:
            if open_incident is None:
                open_incident = {'start': check['at'], 'end': check['at'], 'failedChecks': 0}
            open_incident['end'] = check['at']
            open_incident['failedChecks'] += 1
        elif open_incident:
            incidents.append({**open_incident, 'end': check['at']})
            open_incident = None
    if open_incident:
        incidents.append({**open_incident, 'ongoing': True})
    ok_ms = sorted(check['ms'] for check in checks if check['ok'] and check['ms'] is not None)
    return {
        **uptime_summary(checks),
        'p95Ms': ok_ms[min(len(ok_ms) - 1, math.ceil(len(ok_ms) * 0.95) - 1)] if ok_ms else None,
        'buckets': [{
            'start': bucket['start'],
            'checks': bucket['checks'],
            'uptimePercent': (bucket['up'] / bucket['checks']) * 100 if bucket['checks'] else None,
            'averageMs': js_round(bucket['msTotal'] / bucket['msCount']) if bucket['msCount'] else None,
            'maxMs': bucket['maxMs'],
        } for bucket in buckets],
        # Ties keep the original order: status codes ascending, then "No response".
        'statuses': sorted(({'status': status, 'count': count} for status, count in sorted(statuses.items(), key=lambda item: (not item[0].isdigit(), int(item[0]) if item[0].isdigit() else 0))),
                           key=lambda item: -item['count']),
        'incidents': incidents[::-1],
    }


def next_state(project, report):
    """A report moves the "active cloud" pointer only when traffic actually moved."""
    event, cloud = report.get('event'), report.get('cloud')
    if event == 'deployed':
        return {'active': cloud, 'previous': project['previous'], 'url': report.get('url') or project['url']}
    if event in ('switched', 'rolled_back'):
        return {'active': cloud, 'previous': report.get('from') or project['active'], 'url': report.get('url') or project['url']}
    if event == 'destroyed':
        if cloud == project['active']:
            return {'active': None, 'previous': None if project['previous'] == cloud else project['previous'], 'url': None}
        if cloud == project['previous']:
            return {'active': project['active'], 'previous': None, 'url': project['url']}
    return {'active': project['active'], 'previous': project['previous'], 'url': project['url']}
