"""Deterministic cost and credit math. No LLM may change these numbers.

ponytail: hardcoded on-demand list prices (us-east-1 / us-central1) written from memory, not checked
against a price API. Replace with cached AWS Price List and GCP Catalog data before relying on them.
"""
from .timeutil import now_ms, parse_date_ms

PRICES_VERIFIED = False

HOURS = 730
SECONDS = HOURS * 3600
REQUEST_KB = 50
REQUEST_SECONDS = 0.2
EGRESS = {'aws': 0.09, 'gcp': 0.12}
MONTH_MS = (365.25 / 12) * 24 * 3600 * 1000


def hourly(rate, count=1):
    return rate * HOURS * count


def fargate(vcpu, gb):
    return hourly(0.04048 * vcpu + 0.004445 * gb)


def has(service_map, *kinds):
    return any(store['kind'] in kinds for store in service_map['datastores'])


def line(label, amount):
    return {'label': label, 'amount': amount}


def _if(condition, *items):
    return list(items) if condition else []


# Each builder returns cost lines for one app on one setup: (map, web count, requests per month) -> lines.
BLUEPRINTS = [
    {
        'id': 'aws-vm', 'cloud': 'aws', 'tier': 'cheapest', 'reliability': 1, 'name': 'AWS: single VM with Compose',
        'note': 'All services and data on one machine. You own backups. Downtime if it fails.',
        'build': lambda m, web, rpm: [line('EC2 t3.small', hourly(0.0208)), line('Disk 20 GB', 1.6), line('Public IP', hourly(0.005))],
    },
    {
        'id': 'aws-balanced', 'cloud': 'aws', 'tier': 'balanced', 'reliability': 2, 'name': 'AWS: ECS Express with managed data',
        'note': 'Managed containers and database in one zone, with automatic backups.',
        'build': lambda m, web, rpm: [
            line(f'Fargate x{web}', fargate(0.25, 0.5) * web),
            line('Public IP per task', hourly(0.005, web)),
            line('Load balancer', hourly(0.0225)),
            *_if(has(m, 'postgres', 'mysql'), line('RDS t4g.micro + 20 GB', hourly(0.016) + 2.3)),
            *_if(has(m, 'redis'), line('ElastiCache t4g.micro', hourly(0.016))),
            *_if(has(m, 'objects'), line('S3 10 GB', 0.23)),
        ],
    },
    {
        'id': 'aws-prod', 'cloud': 'aws', 'tier': 'production', 'reliability': 3, 'name': 'AWS: multi-zone production',
        'note': 'Two copies of each service, database failover, private networking.',
        'build': lambda m, web, rpm: [
            line(f'Fargate x{web * 2}', fargate(0.5, 1) * web * 2),
            line('Load balancer', hourly(0.0225)),
            line('NAT gateway', hourly(0.045)),
            *_if(has(m, 'postgres', 'mysql'), line('RDS t4g.small Multi-AZ + 20 GB', hourly(0.065) + 4.6)),
            *_if(has(m, 'redis'), line('ElastiCache t4g.small x2', hourly(0.032, 2))),
            *_if(has(m, 'objects'), line('S3 10 GB', 0.23)),
        ],
    },
    {
        'id': 'gcp-vm', 'cloud': 'gcp', 'tier': 'cheapest', 'reliability': 1, 'name': 'GCP: single VM with Compose',
        'note': 'All services and data on one machine. You own backups. Downtime if it fails.',
        'build': lambda m, web, rpm: [line('e2-small', hourly(0.0168)), line('Disk 20 GB', 2), line('Public IP', hourly(0.005))],
    },
    {
        'id': 'gcp-balanced', 'cloud': 'gcp', 'tier': 'balanced', 'reliability': 2, 'name': 'GCP: Cloud Run with managed data',
        'note': 'Scales to zero when idle, so first request can be slow. Database has automatic backups.',
        'build': lambda m, web, rpm: [
            line(f'Cloud Run x{web}', rpm * REQUEST_SECONDS * (0.000024 + 0.0000025 * 0.5)),
            *_if(has(m, 'postgres', 'mysql'), line('Cloud SQL db-f1-micro + 20 GB', hourly(0.0105) + 3.4)),
            *_if(has(m, 'redis'), line('Memorystore basic 1 GB', hourly(0.049))),
            *_if(has(m, 'objects'), line('Cloud Storage 10 GB', 0.2)),
        ],
    },
    {
        'id': 'gcp-prod', 'cloud': 'gcp', 'tier': 'production', 'reliability': 3, 'name': 'GCP: always-on with database failover',
        'note': 'Always-on instances, high-availability database and Redis.',
        'build': lambda m, web, rpm: [
            line(f'Cloud Run always-on x{web}', SECONDS * (0.000018 + 0.000002 * 0.5) * web),
            *_if(has(m, 'postgres', 'mysql'), line('Cloud SQL 1 vCPU HA + 20 GB', hourly(0.138) + 6.8)),
            *_if(has(m, 'redis'), line('Memorystore standard 1 GB', hourly(0.098))),
            *_if(has(m, 'objects'), line('Cloud Storage 10 GB', 0.2)),
        ],
    },
]

