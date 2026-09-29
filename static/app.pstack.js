let pstackSelectedTraceId=null;

function pstackTime(value){
  if(!value)return '—';
  const date=new Date(value);
  return Number.isNaN(date.getTime())?String(value):date.toLocaleTimeString();
}

function pstackTraceStatus(status){
  const normalized=String(status||'idle').toLowerCase();
  if(normalized==='ready_for_hirda')return ['READY FOR HIRDA','ready'];
  if(normalized==='completed')return ['COMPLETED','passed'];
  if(normalized==='failed')return ['FAILED','failed'];
  if(normalized==='blocked')return ['BLOCKED','failed'];
  if(normalized==='executing')return ['HIRDA EXECUTING','running'];
  if(normalized==='planned')return ['HIRDA PLANNED','ready'];
  if(normalized==='active')return ['ACTIVE','running'];
  if(normalized==='running')return ['RUNNING','running'];
  return [normalized.toUpperCase(),'idle'];
}

function pstackEventHtml(event){
  const status=String(event.status||'idle').toLowerCase();
  const label=(event.actor||'pstack')+' · '+(event.role||event.tool||'workflow');
  return '<div class="pstack-timeline-row '+esc(status)+'">'
    +'<div class="pstack-timeline-rail"><i></i></div>'
    +'<div class="pstack-timeline-content">'
    +'<div><strong>'+esc(event.title||event.phase||'Pstack event')+'</strong><span>'+esc(pstackTime(event.timestamp))+'</span></div>'
    +'<p>'+esc(event.detail||'')+'</p>'
    +'<small>'+esc(event.workflow||'no workflow')+' · '+esc(label)+' · #'+esc(event.sequence||0)+'</small>'
    +'</div></div>';
}

function pstackTaskRow(trace, selected){
  const state=pstackTraceStatus(trace.status);
  const identity=trace.identity||{};
  const workspace=identity.workspace_key||'—';
  return '<button class="pstack-task-row '+(selected?'selected':'')+'" type="button" data-pstack-trace="'+esc(trace.trace_id||'')+'">'
    +'<span class="pstack-task-dot '+esc(state[1])+'"></span>'
    +'<span class="pstack-task-main"><strong>'+esc(trace.goal||trace.task_id||trace.trace_id||'Untitled task')+'</strong>'
    +'<small>'+esc(workspace)+' · '+esc(trace.workflow||'unclassified')+' · '+esc(pstackTime(trace.updated_at||trace.started_at))+'</small></span>'
    +'<span class="pstack-state '+esc(state[1])+'">'+esc(state[0])+'</span>'
    +'</button>';
}

function pstackFlowSteps(trace, events){
  const phases=new Set(events.map(event=>String(event.phase||'').toLowerCase()));
  const actors=new Set(events.map(event=>String(event.actor||'').toLowerCase()));
  const failed=events.some(event=>String(event.status||'').toLowerCase()==='failed')
    ||String(trace&&trace.status||'').toLowerCase()==='failed';
  const completed=String(trace&&trace.status||'').toLowerCase()==='completed';
  const pstackDone=phases.has('handoff_ready');
  const pstackSeen=actors.has('pstack');
  const jevSeen=actors.has('jev')||[...phases].some(phase=>phase.includes('jev'));
  const hirdaDone=phases.has('hirda_execution_started')
    ||phases.has('hirda_execution_completed');
  const hirdaSeen=actors.has('hirda');
  const toolStarted=phases.has('tool_started');
  const toolDone=phases.has('tool_completed')||phases.has('hirda_execution_completed');
  const downstreamSeen=hirdaSeen||hirdaDone||toolStarted||toolDone;
  const verifySeen=[...phases].some(phase=>
    phase.includes('verify')
    ||phase.includes('verification')
    ||phase.includes('test_passed')
    ||phase.includes('tests_passed')
  );

  const lifecycle=(trace&&trace.lifecycle)||{};
  const lifecycleStages=Array.isArray(lifecycle.stages)?lifecycle.stages:[];
  if(lifecycleStages.length){
    const lifecycleSteps=lifecycleStages.map(stage=>{
      const status=String(stage&&stage.status||'pending').toLowerCase();
      return {
        label:String(stage&&stage.phase||'phase').replace(/(^|_)([a-z])/g,(_,space,ch)=>ch.toUpperCase()),
        state:status==='passed'?'done':status==='failed'?'failed':status==='active'?'active':'waiting',
      };
    });
    return [
      {label:'Task',state:trace?'done':'waiting'},
      {label:'Pstack',state:pstackDone?'done':(pstackSeen?'active':(downstreamSeen?'skipped':'waiting'))},
      ...lifecycleSteps,
    ];
  }

  const steps=[
    {label:'Task',state:trace?'done':'waiting'},
    {label:'Pstack',state:pstackDone?'done':(pstackSeen?'active':(downstreamSeen?'skipped':'waiting'))},
    {label:'JEV',state:jevSeen?'done':(downstreamSeen?'skipped':'waiting')},
    {label:'HIRDA',state:hirdaDone?'done':(hirdaSeen?'active':'waiting')},
    {label:'Runtime / Tools',state:toolDone?'done':(toolStarted?'active':'waiting')},
    {label:'Verify',state:verifySeen?'done':(completed?'done':'waiting')},
    {label:'Done',state:completed?'done':'waiting'},
  ];
  if(failed){
    const activeIndex=[...steps].reverse().findIndex(step=>step.state!=='waiting');
    const index=activeIndex<0?0:steps.length-1-activeIndex;
    steps[index]={...steps[index],state:'failed'};
  }
  return steps;
}

