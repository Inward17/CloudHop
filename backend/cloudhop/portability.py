"""Answers "how hard is it to move this app to another cloud?" from the service map."""

DATASTORE_TIERS = {
    'postgres': ('auto', 'Managed on every major cloud; logical replication works'),
    'mysql': ('auto', 'Managed on every major cloud; replication works'),
    'redis': ('auto', 'Managed on every major cloud; treat contents as cache unless persistence is confirmed'),
    'objects': ('auto', 'Files sync between S3-compatible stores'),
    'mongodb': ('code', 'No same-API managed service on every cloud; use Atlas or run as container'),
    'rabbitmq': ('code', 'Not managed on every cloud; run as container or use hosted broker'),
    'kafka': ('code', 'Managed offerings differ per cloud; needs review'),
    'elasticsearch': ('code', 'Managed offerings differ per cloud; needs reindex'),
    'sqlite': ('redesign', 'File on local disk does not survive container restarts; move to Postgres'),
}

VERDICTS = {
    'auto': 'Portable by design',
    'code': 'Portable after code changes',
    'redesign': 'Needs redesign before switching',
    'blocked': 'Has parts that cannot move as is',
}
SEVERITY = ['auto', 'code', 'redesign', 'blocked']


def portability_report(service_map):
    items = []
    for service in service_map['services']:
        name = f"{service['path']} ({service['runtime']})"
        if service['runtime'] in ('node', 'python') or service['hasDockerfile']:
            items.append({'name': name, 'tier': 'auto', 'why': 'Runs as a container, so it runs on any cloud', 'evidence': (service['evidence'] or [None])[0]})
        else:
            items.append({'name': name, 'tier': 'code', 'why': 'Add a Dockerfile so it can run anywhere', 'evidence': None})
    for store in service_map['datastores']:
        tier, why = DATASTORE_TIERS[store['kind']]
        items.append({'name': store['kind'], 'tier': tier, 'why': why, 'evidence': store['evidence'][0]})
    for binding in service_map['bindings']:
        items.append({'name': binding['service'], 'tier': binding['tier'], 'why': binding['why'], 'evidence': binding['evidence']})
    items.sort(key=lambda item: -SEVERITY.index(item['tier']))
    worst = items[0]['tier'] if items else 'auto'
    return {'verdict': VERDICTS[worst], 'worst': worst, 'items': items}
