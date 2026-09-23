const cdpBase = (process.env.CDP_BASE_URL || process.argv[2] || 'http://127.0.0.1:9222').replace(/\/$/, '');

(async () => {
  const response = await fetch(cdpBase + '/json');
  if (!response.ok) throw new Error('CDP pages HTTP ' + response.status);
  const pages = await response.json();
  console.log(JSON.stringify(pages.map((p) => ({
    id: p.id,
    type: p.type,
    title: p.title,
    url: p.url,
    webSocketDebuggerUrl: p.webSocketDebuggerUrl,
  }))));
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
