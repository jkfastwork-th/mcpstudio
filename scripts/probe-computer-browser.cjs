const sessionId = process.argv[2] || process.env.HIRDA_SESSION_ID;
let cdpPort = Number(process.argv[3] || process.env.HIRDA_CDP_PORT || 0);
const studio = (process.env.HIRDA_BASE_URL || process.argv[4] || 'http://127.0.0.1:8100').replace(/\/$/, '');
if (!sessionId) {
  console.error('usage: node scripts/probe-computer-browser.cjs <managed-session-id> [cdp-port] [studio-base-url]');
  process.exit(2);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function ensureRuntime() {
  const startedAt = Date.now();
  const response = await fetch(studio + '/api/computer/descriptor/' + encodeURIComponent(sessionId));
  const body = await response.text();
  console.log('DESCRIPTOR', response.status, Date.now() - startedAt, body.slice(0, 1200));
  if (!response.ok) throw new Error('descriptor failed');
  const descriptor = JSON.parse(body);
  if (!cdpPort) cdpPort = Number(descriptor.cdp_port || 0);
  if (!cdpPort) throw new Error('descriptor did not provide a CDP port');
  return descriptor;
}

async function newPage() {
  const url = studio + '/#computer';
  const r = await fetch('http://127.0.0.1:' + cdpPort + '/json/new?' + encodeURIComponent(url), {method:'PUT'});
  if (!r.ok) throw new Error('CDP new page HTTP ' + r.status + ': ' + await r.text());
  return r.json();
}

async function connectCdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, {once:true});
    ws.addEventListener('error', reject, {once:true});
  });
  let seq = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const {resolve, reject} = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result || {});
      return;
    }
    if (msg.method) {
      if (msg.method.startsWith('Network.webSocket') ||
          msg.method === 'Runtime.exceptionThrown' ||
          msg.method === 'Runtime.consoleAPICalled' ||
          msg.method === 'Log.entryAdded') {
        events.push(msg);
      }
    }
  });
  const call = (method, params={}) => new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, {resolve, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
  return {ws, call, events};
}

(async () => {
  await ensureRuntime();
  const page = await newPage();
  console.log('PAGE', JSON.stringify({id:page.id,url:page.url,title:page.title}));
  const {ws, call, events} = await connectCdp(page.webSocketDebuggerUrl);
  await Promise.all([
    call('Runtime.enable'),
    call('Network.enable'),
    call('Page.enable'),
    call('Log.enable'),
  ]);
  await sleep(1800);

  const before = await call('Runtime.evaluate', {
    expression: `JSON.stringify({
      hash: location.hash,
      message: document.getElementById('computerMessage')?.textContent || null,
      cards: [...document.querySelectorAll('[data-computer-session]')].map(x=>({id:x.dataset.computerSession,status:x.dataset.status}))
    })`,
    returnByValue:true
  });
  console.log('BEFORE', before.result?.value);

  const connect = await call('Runtime.evaluate', {
    expression: `(async()=>{
      for(let i=0;i<50;i++){
        const card=document.querySelector('[data-computer-session="'+${JSON.stringify(sessionId)}+'"]');
        if(card){card.click();return {clicked:true};}
        await new Promise(r=>setTimeout(r,100));
      }
      return {clicked:false};
    })()`,
    awaitPromise:true,
    returnByValue:true
  });
  console.log('CONNECT_CALL', JSON.stringify(connect));
  await sleep(5000);

  const state = await call('Runtime.evaluate', {
    expression: `(()=> {
      const iframe = document.getElementById('computerViewer');
      const out = {
        mainMessage: document.getElementById('computerMessage')?.textContent || null,
        iframeSrc: iframe?.src || null,
        iframeLoaded: !!iframe?.contentDocument,
      };
      try {
        const d = iframe?.contentDocument;
        out.frameHref = iframe?.contentWindow?.location?.href || null;
        out.noVncStatus = d?.getElementById('noVNC_status')?.textContent || null;
        out.noVncStatusClass = d?.getElementById('noVNC_status')?.className || null;
        out.credentialsDialog = d?.getElementById('noVNC_credentials_dlg')?.open ?? null;
        out.passwordVisible = !!(d?.getElementById('noVNC_password_input') && !d.getElementById('noVNC_password_input').hidden);
        out.connectButtonText = d?.getElementById('noVNC_connect_button')?.textContent || null;
        out.bodyText = d?.body?.innerText?.slice(0,1200) || null;
      } catch (e) {
        out.frameError = String(e);
      }
      return JSON.stringify(out);
    })()`,
    returnByValue:true
  });
  console.log('STATE', state.result?.value);

  for (const e of events) {
    if (e.method === 'Network.webSocketCreated') {
      console.log('EV_WS_CREATED', e.params.url);
    } else if (e.method === 'Network.webSocketWillSendHandshakeRequest') {
      console.log('EV_WS_REQUEST', e.params.request?.url || '', JSON.stringify(e.params.request?.headers || {}));
    } else if (e.method === 'Network.webSocketHandshakeResponseReceived') {
      console.log('EV_WS_RESPONSE', e.params.response?.status, JSON.stringify(e.params.response?.headers || {}));
    } else if (e.method === 'Network.webSocketClosed') {
      console.log('EV_WS_CLOSED', JSON.stringify(e.params));
    } else if (e.method === 'Network.webSocketFrameError') {
      console.log('EV_WS_ERROR', JSON.stringify(e.params));
    } else if (e.method === 'Runtime.exceptionThrown') {
      console.log('EV_EXCEPTION', e.params.exceptionDetails?.text, e.params.exceptionDetails?.exception?.description || '');
    } else if (e.method === 'Runtime.consoleAPICalled') {
      console.log('EV_CONSOLE', e.params.type, (e.params.args||[]).map(a=>a.value ?? a.description).join(' '));
    } else if (e.method === 'Log.entryAdded') {
      console.log('EV_LOG', e.params.entry?.level, e.params.entry?.text);
    }
  }

  ws.close();
})().catch(err => {
  console.error(err && err.stack || String(err));
  process.exit(1);
});