function pstackFlowHtml(trace, events){
  const steps=pstackFlowSteps(trace,events);
  return '<article class="panel-card pstack-flow-card">'
    +'<div class="panel-head"><div><span class="eyebrow">HIRDA STACK FLOW</span><h3>Task flow overview</h3></div><small>selected task</small></div>'
    +'<div class="pstack-flow">'
    +steps.map((step,index)=>'<div class="pstack-flow-step '+esc(step.state)+'">'
      +'<span class="pstack-flow-node">'+(step.state==='done'?'✓':step.state==='failed'?'!':step.state==='skipped'?'—':'')+'</span>'
      +'<strong>'+esc(step.label)+'</strong>'
      +(index<steps.length-1?'<i></i>':'')
      +'</div>').join('')
    +'</div></article>';
}

function renderPstackActivity(snapshot){
  const root=document.getElementById('pstackActivityPanel');
  if(!root)return;
  pstackActivityData=snapshot||null;
  if(!snapshot||!snapshot.available){
    root.innerHTML='<div class="panel-card"><div class="empty">Pstack activity observability is unavailable.</div></div>';
    return;
  }

  const traces=snapshot.traces||[];
  if(pstackSelectedTraceId&&!traces.some(item=>item.trace_id===pstackSelectedTraceId)){
    pstackSelectedTraceId=null;
  }
  const trace=traces.find(item=>item.trace_id===pstackSelectedTraceId)
    ||snapshot.active_trace
    ||traces[0]
    ||null;
  if(trace&&!pstackSelectedTraceId)pstackSelectedTraceId=trace.trace_id;

  const events=snapshot.events||[];
  const traceEvents=trace?events.filter(event=>event.trace_id===trace.trace_id):[];
  const state=pstackTraceStatus(trace&&trace.status);
  const statusLabel=state[0];
  const statusClass=state[1];
  const totalByTool={route_task:4,load_workflow:2,build_role_request:2};
  const methodologyEvents=traceEvents.filter(event=>String(event.actor||'').toLowerCase()==='pstack');
  const total=totalByTool[trace&&trace.tool]||Math.max(1,methodologyEvents.length);
  const done=methodologyEvents.filter(event=>['passed','ready'].includes(String(event.status||'').toLowerCase())).length;
  const terminal=trace&&['ready_for_hirda','completed'].includes(String(trace.status||'').toLowerCase());
  const progress=terminal?100:Math.max(0,Math.min(100,Math.round((done/total)*100)));
  const identity=trace&&trace.identity||{};
  const identityText=Object.entries(identity).map(([key,value])=>key+': '+value).join(' · ')||'No external task identity';
  const workflow=trace&&trace.workflow||'—';
  const role=trace&&trace.role||'—';
  const recent=[...traceEvents].reverse();

  let currentBody='<div class="empty">No Pstack task has been observed yet.</div>';
  if(trace){
    currentBody='<div class="pstack-current-body">'
      +'<div class="pstack-goal"><span>Task</span><p>'+esc(trace.goal||'—')+'</p></div>'
      +'<div class="pstack-progress-meta"><strong>'+esc(progress)+'%</strong><span>'+esc(done)+' / '+esc(total)+' Pstack methodology stages</span></div>'
      +'<div class="pstack-progress"><i style="width:'+progress+'%"></i></div>'
      +'<div class="pstack-current-facts">'
      +'<span><small>Workspace</small><strong>'+esc(identity.workspace_key||'—')+'</strong></span>'
      +'<span><small>Workflow</small><strong>'+esc(workflow)+'</strong></span>'
      +'<span><small>Role</small><strong>'+esc(role)+'</strong></span>'
      +'<span><small>Started</small><strong>'+esc(pstackTime(trace.started_at))+'</strong></span>'
      +'<span><small>Task</small><code>'+esc(trace.task_id||identity.task_id||'—')+'</code></span>'
      +'<span><small>Trace</small><code>'+esc(trace.trace_id||'—')+'</code></span>'
      +'</div><small class="pstack-identity">'+esc(identityText)+'</small></div>';
  }

  root.innerHTML=pstackFlowHtml(trace,traceEvents)
    +'<div class="pstack-summary-grid">'
    +'<article class="pstack-summary-card"><span>Selected workflow</span><strong>'+esc(workflow)+'</strong><small>'+esc(trace&&trace.tool||'waiting for activity')+'</small></article>'
    +'<article class="pstack-summary-card"><span>Selected state</span><strong class="pstack-state '+esc(statusClass)+'">'+esc(statusLabel)+'</strong><small>'+esc(trace&&trace.current_step&&trace.current_step.title||'No active step')+'</small></article>'
    +'<article class="pstack-summary-card"><span>Task ledger</span><strong>'+esc(snapshot.trace_count==null?traces.length:snapshot.trace_count)+'</strong><small>persistent task traces</small></article>'
    +'<article class="pstack-summary-card"><span>Trace events</span><strong>'+esc(snapshot.event_count||0)+'</strong><small>'+esc(snapshot.persistent?'persistent Event Ledger':'process memory')+'</small></article>'
    +'</div>'
    +'<article class="panel-card pstack-ledger-card">'
    +'<div class="panel-head"><div><span class="eyebrow">TASK LEDGER</span><h3>Recent Pstack tasks</h3></div><small>'+esc(traces.length)+' shown</small></div>'
    +'<div class="pstack-task-list">'+(traces.length?traces.map(item=>pstackTaskRow(item,trace&&item.trace_id===trace.trace_id)).join(''):'<div class="empty">No task envelopes recorded yet.</div>')+'</div>'
    +'</article>'
    +'<div class="pstack-layout">'
    +'<article class="panel-card pstack-current-card">'
    +'<div class="panel-head"><div><span class="eyebrow">SELECTED TRACE</span><h3>'+esc(trace&&trace.current_step&&trace.current_step.title||'Waiting for Pstack activity')+'</h3></div><span class="pstack-state '+esc(statusClass)+'">'+esc(statusLabel)+'</span></div>'
    +currentBody+'</article>'
    +'<article class="panel-card pstack-timeline-card">'
    +'<div class="panel-head"><div><span class="eyebrow">TRACE TIMELINE</span><h3>Methodology + HIRDA tools</h3></div><small>'+esc(traceEvents.length)+' events</small></div>'
    +'<div class="pstack-timeline">'+(recent.length?recent.map(pstackEventHtml).join(''):'<div class="empty">This task has no timeline events yet.</div>')+'</div>'
    +'</article></div>';
  captureAndTranslateText(root);
}

