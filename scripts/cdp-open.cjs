const targetUrl = process.argv[2];
const cdpBase = (process.env.CDP_BASE_URL || process.argv[3] || 'http://127.0.0.1:9222').replace(/\/$/, '');

if (!targetUrl) {
  console.error('usage: node scripts/cdp-open.cjs <url> [cdp-base-url]');
  process.exit(2);
}

(async () => {
  const version = await fetch(cdpBase + '/json/version').then((r) => {
    if (!r.ok) throw new Error('CDP version HTTP ' + r.status);
    return r.json();
  });
  const created = await fetch(
    cdpBase + '/json/new?' + encodeURIComponent(targetUrl),
    { method: 'PUT' },
  ).then(async (r) => ({ status: r.status, body: await r.text() }));

  console.log(JSON.stringify({
    cdpBase,
    browser: version.Browser,
    webSocketDebuggerUrl: version.webSocketDebuggerUrl,
    created,
  }));
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
