function clean(value, limit = 160, showLinks = false) {
  let text = String(value ?? '').replaceAll('\0', '');
  if (!showLinks) text = text.replace(/https?:\/\/\S+|www\.\S+/g, '[链接]');
  text = text.replace(/\s+/g, ' ').trim();
  return limit === null ? text : text.slice(0, limit);
}

function releaseNotes(body, showLinks) {
  return String(body ?? '').split(/\r?\n/).filter(line => !line.trim().match(/^(?:```|~~~)/)).map(line =>
    clean(line.trim().replace(/^#{1,6}\s+/, '').replace(/^(?:[-*+]\s+|\d+[.)]\s+)/, '• ')
      .replace(/!?\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/\*\*|__|`/g, ''), null, showLinks),
  ).join('\n').trim();
}

export function splitAnnouncement(message) {
  if (message.length <= 1000) return [message];
  const [header, ...lines] = message.split('\n');
  const capacity = Math.max(1, 1000 - header.length - 32);
  const chunks = [];
  let current = '';
  for (let line of lines) {
    line += '\n';
    if (current && current.length + line.length > capacity) {
      chunks.push(current.trimEnd());
      current = '';
    }
    while (line.length > capacity) {
      // Keep UTF-16 surrogate pairs together at message boundaries.
      const end = /[\uD800-\uDBFF]/.test(line[capacity - 1]) ? capacity - 1 : capacity;
      chunks.push(line.slice(0, end));
      line = line.slice(end);
    }
    current += line;
  }
  if (current.trim()) chunks.push(current.trimEnd());
  return chunks.map((part, index) => `${header}（${index + 1}/${chunks.length}）\n${part}`);
}

export function formatEvent(type, payload, env) {
  const showLinks = env.SHOW_LINKS === 'true';
  const text = (value, limit = 160) => clean(value, limit, showLinks);
  const actor = text(payload.sender?.login ?? 'GitHub', 50);
  const action = payload.action;
  const header = `【GitHub · ${env.REPOSITORY}】`;
  let lines;
  let url;
  if (type === 'push') {
    lines = [header, `${actor} ${payload.deleted ? '删除分支' : '推送提交'}：${text(String(payload.ref ?? '').replace(/^refs\/heads\//, ''))}`];
    const commits = payload.commits ?? [];
    for (const commit of commits.slice(0, 5)) lines.push(`${String(commit.id ?? '').slice(0, 7)} ${text(String(commit.message ?? '').split('\n')[0])}`);
    if (commits.length > 5) lines.push(`另有 ${commits.length - 5} 条提交`);
    url = payload.compare;
  } else if (type === 'issues' || type === 'pull_request') {
    if (!['opened', 'closed', 'reopened', 'ready_for_review'].includes(action)) return null;
    const item = payload[type === 'issues' ? 'issue' : 'pull_request'] ?? {};
    const kind = type === 'issues' ? 'Issue' : 'PR';
    const verb = kind === 'PR' && action === 'closed' && item.merged ? '合并'
      : { opened: '新建', closed: '关闭', reopened: '重新打开', ready_for_review: '请求评审' }[action];
    lines = [header, `${actor} ${verb} ${kind} #${item.number ?? ''}`, text(item.title)];
    url = item.html_url;
  } else if (type === 'release') {
    if (action !== 'published' || payload.release?.draft) return null;
    lines = [`【MaaNikki 更新 · ${text(payload.release?.tag_name, 60)}】`,
      releaseNotes(payload.release?.body, showLinks) || '新版本已发布。',
      '更新方式：打开 MaaNikki，检查更新。'];
    if (showLinks && payload.release?.html_url) lines.push(payload.release.html_url);
    return lines.join('\n');
  } else if (type === 'workflow_run') {
    if (action !== 'completed') return null;
    const run = payload.workflow_run ?? {};
    const result = { failure: '失败', timed_out: '超时', action_required: '需要处理', startup_failure: '启动失败' }[run.conclusion];
    if (!result) return null;
    lines = [header, `工作流 ${text(run.name)}：${result}`, `分支：${text(run.head_branch)} · #${run.run_number ?? ''}`];
    url = run.html_url;
  } else return null;
  if (showLinks && url) lines.push(url);
  return lines.filter(Boolean).join('\n').slice(0, 1800);
}
