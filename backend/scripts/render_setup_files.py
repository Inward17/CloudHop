"""Writes the AWS and GCP setup files Cloudhop would hand out for each code host, so CI can lint them.

Usage: python scripts/render_setup_files.py <out-dir>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloudhop.generate import bootstrap_for, trust_for  # noqa: E402

SAMPLES = {
    'github': {'repo': 'acme/shop'},
    'gitlab': {'repo': 'acme/platform/shop', 'defaultBranch': 'main'},
    'bitbucket': {'workspace': 'acme', 'workspaceUuid': '{922e118c-f34f-4226-a897-0e0afbec553d}', 'repositoryUuid': '{1de489be-ce6a-42a0-a8c8-eadbf1174ac7}'},
    'azure': {'organization': 'acme', 'project': 'Web Shop', 'organizationId': '00000000-0000-4000-8000-000000000001'},
}

out = Path(sys.argv[1] if len(sys.argv) > 1 else 'rendered')
out.mkdir(parents=True, exist_ok=True)
for provider, info in SAMPLES.items():
    trust = trust_for(provider, info)
    (out / f'{provider}-aws.yml').write_text(bootstrap_for('aws', 'acme/shop', {}, trust), newline='\n')
    (out / f'{provider}-gcp.sh').write_text(bootstrap_for('gcp', 'acme/shop', {'projectId': 'acme-shop', 'region': 'us-central1'}, trust), newline='\n')
print(f"Rendered setup files for {', '.join(SAMPLES)} in {out}")
