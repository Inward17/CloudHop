"""Turns manifest files into a provider-neutral service map. Pure: no network.

Repo content is untrusted, so it is only ever matched against fixed tables.
"""
import json
import re


def _code(service, why):
    return {'binding': {'service': service, 'tier': 'code', 'why': why}}


def _redesign(service, why):
    return {'binding': {'service': service, 'tier': 'redesign', 'why': why}}


def _blocked(service, why):
    return {'binding': {'service': service, 'tier': 'blocked', 'why': why}}


def _web(framework):
    return {'framework': framework, 'kind': 'web'}


def _static(framework):
    return {'framework': framework, 'kind': 'static'}


NODE_DEPS = {
    'next': _web('Next.js'), 'nuxt': _web('Nuxt'), 'express': _web('Express'), 'fastify': _web('Fastify'),
    '@nestjs/core': _web('NestJS'), 'hono': _web('Hono'), 'koa': _web('Koa'), '@remix-run/node': _web('Remix'),
    'vite': _static('Vite'), 'astro': _static('Astro'), 'react-scripts': _static('Create React App'),
    'pg': {'datastore': 'postgres'}, 'postgres': {'datastore': 'postgres'}, 'mysql': {'datastore': 'mysql'},
    'mysql2': {'datastore': 'mysql'}, 'mongoose': {'datastore': 'mongodb'}, 'mongodb': {'datastore': 'mongodb'},
    'redis': {'datastore': 'redis'}, 'ioredis': {'datastore': 'redis'},
    'better-sqlite3': {'datastore': 'sqlite'}, 'sqlite3': {'datastore': 'sqlite'},
    'bullmq': {'datastore': 'redis', 'worker': True}, 'bull': {'datastore': 'redis', 'worker': True},
    'aws-sdk': _code('AWS SDK v2', 'Calls need review for AWS-only services'),
    '@aws-sdk/client-s3': _code('S3 SDK', 'Swap client or use S3-compatible endpoint'),
    '@aws-sdk/client-sqs': _code('SQS', 'Replace with target queue service'),
    '@aws-sdk/client-sns': _code('SNS', 'Replace with target pub/sub service'),
    '@aws-sdk/client-ses': _code('SES', 'Change email provider; sender reputation does not move'),
    '@aws-sdk/client-bedrock-runtime': _code('Bedrock', 'Swap model client'),
    '@aws-sdk/client-secrets-manager': _code('Secrets Manager', 'Read secrets from environment instead'),
    '@aws-sdk/client-dynamodb': _redesign('DynamoDB', 'No equal API elsewhere; data model must change'),
    '@aws-sdk/client-cognito-identity-provider': _redesign('Cognito', 'User pools have no direct equal elsewhere'),
    '@aws-sdk/client-sfn': _redesign('Step Functions', 'Workflows must be rebuilt'),
    '@aws-sdk/client-kms': _blocked('KMS', 'Keys cannot be exported; re-encrypt data'),
    '@google-cloud/storage': _code('Cloud Storage SDK', 'Swap client'),
    '@google-cloud/pubsub': _code('Pub/Sub', 'Replace with target queue service'),
    '@google-cloud/vertexai': _code('Vertex AI', 'Swap model client'),
    '@google-cloud/firestore': _redesign('Firestore', 'No equal API elsewhere; data model must change'),
    'firebase-admin': _redesign('Firebase', 'Auth and Firestore have no direct equal elsewhere'),
    '@google-cloud/bigquery': _redesign('BigQuery', 'Queries and pipelines must be rebuilt'),
    '@azure/storage-blob': _code('Blob Storage SDK', 'Swap client'),
    '@azure/service-bus': _code('Service Bus', 'Replace with target queue service'),
    '@azure/cosmos': _redesign('Cosmos DB', 'No equal API elsewhere; data model must change'),
}

