"""Maps a typed request to a proposal. Proposes only: nothing runs until the user approves.

ponytail: keyword rules, so phrasing outside them gets a help message. Add an LLM that outputs this
same shape when free-form chat matters.
"""
import re

CLOUD_WORDS = {'aws': 'aws', 'amazon': 'aws', 'gcp': 'gcp', 'google': 'gcp'}
CLOUD_NAMES = {'aws': 'AWS', 'gcp': 'GCP'}
HELP = 'Try "deploy to aws", "switch to gcp", "roll back", "plan on aws", "check aws connection", "destroy aws" or "refresh costs".'


def _has(pattern, text):
    return re.search(pattern, text) is not None


def proposal_for(text, project):
    text = str(text or '').lower()
    connected = list(project['clouds'])
    clouds = [CLOUD_WORDS[word] for word in re.findall(r'\b(aws|amazon|gcp|google)\b', text)]
    active, previous = project.get('active'), project.get('previous')

    def require_connected(cloud):
        return None if cloud in connected else {'reply': f'{CLOUD_NAMES[cloud]} is not connected yet.'}

    def only_or(cloud):
        return cloud or active or (connected[0] if len(connected) == 1 else None)

    if _has(r'\b(switch|move|migrate|hop)\b', text):
        target = re.search(r'\bto\s+(aws|amazon|gcp|google)\b', text)
        cloud = CLOUD_WORDS[target.group(1)] if target else (clouds[-1] if clouds else None)
        if not cloud:
            return {'reply': 'Which cloud should it move to?'}
        if cloud == active:
            return {'reply': f'App is already on {CLOUD_NAMES[cloud]}.'}
        if not active:
            return {'reply': 'Nothing is deployed yet. Deploy first, then switch.'}
        if project.get('portability') in ('redesign', 'blocked'):
            return {'reply': 'Switch refused: portability report found parts that cannot move automatically. Fix those first.'}
        return require_connected(cloud) or {
            'action': 'switch', 'cloud': cloud, 'source': active,
            'summary': f'Move app from {CLOUD_NAMES[active]} to {CLOUD_NAMES[cloud]}',
            'warnings': [
                'App stops accepting requests while the database is copied. Larger databases mean longer downtime.',
                'Public address changes. Update your DNS afterwards.',
                f'Both clouds bill until you destroy {CLOUD_NAMES[active]}.',
            ],
        }
    if _has(r'\b(roll ?back|undo|revert)\b', text):
        if not previous or not active:
            return {'reply': 'There is no previous cloud to roll back to.'}
        return {
            'action': 'rollback', 'cloud': previous, 'source': active,
            'summary': f'Go back from {CLOUD_NAMES[active]} to {CLOUD_NAMES[previous]}',
            'warnings': [f'Data written on {CLOUD_NAMES[active]} since the switch will not be copied back.'],
        }
    if _has(r'\b(destroy|delete|tear ?down|remove)\b', text):
        cloud = clouds[0] if clouds else None
        if not cloud:
            return {'reply': 'Say which cloud to destroy.'}
        return require_connected(cloud) or {
            'action': 'destroy', 'cloud': cloud, 'source': '', 'confirmText': cloud,
            'summary': f'Permanently delete everything Cloudhop created on {CLOUD_NAMES[cloud]}',
            'warnings': ['This is your live cloud. Your app and its database will be gone.' if cloud == active else 'Its database copy will be gone, so rolling back will no longer be possible.'],
        }
    first = clouds[0] if clouds else None
    if _has(r'\b(cost|costs|spend|credit|credits|bill|refresh)\b', text):
        cloud = only_or(first)
        if not cloud:
            return {'reply': 'Connect a cloud first.'}
        return require_connected(cloud) or {'action': 'report', 'cloud': cloud, 'source': '', 'summary': f'Refresh cost data from {CLOUD_NAMES[cloud]}', 'warnings': ['AWS charges about $0.01 for each cost query.'] if cloud == 'aws' else []}
    if _has(r'\b(verify|check|test)\b', text):
        cloud = only_or(first)
        if not cloud:
            return {'reply': 'Connect a cloud first.'}
        return require_connected(cloud) or {'action': 'verify', 'cloud': cloud, 'source': '', 'summary': f'Check connection to {CLOUD_NAMES[cloud]}', 'warnings': []}
    if _has(r'\bplan\b', text):
        cloud = only_or(first)
        if not cloud:
            return {'reply': 'Connect a cloud first.'}
        return require_connected(cloud) or {'action': 'plan', 'cloud': cloud, 'source': '', 'summary': f'Preview changes on {CLOUD_NAMES[cloud]} without applying them', 'warnings': []}
    if _has(r'\b(deploy|ship|launch|release|update)\b', text):
        cloud = only_or(first)
        if not cloud:
            return {'reply': 'Say which cloud to deploy to.'}
        if active and cloud != active:
            return {'reply': f'App runs on {CLOUD_NAMES[active]}. Say "switch to {cloud}" to move it.'}
        return require_connected(cloud) or {
            'action': 'deploy', 'cloud': cloud, 'source': '',
            'summary': f'Deploy latest default branch to {CLOUD_NAMES[cloud]}',
            'warnings': ['Creates one small VM and a public address. You start paying the cloud provider.'],
        }
    return {'reply': HELP}