STATIC_BLUEPRINTS = [
    {'id': 'aws-static', 'cloud': 'aws', 'tier': 'static', 'reliability': 3, 'name': 'AWS: S3 and CloudFront', 'note': 'Static files on a CDN.', 'build': lambda m, web, rpm: [line('Storage and CDN base', 1)]},
    {'id': 'gcp-static', 'cloud': 'gcp', 'tier': 'static', 'reliability': 3, 'name': 'GCP: Cloud Storage and CDN', 'note': 'Static files on a CDN.', 'build': lambda m, web, rpm: [line('Storage and CDN base', 1)]},
]


def simulate_credits(monthly, credits, today, horizon=120):
    """Burns credits month by month, earliest expiry first. Unspent balance past expiry is waste."""
    pool = [{'left': credit['amount'], 'expires': (parse_date_ms(credit['expiresOn']) - today) / MONTH_MS} for credit in credits]
    pool = sorted((credit for credit in pool if credit['expires'] > 0), key=lambda credit: credit['expires'])
    total = sum(credit['left'] for credit in pool)
    covered_months = 0
    out_of_pocket12 = 0
    for month in range(horizon):
        due = monthly
        for credit in pool:
            if credit['expires'] <= month or due == 0:
                continue
            used = min(credit['left'], due)
            credit['left'] -= used
            due -= used
        if month < 12:
            out_of_pocket12 += due
        if due == 0 and covered_months == month:
            covered_months += 1
    wasted = sum(credit['left'] for credit in pool)
    return {'total': total, 'coveredMonths': covered_months, 'outOfPocket12': out_of_pocket12, 'wasted': wasted}


def rank_plans(service_map, requests_per_day=1000, credits=(), today=None):
    today = now_ms() if today is None else today
    web_count = sum(1 for service in service_map['services'] if service['kind'] == 'web')
    static_only = bool(service_map['services']) and web_count == 0 and not service_map['datastores']
    blueprints = STATIC_BLUEPRINTS if static_only else BLUEPRINTS
    requests_per_month = requests_per_day * 30
    egress_gb = (requests_per_month * REQUEST_KB) / 1e6
    priced = {'postgres', 'mysql', 'redis', 'objects'}
    excluded = [store['kind'] for store in service_map['datastores'] if store['kind'] not in priced]

    plans = []
    for blueprint in blueprints:
        lines = blueprint['build'](service_map, max(web_count, 1), requests_per_month)
        lines.append(line(f'Egress {egress_gb:.1f} GB', egress_gb * EGRESS[blueprint['cloud']]))
        monthly = sum(item['amount'] for item in lines)
        credit = simulate_credits(monthly, [item for item in credits if item['cloud'] == blueprint['cloud']], today)
        plans.append({
            'deployable': blueprint['id'] in ('aws-vm', 'gcp-vm'),
            'id': blueprint['id'], 'cloud': blueprint['cloud'], 'tier': blueprint['tier'], 'name': blueprint['name'], 'note': blueprint['note'], 'reliability': blueprint['reliability'],
            'lines': lines, 'monthly': monthly, 'low': monthly * 0.8, 'high': monthly * 1.5, 'credit': credit,
        })

    # Lowest real money over 12 months wins. On a tie, take the more reliable plan,
    # because credits that would expire unused should buy better infrastructure.
    plans.sort(key=lambda plan: (plan['credit']['outOfPocket12'], -plan['reliability'], plan['monthly']))
    return {'plans': plans, 'excluded': excluded, 'pricesVerified': PRICES_VERIFIED}
