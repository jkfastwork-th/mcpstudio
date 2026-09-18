/* === Computer UI === */
const computerViewMeta = {
  title: 'Computer',
  subtitle: 'Use a shared browser desktop for OAuth, logins and human-in-the-loop actions.'
};

function setComputerActive(){
  const computerSection = document.querySelector('[data-page="computer"]');
  if(computerSection) computerSection.classList.add('active');
  const computerLink = document.querySelector('.primary-nav a[data-view="computer"], .mobile-nav a[data-view="computer"]');
  if(computerLink) computerLink.classList.add('active');
}

function clearComputerActive(){
  const computerSection = document.querySelector('[data-page="computer"]');
  if(computerSection) computerSection.classList.remove('active');
  const computerLink = document.querySelector('.primary-nav a[data-view="computer"], .mobile-nav a[data-view="computer"]');
  if(computerLink) computerLink.classList.remove('active');
}

async function loadComputerData(){
  const statusEl = document.getElementById('computerStatus');
  const messageEl = document.getElementById('computerMessage');
  const sessionSelect = document.getElementById('computerSessionSelect');
  if(!statusEl || !messageEl || !sessionSelect) return;

  try {
    statusEl.className = 'pill unknown';
    statusEl.textContent = 'checking';
    messageEl.textContent = 'Loading computer status…';
    messageEl.className = 'computer-message';

    const [computerStatus, managedSessionsData] = await Promise.all([
      getJson('/api/computer/status').catch(()=>null),
      getJson('/api/managed/sessions').catch(()=>null),
    ]);

    const computer = computerStatus || {};
    const managed = managedSessionsData || {};
    const sessions = managed.sessions || [];

    if(!computer.enabled){
      statusEl.className = 'pill unknown';
      statusEl.textContent = 'disabled';
      messageEl.textContent = 'Computer Use is disabled in HIRDA config.';
      messageEl.className = 'computer-message error';
      sessionSelect.innerHTML = '<option value="">disabled</option>';
      return;
    }

    const novncMissing = !computer.novnc_available;
    const websockifyOffline = !computer.websockify_reachable;

    statusEl.className = 'pill ' + (
      novncMissing || websockifyOffline ? 'degraded' : 'healthy'
    );
    statusEl.textContent = novncMissing ? 'noVNC missing' : websockifyOffline ? 'websockify offline' : 'ready';

    const reasons = [];
    if(novncMissing) reasons.push('local noVNC directory missing or incomplete');
    if(websockifyOffline) reasons.push('local websockify bridge not reachable');
    messageEl.textContent = reasons.length ? reasons.join('; ') + '.' : 'Computer desktop ready.';
    messageEl.className = 'computer-message ' + (reasons.length ? 'error' : 'good');

    const selectedId = sessionSelect.value;
    sessionSelect.innerHTML = '<option value="">Choose session…</option>' +
      sessions.map(session => {
        const name = session.name || session.workspace_key || 'session';
        const status = session.status || 'unknown';
        return `<option value="${esc(session.id)}">${esc(name)} — ${esc(status)}</option>`;
      }).join('');
    if(sessions.some(s=>s.id===selectedId)) sessionSelect.value = selectedId;

  } catch(err){
    statusEl.className = 'pill down';
    statusEl.textContent = 'error';
    messageEl.textContent = err.message || 'Failed to load computer status.';
    messageEl.className = 'computer-message error';
  }
}

async function connectComputer(){
  const sessionSelect = document.getElementById('computerSessionSelect');
  const tokenInput = document.getElementById('computerTokenInput');
  const messageEl = document.getElementById('computerMessage');
  const viewer = document.getElementById('computerViewer');
  if(!sessionSelect || !viewer) return;

  const sessionId = sessionSelect.value;
  if(!sessionId){
    messageEl.textContent = 'Select a managed session first.';
    messageEl.className = 'computer-message warning';
    return;
  }

  try {
    const computerStatus = await getJson('/api/computer/status');
    if(!computerStatus || !computerStatus.enabled){
      messageEl.textContent = 'Computer Use is disabled.';
      messageEl.className = 'computer-message error';
      return;
    }
    if(!computerStatus.novnc_available){
      messageEl.textContent = 'Local noVNC is not available; cannot connect.';
      messageEl.className = 'computer-message error';
      return;
    }
    if(!computerStatus.websockify_reachable){
      messageEl.textContent = 'Local websockify is offline; cannot connect.';
      messageEl.className = 'computer-message error';
      return;
    }

    const descriptor = await getJson('/api/computer/descriptor/' + encodeURIComponent(sessionId));
    if(!descriptor || !descriptor.viewer_url){
      messageEl.textContent = 'No viewer URL for this session.';
      messageEl.className = 'computer-message error';
      return;
    }

    const token = (tokenInput && tokenInput.value || '').trim();
    let wsPath = descriptor.websocket_path;
    if(token) wsPath += '?token=' + encodeURIComponent(token);

    const viewerUrl = new URL(descriptor.viewer_url, location.origin);
    viewerUrl.searchParams.set('autoconnect', 'true');
    viewerUrl.searchParams.set('resize', 'scale');
    viewerUrl.searchParams.set('path', wsPath);

    viewer.src = viewerUrl.href;
    messageEl.textContent = 'Connected to ' + (descriptor.managed_session_id || sessionId) + '.';
    messageEl.className = 'computer-message good';

  } catch(err){
    messageEl.textContent = 'Connection failed: ' + (err.message || 'unknown error');
    messageEl.className = 'computer-message error';
  }
}

function disconnectComputer(){
  const viewer = document.getElementById('computerViewer');
  const messageEl = document.getElementById('computerMessage');
  if(viewer) viewer.src = 'about:blank';
  if(messageEl){
    messageEl.textContent = 'Disconnected.';
    messageEl.className = 'computer-message';
  }
}

async function fullscreenComputer(){
  const viewerShell = document.getElementById('computerViewerShell');
  if(!viewerShell) return;
  try {
    if(!document.fullscreenElement){
      await viewerShell.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch(err){
    // Fullscreen may be denied; ignore silently.
  }
}

document.addEventListener('click', e => {
  const target = e.target.closest('button');
  if(!target) return;

  if(target.id === 'computerConnectBtn'){
    connectComputer();
  } else if(target.id === 'computerDisconnectBtn'){
    disconnectComputer();
  } else if(target.id === 'computerReconnectBtn'){
    disconnectComputer();
    setTimeout(connectComputer, 100);
  } else if(target.id === 'computerFullscreenBtn'){
    fullscreenComputer();
  }
});

document.getElementById('computerSessionSelect')?.addEventListener('change', () => {
});

document.getElementById('computerTokenInput')?.addEventListener('input', () => {
});

window.addEventListener('hashchange', () => {
  if(location.hash === '#computer'){
    setComputerActive();
    loadComputerData();
  } else {
    clearComputerActive();
  }
});

if(location.hash === '#computer'){
  setComputerActive();
  requestAnimationFrame(loadComputerData);
}