PYTHON_DEPS = {
    'django': _web('Django'), 'flask': _web('Flask'), 'fastapi': _web('FastAPI'),
    'psycopg': {'datastore': 'postgres'}, 'psycopg2': {'datastore': 'postgres'},
    'psycopg2-binary': {'datastore': 'postgres'}, 'asyncpg': {'datastore': 'postgres'},
    'pymysql': {'datastore': 'mysql'}, 'mysqlclient': {'datastore': 'mysql'}, 'aiomysql': {'datastore': 'mysql'},
    'pymongo': {'datastore': 'mongodb'}, 'motor': {'datastore': 'mongodb'},
    'redis': {'datastore': 'redis'}, 'rq': {'datastore': 'redis', 'worker': True}, 'celery': {'worker': True},
    'boto3': _code('AWS SDK', 'Calls need review for AWS-only services'),
    'google-cloud-storage': _code('Cloud Storage SDK', 'Swap client'),
    'google-cloud-pubsub': _code('Pub/Sub', 'Replace with target queue service'),
    'google-cloud-firestore': _redesign('Firestore', 'No equal API elsewhere; data model must change'),
    'google-cloud-bigquery': _redesign('BigQuery', 'Queries and pipelines must be rebuilt'),
    'firebase-admin': _redesign('Firebase', 'Auth and Firestore have no direct equal elsewhere'),
    'azure-storage-blob': _code('Blob Storage SDK', 'Swap client'),
    'azure-cosmos': _redesign('Cosmos DB', 'No equal API elsewhere; data model must change'),
}

COMPOSE_IMAGES = {
    'postgres': 'postgres', 'postgis': 'postgres', 'mysql': 'mysql', 'mariadb': 'mysql', 'redis': 'redis', 'valkey': 'redis',
    'mongo': 'mongodb', 'minio': 'objects', 'rabbitmq': 'rabbitmq', 'elasticsearch': 'elasticsearch', 'kafka': 'kafka',
}

OTHER_RUNTIMES = {
    'pom.xml': 'java', 'build.gradle': 'java', 'build.gradle.kts': 'java', 'go.mod': 'go',
    'Gemfile': 'ruby', 'Cargo.toml': 'rust', 'composer.json': 'php',
}
ENV_FILES = {'.env.example', '.env.sample', '.env.template'}  # Never a real .env: it holds secrets.
COMPOSE_FILES = {'docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'}

MANIFESTS = {'package.json', 'requirements.txt', 'pyproject.toml', 'Dockerfile', 'Procfile', *ENV_FILES, *COMPOSE_FILES, *OTHER_RUNTIMES}

SKIP = re.compile(r'(^|/)(node_modules|vendor|dist|build|examples?|samples?|tests?|__tests__|fixtures|docs|\.github|\.cloudhop)/')
MAX_FILES = 40  # Files read per analysis, so one repo cannot make Cloudhop download thousands of files.
MAX_BYTES = 200_000
SCHEMES = {'postgres://': 'postgres', 'postgresql://': 'postgres', 'mysql://': 'mysql', 'mongodb://': 'mongodb', 'redis://': 'redis'}


def _base_name(path):
    return path[path.rfind('/') + 1:]


def _dir_name(path):
    return path[:path.rfind('/')] if '/' in path else '.'


def select_manifests(entries):
    """Picks the manifest files worth reading, shallowest first, within the file budget."""
    wanted = [entry for entry in entries
              if (entry.get('size') or 0) <= MAX_BYTES and not SKIP.search(entry['path']) and _base_name(entry['path']) in MANIFESTS]
    wanted.sort(key=lambda entry: len(entry['path'].split('/')))
    return {'selected': wanted[:MAX_FILES], 'total': len(wanted)}


def _table(obj, key):
    """A package.json section, or {} when it is missing or not an object (repo content is untrusted)."""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _line_of(text, needle):
    for index, line in enumerate(text.split('\n')):
        if needle in line:
            return index + 1
    return None


