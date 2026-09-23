const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const sessionId = process.argv[2];
if (!sessionId) process.exit(2);

const chrome = process.env.CHROMIUM_PATH || '/snap/bin/chromium';
const registry = JSON.parse(fs.readFileSync('data/computers/registry.json', 'utf8'));
const runtime = registry.sessions?.[sessionId];
if (!runtime) throw new Error('runtime not found');

const token = '--user-data-dir=' + runtime.profile_dir;

function mainPids() {
  const out = [];
  for (const name of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(name)) continue;
    try {
      const args = fs.readFileSync('/proc/' + name + '/cmdline').toString('utf8').split('\0').filter(Boolean);
      if (!args.includes(token)) continue;
      if (args.some((x) => x.startsWith('--type='))) continue;
      out.push(Number(name));
    } catch {}
  }
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdpUp() {
  try {
    const r = await fetch('http://127.0.0.1:' + runtime.cdp_port + '/json/version');
    return r.ok;
  } catch {
    return false;
  }
}

(async () => {
  for (const pid of mainPids()) {
    try { process.kill(pid, 'SIGTERM'); } catch {}
  }

  for (let i = 0; i < 50 && await cdpUp(); i++) await sleep(100);

  const root = runtime.root;
  const env = {
    ...process.env,
    DISPLAY: ':' + runtime.display,
    HOME: path.join(root, 'home'),
    XDG_CONFIG_HOME: path.join(root, 'desktop', 'config'),
    XDG_CACHE_HOME: path.join(root, 'desktop', 'cache'),
    XDG_STATE_HOME: path.join(root, 'desktop', 'state'),
    XDG_RUNTIME_DIR: path.join(root, 'desktop', 'runtime'),
    TMPDIR: '/tmp',
    TMP: '/tmp',
    TEMP: '/tmp',
  };

  const log = fs.openSync(runtime.chrome_log, 'a');
  const args = [
    '--user-data-dir=' + runtime.profile_dir,
    '--remote-debugging-port=' + runtime.cdp_port,
    '--remote-debugging-address=127.0.0.1',
    '--no-first-run',
    '--no-default-browser-check',
    '--enable-gpu',
    '--enable-unsafe-webgpu',
    '--ignore-gpu-blocklist',
    '--enable-features=Vulkan',
    '--use-gl=angle',
    '--use-angle=vulkan',
    '--use-vulkan=swiftshader',
    '--use-webgpu-adapter=swiftshader',
    '--disable-vulkan-surface',
    '--enable-unsafe-swiftshader',
    'about:blank',
  ];

  const child = spawn(chrome, args, {
    env,
    stdio: ['ignore', log, log],
    detached: true,
  });
  child.unref();

  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (await cdpUp()) {
      ready = true;
      break;
    }
    await sleep(150);
  }

  console.log(JSON.stringify({
    ready,
    pid: child.pid,
    display: ':' + runtime.display,
    cdp_port: runtime.cdp_port,
    profile_dir: runtime.profile_dir,
  }));
  if (!ready) process.exit(1);
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
