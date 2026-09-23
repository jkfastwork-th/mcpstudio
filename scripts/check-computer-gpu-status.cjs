const studio = (process.env.HIRDA_BASE_URL || process.argv[2] || 'http://127.0.0.1:8100').replace(/\/$/, '');

(async () => {
  const status = await fetch(studio + '/api/computer/status').then(async (r) => {
    if (!r.ok) throw new Error('status ' + r.status + ' ' + (await r.text()).slice(0, 300));
    return r.json();
  });
  const safe = {
    enabled: status.enabled,
    configured: status.configured,
    runtime_mode: status.runtime_mode,
    gpu_mode: status.gpu_mode,
    gpu_hardware_available: status.gpu_hardware_available,
    transport_ready: status.transport_ready,
    cdp_port: status.cdp_port,
    runtime_displays: status.runtime_displays,
  };
  console.log(JSON.stringify(safe));
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