def analyze_files(files):
    """Every finding keeps file and line so the user can check why it was detected."""
    services = {}
    datastores = {}
    bindings = {}
    workers = []
    env_vars = {}
    unknowns = []

    def service_at(directory):
        if directory not in services:
            services[directory] = {'path': directory, 'runtime': None, 'framework': None, 'kind': None, 'port': None, 'hasDockerfile': False, 'hasStart': False, 'evidence': []}
        return services[directory]

    def add_datastore(kind, evidence):
        datastores.setdefault(kind, {'kind': kind, 'evidence': []})['evidence'].append(evidence)

    def apply_dep(signal, dep, file, text, service, production):
        evidence = {'file': file, 'line': _line_of(text, dep), 'detail': dep}
        if signal.get('framework') and (not service['kind'] or signal['kind'] == 'web'):
            service['framework'] = signal['framework']
            service['kind'] = signal['kind']
            service['evidence'].append(evidence)
        if not production:
            return
        if signal.get('datastore'):
            add_datastore(signal['datastore'], evidence)
        if signal.get('worker'):
            workers.append({'path': service['path'], 'evidence': evidence})
        binding = signal.get('binding')
        if binding and binding['service'] not in bindings:
            bindings[binding['service']] = {**binding, 'evidence': evidence}

    for file in files:
        path, text = file['path'], file['text']
        name = _base_name(path)
        directory = _dir_name(path)

        if name == 'package.json':
            try:
                pkg = json.loads(text)
            except ValueError:
                unknowns.append(f'Could not parse {path}')
                continue
            if not isinstance(pkg, dict):
                pkg = {}
            prod = list(_table(pkg, 'dependencies'))
            dev = list(_table(pkg, 'devDependencies'))
            has_start = bool(_table(pkg, 'scripts').get('start'))
            is_app = has_start or any(NODE_DEPS.get(dep, {}).get('framework') for dep in prod + dev)
            if not is_app:
                continue  # Workspace root or shared library, not something that runs.
            service = service_at(directory)
            service['runtime'] = 'node'
            service['hasStart'] = has_start
            for dep in prod:
                if dep in NODE_DEPS:
                    apply_dep(NODE_DEPS[dep], dep, path, text, service, True)
            for dep in dev:
                if dep in NODE_DEPS:
                    apply_dep(NODE_DEPS[dep], dep, path, text, service, False)
        elif name in ('requirements.txt', 'pyproject.toml'):
            deps = []
            for line in text.split('\n'):
                match = re.match(r'''^\s*["']?([A-Za-z0-9][A-Za-z0-9_.-]*)''', line)
                if match and match.group(1).lower() in PYTHON_DEPS:
                    deps.append(match.group(1).lower())
            # A pyproject without a framework is usually a workspace root or library, not something that runs.
            is_app = name == 'requirements.txt' or directory in services or any(PYTHON_DEPS[dep].get('framework') for dep in deps)
            if not is_app:
                continue
            service = service_at(directory)
            service['runtime'] = 'python'
            for dep in deps:
                apply_dep(PYTHON_DEPS[dep], dep, path, text, service, True)
        elif name == 'Dockerfile':
            service = service_at(directory)
            service['hasDockerfile'] = True
            service['runtime'] = service['runtime'] or 'container'
            port = re.search(r'^\s*EXPOSE\s+(\d+)', text, re.IGNORECASE | re.MULTILINE)
            if port:
                service['port'] = int(port.group(1))
        elif name == 'Procfile':
            if re.search(r'^worker:', text, re.MULTILINE):
                workers.append({'path': directory, 'evidence': {'file': path, 'line': _line_of(text, 'worker:'), 'detail': 'worker'}})
        elif name in COMPOSE_FILES:
            for index, line in enumerate(text.split('\n')):
                match = re.match(r'''^\s*image:\s*["']?(?:[\w.-]+/)*([\w.-]+)''', line, re.ASCII)
                image = match.group(1).lower() if match else None
                if image in COMPOSE_IMAGES:
                    add_datastore(COMPOSE_IMAGES[image], {'file': path, 'line': index + 1, 'detail': f'image {image}'})
        elif name in ENV_FILES:
            for index, line in enumerate(text.split('\n')):
                match = re.match(r'''^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*["']?(\w+://)?''', line, re.ASCII)
                if not match:
                    continue
                env_vars[match.group(1)] = True  # Name only: values stay out of the map.
                scheme = SCHEMES.get(match.group(2))
                if scheme:
                    add_datastore(scheme, {'file': path, 'line': index + 1, 'detail': match.group(1)})
        elif name in OTHER_RUNTIMES:
            service = service_at(directory)
            if not service['runtime'] or service['runtime'] == 'container':
                service['runtime'] = OTHER_RUNTIMES[name]

    found = list(services.values())
    for service in found:
        service['kind'] = service['kind'] or 'web'
    if not found:
        unknowns.append('No runnable service found. Is the app in a language other than Node or Python, or in an excluded folder?')
    for service in found:
        if service['runtime'] not in ('node', 'python') and not service['hasDockerfile']:
            unknowns.append(f"{service['path']}: {service['runtime']} has no built-in build support yet and no Dockerfile")
        if service['kind'] == 'web' and not service['framework'] and not service['hasDockerfile']:
            unknowns.append(f"{service['path']}: no known framework found, so start command needs confirming")
    if 'AWS SDK' in bindings or 'AWS SDK v2' in bindings:
        unknowns.append('Generic AWS SDK found. Source is not scanned yet, so which services it calls is unknown.')
    if workers and 'redis' not in datastores and 'rabbitmq' not in datastores:
        unknowns.append('Background worker found, but its message broker was not detected.')

    return {'services': found, 'datastores': list(datastores.values()), 'workers': workers, 'bindings': list(bindings.values()), 'envVars': list(env_vars), 'unknowns': unknowns}
