const fs = require('node:fs');

const sessionId = process.argv[2] || process.env.HIRDA_SESSION_ID;
if (!sessionId) {
  console.error('usage: node scripts/show-computer-chrome.cjs <managed-session-id>');
  process.exit(2);
}

const registry = JSON.parse(fs.readFileSync('data/computers/registry.json', 'utf8'));
const runtime = registry.sessions?.[sessionId];
if (!runtime) {
  console.error('runtime not found for ' + sessionId);
  process.exit(2);
}

const wanted = '--user-data-dir=' + runtime.profile_dir;
const matches = [];
for (const name of fs.readdirSync('/proc')) {
  if (!/^\d+$/.test(name)) continue;
  try {
    const args = fs.readFileSync('/proc/' + name + '/cmdline')
      .toString('utf8')
      .split('\0')
      .filter(Boolean);
    if (!args.includes(wanted)) continue;
    if (args.some((x) => x.startsWith('--type='))) continue;
    const envRaw = fs.readFileSync('/proc/' + name + '/environ').toString('utf8');
    const env = Object.fromEntries(envRaw.split('\0').filter(Boolean).map((entry) => {
      const index = entry.indexOf('=');
      return [entry.slice(0, index), entry.slice(index + 1)];
    }));
    matches.push({
      pid: Number(name),
      args,
      env: {
        DISPLAY: env.DISPLAY || null,
        WAYLAND_DISPLAY: env.WAYLAND_DISPLAY || null,
        HOME: env.HOME || null,
        XDG_CONFIG_HOME: env.XDG_CONFIG_HOME || null,
        XDG_CACHE_HOME: env.XDG_CACHE_HOME || null,
        XDG_STATE_HOME: env.XDG_STATE_HOME || null,
        XDG_RUNTIME_DIR: env.XDG_RUNTIME_DIR || null,
        DBUS_SESSION_BUS_ADDRESS: env.DBUS_SESSION_BUS_ADDRESS || null,
        XAUTHORITY: env.XAUTHORITY || null,
      },
    });
  } catch {}
}
console.log(JSON.stringify({ sessionId, runtime, matches }));
