// Maps a typed request to a proposal. Proposes only: nothing runs until the user approves.
// ponytail: keyword rules, so phrasing outside them gets a help message. Add an LLM that outputs this same shape when free-form chat matters.

const CLOUD_WORDS = { aws: 'aws', amazon: 'aws', gcp: 'gcp', google: 'gcp' };
const CLOUD_NAMES = { aws: 'AWS', gcp: 'GCP' };
const HELP = 'Try "deploy to aws", "switch to gcp", "roll back", "plan on aws", "check aws connection", "destroy aws" or "refresh costs".';

function mentionedClouds(text) {
  return [...text.matchAll(/\b(aws|amazon|gcp|google)\b/g)].map((match) => CLOUD_WORDS[match[1]]);
}

export function proposalFor(text, project) {
  const input = String(text ?? '').toLowerCase();
  const connected = Object.keys(project.clouds);
  const clouds = mentionedClouds(input);
  const { active, previous } = project;
  const requireConnected = (cloud) => (connected.includes(cloud) ? null : { reply: `${CLOUD_NAMES[cloud]} is not connected yet.` });
  const onlyOr = (cloud) => cloud ?? active ?? (connected.length === 1 ? connected[0] : null);

  if (/\b(switch|move|migrate|hop)\b/.test(input)) {
    const target = input.match(/\bto\s+(aws|amazon|gcp|google)\b/)?.[1];
    const cloud = target ? CLOUD_WORDS[target] : clouds[clouds.length - 1];
    if (!cloud) return { reply: 'Which cloud should it move to?' };
    if (cloud === active) return { reply: `App is already on ${CLOUD_NAMES[cloud]}.` };
    if (!active) return { reply: 'Nothing is deployed yet. Deploy first, then switch.' };
    if (project.portability === 'redesign' || project.portability === 'blocked') {
      return { reply: 'Switch refused: portability report found parts that cannot move automatically. Fix those first.' };
    }
    return requireConnected(cloud) ?? {
      action: 'switch', cloud, source: active,
      summary: `Move app from ${CLOUD_NAMES[active]} to ${CLOUD_NAMES[cloud]}`,
      warnings: [
        'App stops accepting requests while the database is copied. Larger databases mean longer downtime.',
        'Public address changes. Update your DNS afterwards.',
        `Both clouds bill until you destroy ${CLOUD_NAMES[active]}.`,
      ],
    };
  }
  if (/\b(roll ?back|undo|revert)\b/.test(input)) {
    if (!previous || !active) return { reply: 'There is no previous cloud to roll back to.' };
    return {
      action: 'rollback', cloud: previous, source: active,
      summary: `Go back from ${CLOUD_NAMES[active]} to ${CLOUD_NAMES[previous]}`,
      warnings: [`Data written on ${CLOUD_NAMES[active]} since the switch will not be copied back.`],
    };
  }
  if (/\b(destroy|delete|tear ?down|remove)\b/.test(input)) {
    const cloud = clouds[0];
    if (!cloud) return { reply: 'Say which cloud to destroy.' };
    return requireConnected(cloud) ?? {
      action: 'destroy', cloud, source: '', confirmText: cloud,
      summary: `Permanently delete everything Cloudhop created on ${CLOUD_NAMES[cloud]}`,
      warnings: [cloud === active ? 'This is your live cloud. Your app and its database will be gone.' : 'Its database copy will be gone, so rolling back will no longer be possible.'],
    };
  }
  if (/\b(cost|costs|spend|credit|credits|bill|refresh)\b/.test(input)) {
    const cloud = onlyOr(clouds[0]);
    if (!cloud) return { reply: 'Connect a cloud first.' };
    return requireConnected(cloud) ?? { action: 'report', cloud, source: '', summary: `Refresh cost data from ${CLOUD_NAMES[cloud]}`, warnings: cloud === 'aws' ? ['AWS charges about $0.01 for each cost query.'] : [] };
  }
  if (/\b(verify|check|test)\b/.test(input)) {
    const cloud = onlyOr(clouds[0]);
    if (!cloud) return { reply: 'Connect a cloud first.' };
    return requireConnected(cloud) ?? { action: 'verify', cloud, source: '', summary: `Check connection to ${CLOUD_NAMES[cloud]}`, warnings: [] };
  }
  if (/\bplan\b/.test(input)) {
    const cloud = onlyOr(clouds[0]);
    if (!cloud) return { reply: 'Connect a cloud first.' };
    return requireConnected(cloud) ?? { action: 'plan', cloud, source: '', summary: `Preview changes on ${CLOUD_NAMES[cloud]} without applying them`, warnings: [] };
  }
  if (/\b(deploy|ship|launch|release|update)\b/.test(input)) {
    const cloud = onlyOr(clouds[0]);
    if (!cloud) return { reply: 'Say which cloud to deploy to.' };
    if (active && cloud !== active) return { reply: `App runs on ${CLOUD_NAMES[active]}. Say "switch to ${cloud}" to move it.` };
    return requireConnected(cloud) ?? {
      action: 'deploy', cloud, source: '',
      summary: `Deploy latest default branch to ${CLOUD_NAMES[cloud]}`,
      warnings: ['Creates one small VM and a public address. You start paying the cloud provider.'],
    };
  }
  return { reply: HELP };
}
