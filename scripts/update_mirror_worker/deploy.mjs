// Deploy only to Workers Free; use the maintainer's cf login for plan access.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const cli = fileURLToPath(new URL('./node_modules/cf/bin/cf', import.meta.url));
const result = spawnSync(process.execPath, [cli, 'accounts', 'subscriptions', 'get'], { encoding: 'utf8' });
if (result.status !== 0) {
  process.stderr.write(result.stderr || 'Cannot verify the Workers plan\n');
  process.exit(1);
}
const subscriptions = JSON.parse(result.stdout);
if (!Array.isArray(subscriptions) || !subscriptions.length) {
  throw new Error('Cannot verify the Workers plan');
}
for (const { rate_plan: plan = {} } of subscriptions) {
  const description = `${plan.id || ''} ${plan.public_name || ''}`.toLowerCase();
  if (description.includes('worker') && !description.includes('free')) {
    throw new Error('Workers Paid detected; deployment stopped');
  }
}
const flags = process.argv.slice(2);
if (flags.some((flag) => flag !== '--dry-run')) throw new Error('Unsupported deployment flag');
const deploy = spawnSync(process.execPath, [cli, 'deploy', ...flags], { stdio: 'inherit' });
process.exit(deploy.status ?? 1);
