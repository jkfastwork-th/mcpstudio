const sessionId = process.argv[2] || process.env.HIRDA_SESSION_ID;
const studio = (process.env.HIRDA_BASE_URL || process.argv[3] || 'http://127.0.0.1:8100').replace(/\/$/, '');

if (!sessionId) {
  console.error('usage: node scripts/probe-computer-ws.cjs <managed-session-id> [studio-base-url]');
  process.exit(2);
}

async function getJson(url) {
  const response = await fetch(url);
  const body = await response.text();
  console.log('HTTP', response.status, url);
  console.log(body.slice(0, 4000));
  if (!response.ok) throw new Error('HTTP ' + response.status + ' for ' + url);
  return JSON.parse(body);
}

function wsProbe(wsUrl) {
  return new Promise((resolve) => {
    console.log('WS_CONNECT', wsUrl);
    const ws = new WebSocket(wsUrl, ['binary']);
    ws.binaryType = 'arraybuffer';
    const timer = setTimeout(() => {
      console.log('WS_TIMEOUT', ws.readyState, 'protocol=', ws.protocol);
      try { ws.close(); } catch {}
      resolve();
    }, 7000);
    ws.addEventListener('open', () => {
      console.log('WS_OPEN', 'protocol=', JSON.stringify(ws.protocol));
    });
    ws.addEventListener('message', (event) => {
      const payload = Buffer.from(event.data);
      console.log('WS_DATA', JSON.stringify(payload.toString('latin1')), payload.toString('hex'));
      clearTimeout(timer);
      ws.close();
      resolve();
    });
    ws.addEventListener('close', (event) => {
      console.log('WS_CLOSE', event.code, JSON.stringify(event.reason), 'clean=', event.wasClean, 'protocol=', JSON.stringify(ws.protocol));
      clearTimeout(timer);
      resolve();
    });
    ws.addEventListener('error', (event) => {
      console.log('WS_ERROR', event && event.message ? event.message : String(event));
    });
  });
}

(async () => {
  await getJson(studio + '/api/computer/status');
  const descriptor = await getJson(studio + '/api/computer/descriptor/' + encodeURIComponent(sessionId));
  const wsBase = studio.replace(/^http/, 'ws');
  await wsProbe(wsBase + descriptor.websocket_path);
})().catch((error) => {
  console.error(error && error.stack || String(error));
  process.exit(1);
});
