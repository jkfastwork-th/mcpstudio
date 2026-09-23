const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const chrome = process.env.CHROMIUM_PATH || '/snap/bin/chromium';
const display = process.argv[2] || process.env.DISPLAY || ':2';

const cases = [
  {
    name: 'minimal',
    port: 9330,
    flags: [
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader',
      '--enable-unsafe-webgpu',
      '--ignore-gpu-blocklist',
    ],
  },
  {
    name: 'vulkan-swiftshader',
    port: 9331,
    flags: [
      '--enable-unsafe-webgpu',
      '--ignore-gpu-blocklist',
      '--enable-gpu',
      '--enable-features=Vulkan',
      '--use-angle=vulkan',
      '--use-vulkan=swiftshader',
      '--use-webgpu-adapter=swiftshader',
      '--disable-vulkan-surface',
      '--enable-unsafe-swiftshader',
    ],
  },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitHttp(port) {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch('http://127.0.0.1:' + port + '/json/version');
      if (r.ok) return;
    } catch {}
    await sleep(150);
  }
  throw new Error('CDP did not start on ' + port);
}

function rpc(ws, method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = Math.floor(Math.random() * 1e9);
    const onMessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== id) return;
      ws.removeEventListener('message', onMessage);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    };
    ws.addEventListener('message', onMessage);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function probe(test) {
  const root = path.join(process.cwd(), 'data', 'webgpu-probe-' + test.name);
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });

  const args = [
    '--user-data-dir=' + path.join(root, 'profile'),
    '--remote-debugging-port=' + test.port,
    '--remote-debugging-address=127.0.0.1',
    '--no-first-run',
    '--no-default-browser-check',
    ...test.flags,
    'about:blank',
  ];

  const desktop = path.join(root, 'desktop');
  for (const name of ['config', 'cache', 'state', 'runtime']) {
    const dir = path.join(desktop, name);
    fs.mkdirSync(dir, { recursive: true });
    if (name === 'runtime') fs.chmodSync(dir, 0o700);
  }
  const env = {
    ...process.env,
    DISPLAY: display,
    HOME: root,
    XDG_CONFIG_HOME: path.join(desktop, 'config'),
    XDG_CACHE_HOME: path.join(desktop, 'cache'),
    XDG_STATE_HOME: path.join(desktop, 'state'),
    XDG_RUNTIME_DIR: path.join(desktop, 'runtime'),
  };
  const child = spawn(chrome, args, {
    env,
    stdio: 'ignore',
    detached: true,
  });
  child.unref();

  try {
    await waitHttp(test.port);
    const created = await fetch(
      'http://127.0.0.1:' + test.port + '/json/new?' + encodeURIComponent('https://www.oriverse.com/'),
      { method: 'PUT' }
    ).then((r) => r.json());

    const ws = new WebSocket(created.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });
    await rpc(ws, 'Runtime.enable');
    await sleep(1500);
    const out = await rpc(ws, 'Runtime.evaluate', {
      expression: `(async () => {
        const supported = !!navigator.gpu;
        if (!supported) return { supported, adapter: false };
        const adapter = await navigator.gpu.requestAdapter();
        if (!adapter) return { supported, adapter: false };
        let info = null;
        try {
          if (adapter.info) info = {
            vendor: adapter.info.vendor || '',
            architecture: adapter.info.architecture || '',
            device: adapter.info.device || '',
            description: adapter.info.description || '',
          };
        } catch {}
        return {
          supported,
          adapter: true,
          info,
          features: [...adapter.features].slice(0, 30),
        };
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    ws.close();
    return { name: test.name, result: out.result.value };
  } finally {
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch {}
  }
}

(async () => {
  const results = [];
  for (const test of cases) {
    try {
      results.push(await probe(test));
    } catch (error) {
      results.push({ name: test.name, error: String(error) });
    }
  }
  console.log(JSON.stringify(results));
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
