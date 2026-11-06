// Writes the AWS and GCP setup files Cloudhop would hand out for each code host, so CI can lint them.
// Usage: node scripts/render-setup-files.js <out-dir>
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { bootstrapFor, trustFor } from '../src/generate.js';

const out = process.argv[2] ?? 'rendered';
const samples = {
  github: { repo: 'acme/shop' },
  gitlab: { repo: 'acme/platform/shop', defaultBranch: 'main' },
  bitbucket: { workspace: 'acme', workspaceUuid: '{922e118c-f34f-4226-a897-0e0afbec553d}', repositoryUuid: '{1de489be-ce6a-42a0-a8c8-eadbf1174ac7}' },
  azure: { organization: 'acme', project: 'Web Shop', organizationId: '00000000-0000-4000-8000-000000000001' },
};
mkdirSync(out, { recursive: true });
for (const [provider, info] of Object.entries(samples)) {
  const trust = trustFor(provider, info);
  writeFileSync(join(out, `${provider}-aws.yml`), bootstrapFor('aws', 'acme/shop', {}, trust));
  writeFileSync(join(out, `${provider}-gcp.sh`), bootstrapFor('gcp', 'acme/shop', { projectId: 'acme-shop', region: 'us-central1' }, trust));
}
console.log(`Rendered setup files for ${Object.keys(samples).join(', ')} in ${out}`);
