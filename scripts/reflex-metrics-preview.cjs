const http=require('http');
const fs=require('fs');
const path=require('path');
const {spawn}=require('child_process');

const root=path.resolve(__dirname,'..');
const port=Number(process.env.HIRDA_REFLEX_PREVIEW_PORT||8118);

function send(res,status,body,type='application/json; charset=utf-8'){
  res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store'});
  res.end(body);
}
function json(res,obj,status=200){send(res,status,JSON.stringify(obj), 'application/json; charset=utf-8');}
function sampleMetrics(){
  const now=new Date();
  const iso=(mins)=>new Date(now.getTime()-mins*60000).toISOString();
  return {
    version:'hirda-reflex-metrics-v1',
    window:'24h',
    generated_at:now.toISOString(),
    state:'preview',
    config:{reflex_enabled:true,reflex_mode:'enforce',review_risk_threshold:.72,deny_risk_threshold:.92,min_confidence:.88,teacher_enabled:true,teacher_mode:'shadow',teacher_model:'jev-latest'},
    summary:{decisions:1248,allow:1142,review:94,deny:12,blocked:106,allow_rate:91.51,review_rate:7.53,deny_rate:.96,average_risk:.31,average_confidence:.93},
    teacher:{enabled_decisions:1210,evaluated:1189,availability_rate:98.26,agreement:1126,disagreement:63,agreement_rate:94.7,actions:{allow:1090,review:88,deny:11},errors:[{error:'timeout',count:14},{error:'missing_api_key',count:7}]},
    latency:{policy:{samples:1248,median_ms:.18,p95_ms:.41},reflex:{samples:1248,median_ms:1.8,p95_ms:3.7},teacher:{samples:1189,median_ms:420,p95_ms:780}},
    outcomes:{transport_records:1212,http_success:1200,http_failure:12,http_success_rate:99.01,semantics:'transport_only_not_semantic_correctness'},
    distributions:{
      actions:{allow:1142,review:94,deny:12},
      risk:{low:981,elevated:161,review:94,deny:12},
      compute_lanes:{fast:1030,deep:218},
      permission_classes:{read:0,write:522,execute:714,destructive:12},
      workspaces:{'nova-oracle':602,'mcp-studio':351,'oriverse':187,'earth-616':108},
      signals:[
        {signal:'stateful_git_operation',count:126},
        {signal:'force_flag',count:42},
        {signal:'credential_text',count:31},
        {signal:'privilege_escalation',count:19},
        {signal:'network_write',count:14},
        {signal:'large_argument_surface',count:11}
      ]
    },
    timeline:Array.from({length:12},(_,i)=>({bucket:new Date(now.getTime()-(11-i)*2*3600000).toISOString(),decisions:[74,81,96,90,108,104,112,116,121,119,111,116][i],allow:[69,74,89,82,99,94,101,106,111,109,101,107][i],review:[5,6,6,7,8,9,10,9,9,9,9,8][i],deny:[0,1,1,1,1,1,1,1,1,1,1,1][i]})),
    recent:[
      {recorded_at:iso(1),reflex_id:'preview-1',workspace_key:'nova-oracle',tool:'execute_shell_command',permission_class:'execute',action:'allow',risk:.34,confidence:.89,compute_lane:'fast',blocked:false,signals:[],teacher:{enabled:true,evaluated:true,action:'allow',agreement:true,error:null,model:'jev-latest'},timings:{policy_ms:.17,reflex_ms:1.6,teacher_ms:402}},
      {recorded_at:iso(3),reflex_id:'preview-2',workspace_key:'mcp-studio',tool:'execute_shell_command',permission_class:'execute',action:'review',risk:.88,confidence:.94,compute_lane:'deep',blocked:true,signals:['privilege_escalation'],teacher:{enabled:true,evaluated:true,action:'review',agreement:true,error:null,model:'jev-latest'},timings:{policy_ms:.21,reflex_ms:2.4,teacher_ms:488}},
      {recorded_at:iso(5),reflex_id:'preview-3',workspace_key:'oriverse',tool:'replace_content',permission_class:'write',action:'allow',risk:.24,confidence:.89,compute_lane:'fast',blocked:false,signals:[],teacher:{enabled:true,evaluated:true,action:'allow',agreement:true,error:null,model:'jev-latest'},timings:{policy_ms:.15,reflex_ms:1.2,teacher_ms:391}},
      {recorded_at:iso(8),reflex_id:'preview-4',workspace_key:'earth-616',tool:'execute_shell_command',permission_class:'execute',action:'deny',risk:.96,confidence:.98,compute_lane:'deep',blocked:true,signals:['network_write','credential_text'],teacher:{enabled:true,evaluated:true,action:'review',agreement:false,error:null,model:'jev-latest'},timings:{policy_ms:.22,reflex_ms:2.8,teacher_ms:531}}
    ],
    dataset:{enabled:true,file:'reflex-decisions.jsonl',records:2460,first_recorded_at:iso(1440),last_recorded_at:iso(1)},
    privacy:{raw_arguments_exposed:false,credentials_exposed:false,dataset_path_exposed:false},
    authority:{static_policy_remains_hard_boundary:true,reflex_may_only_narrow_allowed_calls:true,jev_teacher_only:true}
  };
}
function apiStub(url){
  if(url.startsWith('/api/reflex/metrics')) return sampleMetrics();
  if(url==='/api/status') return {studio:{status:'healthy',version:'preview',production_mode:false},servers:[],telemetry:{success_rate:100,per_worker:[]}};
  if(url==='/api/workers') return {workers:[],leases:[]};
  if(url.startsWith('/api/work?')) return {items:[],work:[]};
  if(url==='/api/sessions') return {sessions:[]};
  if(url==='/api/gateway/sessions') return {sessions:[]};
  if(url==='/api/managed/sessions') return {sessions:[]};
  if(url==='/api/managed/workspaces') return {workspaces:[]};
  if(url==='/api/openai/compatibility') return {summary:{observed:0,openai_like:0},observations:[],connector_reclaim_enabled:false};
  if(url==='/api/operations') return {};
  if(url==='/api/observability') return {overall_state:'preview'};
  if(url.startsWith('/api/alerts')) return {alerts:[]};
  if(url.startsWith('/api/audit')) return {items:[],audit:[]};
  if(url.startsWith('/api/events')) return {events:[]};
  if(url.startsWith('/api/capsules')) return {capsules:[],summary:{}};
  if(url.startsWith('/api/agents/runtimes')) return {runtimes:[],summary:{}};
  return null;
}
function serveStatic(req,res){
  let rel=req.url.replace(/^\/static\//,'');
  rel=rel.split('?')[0];
  const full=path.resolve(root,'static',rel);
  if(!full.startsWith(path.resolve(root,'static')+path.sep)) return send(res,403,'forbidden','text/plain');
  fs.readFile(full,(err,data)=>{
    if(err)return send(res,404,'not found','text/plain');
    const ext=path.extname(full);
    const types={'.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.webp':'image/webp','.png':'image/png','.json':'application/json; charset=utf-8'};
    send(res,200,data,types[ext]||'application/octet-stream');
  });
}
function runChild(){
  const server=http.createServer((req,res)=>{
    const url=req.url||'/';
    if(url.startsWith('/static/'))return serveStatic(req,res);
    const stub=apiStub(url);
    if(stub)return json(res,stub);
    if(url==='/'||url.startsWith('/#')||url.startsWith('/?')){
      fs.readFile(path.join(root,'templates','index.html'),'utf8',(err,html)=>{
        if(err)return send(res,500,String(err),'text/plain');
        html=html.replace('</body>',`<div style="position:fixed;right:14px;bottom:14px;z-index:9999;padding:7px 10px;border-radius:999px;background:#16253f;color:white;font:600 11px Kanit, sans-serif;box-shadow:0 5px 20px #0003">REFLEX METRICS · WORKTREE PREVIEW</div></body>`);
        send(res,200,html,'text/html; charset=utf-8');
      });
      return;
    }
    send(res,404,'not found','text/plain');
  });
  server.on('error',err=>{process.exitCode=1;});
  server.listen(port,'0.0.0.0');
}
if(process.argv.includes('--child')){runChild();}
else{
  const child=spawn(process.execPath,[__filename,'--child'],{cwd:root,detached:true,stdio:'ignore',env:{...process.env,HIRDA_REFLEX_PREVIEW_PORT:String(port)}});
  child.unref();
  process.stdout.write(String(port));
}