async function refreshPstackActivity(){
  const snapshot=await getJson('/api/pstack/activity?limit=500');
  renderPstackActivity(snapshot);
}

function stopPstackActivityStream(){
  if(pstackActivitySource){
    pstackActivitySource.close();
    pstackActivitySource=null;
  }
}

function startPstackActivityStream(){
  if(typeof EventSource==='undefined'||currentView!=='pstack'||pstackActivitySource)return;
  const source=new EventSource('/api/pstack/activity/stream');
  pstackActivitySource=source;
  source.addEventListener('snapshot',event=>{
    try{renderPstackActivity(JSON.parse(event.data));}catch{}
  });
  source.onerror=()=>{
    const chip=document.querySelector('.pstack-live-chip');
    if(chip)chip.classList.add('reconnecting');
  };
  source.onopen=()=>{
    const chip=document.querySelector('.pstack-live-chip');
    if(chip)chip.classList.remove('reconnecting');
  };
}

document.getElementById('pstackActivityPanel')?.addEventListener('click',event=>{
  const row=event.target.closest('[data-pstack-trace]');
  if(!row)return;
  pstackSelectedTraceId=row.dataset.pstackTrace||null;
  if(pstackActivityData)renderPstackActivity(pstackActivityData);
});

document.getElementById('pstackRefreshBtn')?.addEventListener('click',()=>{
  uiAction(async()=>{
    const button=document.getElementById('pstackRefreshBtn');
    button.disabled=true;
    button.textContent='Refreshing…';
    try{await refreshPstackActivity();}
    finally{button.disabled=false;button.textContent='Refresh trace';}
  });
});
