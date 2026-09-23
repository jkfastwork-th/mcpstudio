const sessionId = process.argv[2] || process.env.HIRDA_SESSION_ID;
const studio = (process.env.HIRDA_BASE_URL || process.argv[3] || 'http://127.0.0.1:8100').replace(/\/$/, '');

if (!sessionId) {
  console.error('usage: node scripts/probe-computer-use.cjs <managed-session-id> [studio-base-url]');
  process.exit(2);
}

const targets = [
  studio + '/api/computer/status',
  studio + '/api/computer/descriptor/' + encodeURIComponent(sessionId),
];

(async () => {
  for (const url of targets) {
    try {
      const response = await fetch(url);
      console.log(JSON.stringify({ url, status: response.status, body: await response.text() }));
    } catch (error) {
      console.log(JSON.stringify({ url, error: String(error) }));
    }
  }
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
