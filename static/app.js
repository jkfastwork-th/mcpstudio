let currentView = (location.hash || '#home').slice(1);
let sessionTunnelFilter = '';
let showSessionHistory = false;
let showManagedHistory = false;
let reflexWindow = '24h';
let actionProviders = [];
let currentActionProvider = null;
let latestActionJudgment = null;
let latestData = null;


const SUPPORTED_LANGUAGES=['en','th'];
const I18N_TH={
  'Production':'โปรดักชัน','Live':'ใช้งานจริง','All ingress through HIRDA Gateway':'ทุกช่องทางเข้าใช้งานผ่าน HIRDA Gateway',
  'Home':'หน้าหลัก','Sessions':'เซสชัน','Workspaces':'เวิร์กสเปซ','Guide':'คู่มือ','System':'ระบบ',
  'Production isolated':'แยกโปรดักชันแล้ว','Only what matters right now.':'แสดงเฉพาะสิ่งที่สำคัญตอนนี้',
  'Waiting…':'กำลังรอ…','Light':'สว่าง','Dark':'มืด','Refresh':'รีเฟรช','UNKNOWN':'ไม่ทราบ',
  'ACTIVE NOW':'กำลังใช้งาน','Project sessions':'เซสชันโปรเจกต์','Every project here has its own pinned Serena instance.':'แต่ละโปรเจกต์มี Serena instance ที่ถูก pin แยกของตัวเอง',
  'Manage sessions':'จัดการเซสชัน','INGRESS':'ช่องทางเข้า','Connections':'การเชื่อมต่อ','ATTENTION':'ต้องตรวจสอบ','Needs attention':'รายการที่ต้องตรวจสอบ',
  'PROJECT SESSIONS':'เซสชันโปรเจกต์','Create one session per project. Project pinning and Serena isolation are automatic.':'สร้างหนึ่งเซสชันต่อหนึ่งโปรเจกต์ ระบบจะ pin โปรเจกต์และแยก Serena ให้อัตโนมัติ',
  'Show stopped':'แสดงที่หยุดแล้ว','Hide stopped':'ซ่อนที่หยุดแล้ว','+ New session':'+ เซสชันใหม่','Session name':'ชื่อเซสชัน','Project / workspace':'โปรเจกต์ / เวิร์กสเปซ',
  'Create session':'สร้างเซสชัน','Cancel':'ยกเลิก','Client transport details':'รายละเอียด client transport','Client transports':'Client transports','Normally you do not need to manage these manually.':'ปกติไม่จำเป็นต้องจัดการส่วนนี้ด้วยตนเอง',
  'All ingress':'ทุกช่องทางเข้า','Show history':'แสดงประวัติ','Hide history':'ซ่อนประวัติ','PROJECT REGISTRY':'ทะเบียนโปรเจกต์','Register the projects that HIRDA is allowed to isolate and manage.':'ลงทะเบียนโปรเจกต์ที่ HIRDA ได้รับอนุญาตให้แยกและจัดการ',
  '+ Register workspace':'+ ลงทะเบียนเวิร์กสเปซ','APPROVED':'อนุมัติแล้ว','Workspace registry':'ทะเบียนเวิร์กสเปซ','ACTIVE':'ใช้งาน','Workspace ownership':'ผู้ถือครองเวิร์กสเปซ','WRITE SAFETY':'ความปลอดภัยการเขียน','Active write leases':'write lease ที่ใช้งานอยู่',
  'HOW TO USE':'วิธีใช้งาน','The shortest path from a project folder to a safe ChatGPT coding session.':'ขั้นตอนสั้นที่สุดจากโฟลเดอร์โปรเจกต์ไปสู่ ChatGPT coding session ที่ปลอดภัย',
  'START HERE':'เริ่มตรงนี้','Three steps for normal use':'3 ขั้นตอนสำหรับการใช้งานทั่วไป','You normally only need Workspaces and Sessions. HIRDA handles project pinning, Serena isolation, ingress attribution and reconnects automatically.':'โดยทั่วไปใช้แค่ Workspaces และ Sessions ส่วน project pinning, Serena isolation, ingress attribution และ reconnect ให้ HIRDA จัดการอัตโนมัติ',
  'Register the project':'ลงทะเบียนโปรเจกต์','Add the project folder once in Workspaces.':'เพิ่มโฟลเดอร์โปรเจกต์ครั้งเดียวใน Workspaces','Open Workspaces →':'เปิด Workspaces →','Create a session':'สร้างเซสชัน','Create one isolated session for that workspace. Its project is pinned.':'สร้าง isolated session หนึ่งตัวให้เวิร์กสเปซนั้น โปรเจกต์จะถูก pin ไว้','Open Sessions →':'เปิด Sessions →',
  'Use it from ChatGPT':'ใช้งานจาก ChatGPT','Choose/use that managed session. Reconnects stay attached to the pinned project.':'เลือก managed session นั้น เมื่อ reconnect จะยังผูกกับโปรเจกต์ที่ pin ไว้','Check active sessions →':'ดูเซสชันที่กำลังใช้งาน →',
  'A session is your safe unit of work. One session → one project → one isolated Serena process.':'เซสชันคือหน่วยงานที่ปลอดภัย: 1 เซสชัน → 1 โปรเจกต์ → 1 Serena process ที่แยกออกมา',
  'Use when':'ใช้เมื่อ','Starting work on Nova, Earth-616, Oriverse, or any other project.':'เริ่มทำงานกับ Nova, Earth-616, Oriverse หรือโปรเจกต์อื่น','Do not switch project inside Serena. Create or select another managed session instead.':'อย่าสลับโปรเจกต์ภายใน Serena ให้สร้างหรือเลือก managed session อื่นแทน',
  'A workspace is an approved project folder that HIRDA is allowed to manage.':'Workspace คือโฟลเดอร์โปรเจกต์ที่อนุมัติให้ HIRDA จัดการ','Register once':'ลงทะเบียนครั้งเดียว','Give it a short key such as':'กำหนด key สั้น ๆ เช่น','and an absolute project path.':'และ absolute project path','Workspace ownership is affinity. WRITE ACTIVE is the exclusive write lock.':'Workspace ownership คือ affinity ส่วน WRITE ACTIVE คือ write lock แบบ exclusive',
  'Ingress':'ช่องทางเข้า','OpenAI tunnel and Cloudflare both enter through HIRDA before reaching a managed session.':'ทั้ง OpenAI tunnel และ Cloudflare ต้องผ่าน HIRDA ก่อนถึง managed session','Normal state':'สถานะปกติ','Ingress is healthy and the client transport is bound to a managed session.':'ช่องทางเข้า healthy และ client transport ผูกกับ managed session แล้ว','An “Unbound client” needs a managed session before coding tools are allowed.':'“Unbound client” ต้องเลือก managed session ก่อนจึงจะใช้ coding tools ได้',
  'Open System only when Home shows an alert, unhealthy ingress, or a session error.':'เปิด System เมื่อหน้า Home มี alert, ingress ผิดปกติ หรือ session error','First checks':'ตรวจสอบก่อน','Look at Alerts & SLO, then Tunnels. Expand diagnostics only if needed.':'ดู Alerts & SLO แล้วค่อยดู Tunnels และเปิด diagnostics เมื่อจำเป็นเท่านั้น','Advanced diagnostics are operational details, not part of the normal workflow.':'Advanced diagnostics เป็นรายละเอียดเชิงระบบ ไม่ใช่ขั้นตอนใช้งานปกติ',
  'STATUS CHEAT SHEET':'ความหมายสถานะ','What the labels mean':'ความหมายของป้ายสถานะ','Session is running and available.':'เซสชันกำลังรันและพร้อมใช้งาน','IDLE':'ว่าง','Session is healthy but not currently busy.':'เซสชันปกติแต่ไม่ได้กำลังทำงาน','STOPPED':'หยุดแล้ว','Session is preserved and can be resumed.':'เซสชันยังถูกเก็บไว้และ resume ได้','WRITE ACTIVE':'กำลังล็อกเขียน','Exclusive write lease is currently held.':'กำลังถือ exclusive write lease','UNBOUND':'ยังไม่ผูก','Client reached HIRDA but has not selected a managed session.':'Client ถึง HIRDA แล้วแต่ยังไม่ได้เลือก managed session','ERROR':'ผิดพลาด','Open System and inspect the alert or session detail.':'เปิด System แล้วตรวจ alert หรือรายละเอียด session',
  'Hover help is available throughout the UI.':'มีคำอธิบายเมื่อวางเมาส์ทั่วทั้ง UI','Move the pointer over a ? icon, important button, status badge, or navigation item. Keyboard users can focus the same controls to show the explanation.':'วางเมาส์เหนือไอคอน ?, ปุ่มสำคัญ, ป้ายสถานะ หรือเมนูเพื่อดูคำอธิบาย ผู้ใช้คีย์บอร์ดสามารถ focus ที่จุดเดียวกันได้',
  'SYSTEM':'ระบบ','System health':'สุขภาพระบบ','Use this page only when something needs investigation.':'ใช้หน้านี้เมื่อจำเป็นต้องตรวจสอบปัญหา','Poll health':'ตรวจสุขภาพ','Refresh Herdr':'รีเฟรช Herdr','Tunnels':'Tunnels','RELIABILITY':'ความเสถียร','Alerts & SLO':'Alerts & SLO','Advanced diagnostics':'วิเคราะห์ขั้นสูง','Workers & work queue':'Workers และคิวงาน','Operations':'การปฏิบัติการ','Activity & audit':'กิจกรรมและ Audit','Execution':'การทำงาน','Servers':'เซิร์ฟเวอร์','Telemetry':'Telemetry','OpenAI compatibility':'ความเข้ากันได้กับ OpenAI',
  'Refreshing…':'กำลังรีเฟรช…','Polling…':'กำลังตรวจ…','Confirm action':'ยืนยันการทำงาน','Continue':'ดำเนินการต่อ','Close':'ปิด','Action failed':'ดำเนินการไม่สำเร็จ','Rename session':'เปลี่ยนชื่อเซสชัน','Choose a clear name for this pinned project session.':'ตั้งชื่อที่เข้าใจง่ายให้เซสชันโปรเจกต์นี้','Rename':'เปลี่ยนชื่อ','Stop isolated Serena session?':'หยุด Serena session ที่แยกไว้หรือไม่?','The pinned project stays registered and can be resumed later. Connected clients must be detached first.':'โปรเจกต์ที่ pin ไว้ยังคงลงทะเบียนอยู่และ resume ได้ภายหลัง ต้อง detach client ที่เชื่อมต่ออยู่ก่อน','Stop session':'หยุดเซสชัน','Choose a project session':'เลือกโปรเจกต์เซสชัน','Select a managed project session before attaching this client transport.':'เลือก managed project session ก่อนผูก client transport นี้','Register workspace':'ลงทะเบียนเวิร์กสเปซ','Only approved workspaces can receive isolated project sessions.':'เฉพาะ workspace ที่อนุมัติแล้วเท่านั้นที่สร้าง isolated project session ได้','Register':'ลงทะเบียน','Workspace key':'Workspace key','Absolute project path':'Absolute project path','Display name':'ชื่อที่แสดง',
  'Technical details':'รายละเอียดทางเทคนิค','Everything is running normally':'ระบบทำงานปกติ','System needs attention':'ระบบต้องตรวจสอบ','No action needed':'ไม่ต้องดำเนินการ','No running project sessions. Create one when you need a project.':'ยังไม่มี project session ที่กำลังรัน สร้างใหม่เมื่อเริ่มทำงานกับโปรเจกต์','No ingress configured.':'ยังไม่ได้กำหนด ingress','Nothing needs attention.':'ไม่มีสิ่งที่ต้องดำเนินการ','No approved workspaces registered.':'ยังไม่มี workspace ที่ลงทะเบียน','No managed sessions in this view.':'ไม่มี managed session ในมุมมองนี้','Managed session isolation is disabled in config.':'Managed session isolation ถูกปิดใน config','Register a workspace first':'ลงทะเบียน workspace ก่อน',
  'ACTIVE':'ใช้งาน','HEALTHY':'ปกติ','DOWN':'ล่ม','DEGRADED':'ผิดปกติ','BUSY':'กำลังทำงาน','RUNNING':'กำลังรัน','READY':'พร้อม','CONNECTED':'เชื่อมต่อ','STALE':'หมดอายุ','CLOSED':'ปิดแล้ว','UNKNOWN':'ไม่ทราบ','FAILED':'ล้มเหลว','COMPLETED':'เสร็จแล้ว','RECONNECTING':'กำลังเชื่อมต่อใหม่','DETACHED':'แยกออกแล้ว',
  'Language':'ภาษา','Change interface language. Your choice is saved in this browser.':'เปลี่ยนภาษาของหน้าจอ ระบบจะจำค่าที่เลือกไว้ในเบราว์เซอร์','Interface language':'ภาษาหน้าจอ','Reset text size to the default 112%.':'คืนขนาดตัวอักษรเป็นค่าเริ่มต้น 112%','Adjust UI text size. This setting is saved in your browser.':'ปรับขนาดตัวอักษรของ UI ระบบจะจำค่าไว้ในเบราว์เซอร์','Decrease text size.':'ลดขนาดตัวอักษร','Increase text size.':'เพิ่มขนาดตัวอักษร','Switch between Light and Dark. Your choice is saved in this browser.':'สลับธีมสว่าง/มืด ระบบจะจำค่าที่เลือกไว้','Use the light Google-inspired color theme.':'ใช้ธีมสว่างโทน Google','Use the dark Google-inspired color theme.':'ใช้ธีมมืดโทน Google','Reload current HIRDA status from the server.':'โหลดสถานะ HIRDA ล่าสุดจากเซิร์ฟเวอร์','Overall HIRDA health. HEALTHY means the control plane is responding normally.':'สุขภาพโดยรวมของ HIRDA; HEALTHY หมายถึง control plane ตอบสนองปกติ',
  'Use HIRDA mainly from ChatGPT. HIRDA Studio is your control panel for sessions, workspaces and system health.':'ใช้งาน HIRDA ผ่าน ChatGPT เป็นหลัก ส่วน HIRDA Studio ใช้ควบคุมเซสชัน เวิร์กสเปซ และสุขภาพระบบ',
  'START HERE · CHATGPT':'เริ่มตรงนี้ · CHATGPT','Work from ChatGPT first':'เริ่มทำงานจาก ChatGPT ก่อน',
  'Start by choosing a workspace in ChatGPT. HIRDA will find or resume its managed session, keep the project pinned, and route you to the isolated Serena instance.':'เริ่มจากเลือก workspace ใน ChatGPT แล้ว HIRDA จะค้นหาหรือ resume managed session, pin โปรเจกต์ และส่งงานไปยัง Serena instance ที่แยกไว้',
  'Try this in ChatGPT':'ลองคำสั่งนี้ใน ChatGPT','Copy command':'คัดลอกคำสั่ง','Copied ✓':'คัดลอกแล้ว ✓','Copy ChatGPT command':'คัดลอกคำสั่ง ChatGPT',
  'Copy this example command and paste it into ChatGPT.':'คัดลอกคำสั่งตัวอย่างนี้แล้วนำไปวางใน ChatGPT',
  'After the workspace is selected, continue talking naturally. You do not need to call':'หลังเลือก workspace แล้ว สามารถคุยต่อแบบปกติได้ ไม่ต้องเรียก','yourself.':'ด้วยตัวเอง',
  'Your working surface':'พื้นที่ทำงานหลัก','Session routing':'จัดเส้นทางเซสชัน','Project Pin 🔒':'Project Pin 🔒','Isolated workspace':'เวิร์กสเปซที่แยกออกมา',
  'Typical ChatGPT workflow':'ตัวอย่าง workflow ใน ChatGPT','Once a workspace is selected, keep working in the same conversation with normal language.':'หลังเลือก workspace แล้ว ให้ทำงานต่อในบทสนทนาเดิมด้วยภาษาปกติ',
  'Switch workspace':'สลับ workspace','Tell ChatGPT which workspace you want. HIRDA switches to that workspace\'s managed session instead of changing project inside a shared Serena process.':'บอก ChatGPT ว่าต้องการ workspace ไหน HIRDA จะสลับไป managed session ของ workspace นั้นแทนการเปลี่ยนโปรเจกต์ภายใน Serena process ร่วม',
  'Example':'ตัวอย่าง','One workspace → one managed session → one isolated Serena instance. This is what prevents active-project cross-talk.':'หนึ่ง workspace → หนึ่ง managed session → หนึ่ง Serena instance ที่แยกออกมา ช่วยป้องกัน active project ชนกัน',
  'MENTAL MODEL':'แนวคิดหลัก','ChatGPT is where you work. HIRDA Studio is where you control the system.':'ChatGPT คือที่ทำงาน ส่วน HIRDA Studio คือที่ควบคุมระบบ',
  'Use ChatGPT for':'ใช้ ChatGPT สำหรับ','Work with code':'ทำงานกับโค้ด','Read or send pane messages':'อ่านหรือส่งข้อความ pane','Run tests':'รัน tests','Fix bugs':'แก้บั๊ก','Review and commit changes':'ตรวจและ commit การเปลี่ยนแปลง',
  'Use HIRDA Studio for':'ใช้ HIRDA Studio สำหรับ','See active sessions':'ดูเซสชันที่กำลังใช้งาน','Register or inspect workspaces':'ลงทะเบียนหรือตรวจ workspace','Check ingress and client binding':'ตรวจ ingress และ client binding','Check isolated Serena instances':'ตรวจ Serena instance ที่แยกไว้','Investigate errors or conflicts':'ตรวจ error หรือ conflict',
  'Admin setup · register a new project':'Admin setup · ลงทะเบียนโปรเจกต์ใหม่','ONE-TIME SETUP':'ตั้งค่าครั้งเดียว','Only needed for a new project':'ใช้เฉพาะตอนเพิ่มโปรเจกต์ใหม่','Normal daily use starts in ChatGPT. Use these steps only when a project has never been registered before.':'การใช้งานประจำวันเริ่มที่ ChatGPT ขั้นตอนนี้ใช้เฉพาะโปรเจกต์ที่ยังไม่เคยลงทะเบียน',
  'Return to ChatGPT':'กลับไปที่ ChatGPT','Select the workspace from ChatGPT and continue working there.':'เลือก workspace จาก ChatGPT แล้วทำงานต่อที่นั่น',
  'Starting or resuming work on Nova, Earth-616, Oriverse, or another project.':'เริ่มหรือกลับมาทำงานต่อใน Nova, Earth-616, Oriverse หรือโปรเจกต์อื่น',
  'Do not switch project inside Serena. Select another workspace/session through HIRDA instead.':'อย่าสลับโปรเจกต์ภายใน Serena ให้เลือก workspace/session อื่นผ่าน HIRDA แทน',
  'Use the “Copy ChatGPT command” button in Workspaces when you want the exact command for a project.':'ใช้ปุ่ม “คัดลอกคำสั่ง ChatGPT” ในหน้า Workspaces เมื่อต้องการคำสั่งของโปรเจกต์นั้นโดยตรง',
  'Start here to learn how to use HIRDA from ChatGPT, then see setup and troubleshooting guidance.':'เริ่มตรงนี้เพื่อเรียนรู้การใช้ HIRDA จาก ChatGPT แล้วค่อยดูการตั้งค่าและแก้ปัญหา',
  'Copy the workspace selection command for ChatGPT.':'คัดลอกคำสั่งเลือก workspace สำหรับใช้ใน ChatGPT.',
  'Core services are responding normally.':'บริการหลักตอบสนองตามปกติ',
  'Open System to investigate the active issue.':'เปิดหน้าระบบเพื่อตรวจสอบปัญหาที่กำลังเกิดขึ้น',
  'Daily use first, then setup and troubleshooting.':'เริ่มจากการใช้งานประจำวัน แล้วค่อยดูการตั้งค่าและแก้ปัญหา',
  'Quick start for daily work, plus setup and troubleshooting when you need it.':'เริ่มใช้งานประจำวันอย่างรวดเร็ว พร้อมการตั้งค่าและแก้ปัญหาเมื่อต้องการ',
  'Keep working naturally':'ทำงานต่อได้ตามธรรมชาติ',
  'After selecting a workspace, stay in the same ChatGPT conversation and ask for the next task normally.':'หลังเลือก workspace แล้ว ใช้บทสนทนา ChatGPT เดิมและสั่งงานถัดไปได้ตามปกติ',
  'Tell ChatGPT the workspace name. HIRDA moves you to that workspace\'s managed session without changing project inside a shared Serena process.':'บอกชื่อ workspace กับ ChatGPT แล้ว HIRDA จะพาไปยัง managed session ของ workspace นั้นโดยไม่สลับโปรเจกต์ใน Serena ที่แชร์ร่วมกัน',
  'Each workspace keeps its own managed session and isolated Serena instance.':'แต่ละ workspace มี managed session และ Serena instance แยกของตัวเอง',
  'DAILY USE':'ใช้งานประจำวัน',
  'ChatGPT for work · HIRDA Studio for control':'ChatGPT สำหรับทำงาน · HIRDA Studio สำหรับควบคุมระบบ',
  'See or resume sessions':'ดูหรือ resume เซสชัน',
  'Register workspaces':'ลงทะเบียน workspace',
  'Inspect alerts or conflicts':'ตรวจ alert หรือ conflict',
  'Only for a new project':'เฉพาะโปรเจกต์ใหม่',
  'Daily work starts in ChatGPT. Do this once when a project has never been registered.':'งานประจำวันเริ่มที่ ChatGPT ขั้นตอนนี้ทำครั้งเดียวเมื่อโปรเจกต์ยังไม่เคยลงทะเบียน',
  'Add its folder in Workspaces.':'เพิ่มโฟลเดอร์ในหน้า Workspaces',
  'Create one isolated session for that workspace.':'สร้าง isolated session หนึ่งตัวสำหรับ workspace นั้น',
  'Select that workspace and continue working.':'เลือก workspace นั้นแล้วทำงานต่อ',
  'Status reference':'อ้างอิงสถานะ',
  'Need context?':'ต้องการคำอธิบาย?',
  'Hover or focus a ? marker and key controls to see a short explanation.':'วางเมาส์หรือโฟกัสที่เครื่องหมาย ? และส่วนควบคุมสำคัญเพื่อดูคำอธิบายสั้น ๆ',
  'Each project session stays pinned to one workspace and uses its own isolated Serena instance.':'แต่ละ project session จะ pin กับ workspace เดียวและใช้ Serena instance ที่แยกของตัวเอง',

  /* Canonical navigation + current page copy */
  'Dashboard':'แดชบอร์ด',
  'Capsule Lanes':'เลนแคปซูล',
  'Agents':'เอเจนต์',
  'Reflex':'รีเฟล็กซ์',
  'Observe HIRDA Reflex decisions, JEV teacher agreement, latency, risk, and dataset growth.':'ดูการตัดสินใจของ HIRDA Reflex ความสอดคล้องกับ JEV teacher เวลาแฝง ความเสี่ยง และการเติบโตของชุดข้อมูล',
  'Inspect HIRDA Reflex decisions, JEV teacher agreement, latency, risk, and dataset growth.':'ตรวจสอบการตัดสินใจของ HIRDA Reflex ความสอดคล้องกับ JEV teacher เวลาแฝง ความเสี่ยง และชุดข้อมูล',
  'DECISION ENGINE':'ระบบตัดสินใจ',
  'Window':'ช่วงเวลา',
  '1 hour':'1 ชั่วโมง',
  '24 hours':'24 ชั่วโมง',
  '7 days':'7 วัน',
  '30 days':'30 วัน',
  'Refresh metrics':'รีเฟรชเมตริก',
  'Loading Reflex metrics…':'กำลังโหลดเมตริก Reflex…',
  'LOCAL AUTHORITY':'ตัวตัดสินภายในเครื่อง',
  'HIRDA Reflex':'HIRDA Reflex',
  'JEV teacher':'JEV teacher',
  'Dataset':'ชุดข้อมูล',
  'Decisions':'การตัดสินใจ',
  'Allow rate':'อัตรา Allow',
  'Review rate':'อัตรา Review',
  'Deny rate':'อัตรา Deny',
  'JEV agreement':'ความสอดคล้องกับ JEV',
  'Reflex median':'Reflex median',
  'JEV median':'JEV median',
  'Transport success':'ความสำเร็จของ transport',
  'Action mix':'สัดส่วนการตัดสินใจ',
  'Risk distribution':'การกระจายความเสี่ยง',
  'Fast vs deep':'Fast เทียบ Deep',
  'JEV comparison':'การเทียบกับ JEV',
  'Decision activity':'กิจกรรมการตัดสินใจ',
  'Top risk signals':'สัญญาณความเสี่ยงหลัก',
  'Recent decisions':'การตัดสินใจล่าสุด',
  'Authority boundary':'ขอบเขตอำนาจ',
  'Computer':'คอมพิวเตอร์',
  'Settings':'การตั้งค่า',
  'Appearance':'รูปลักษณ์',
  'Theme':'ธีม',
  'Text size':'ขนาดตัวอักษร',
  'Color Scheme':'ชุดสี',
  'Default':'ค่าเริ่มต้น',
  'HIRDA Theme':'ธีม HIRDA',
  'Claude Theme':'ธีม Claude',
  'Codex Theme':'ธีม Codex',
  'Hermes Theme':'ธีม Hermes',
  'Owner':'เจ้าของ',
  'Capsule Lane Overview':'ภาพรวมเลนแคปซูล',
  'See where work entered, which agent owns it now, and where the capsule can move next.':'ดูว่างานเข้ามาจากจุดใด อยู่กับเอเจนต์ใด และแคปซูลจะไปขั้นตอนใดต่อ',
  'Agent lanes':'เลนเอเจนต์',
  'Capsule events':'เหตุการณ์แคปซูล',
  'Routing colors':'สีเส้นทาง',
  'reasoning · analysis':'การให้เหตุผล · การวิเคราะห์',
  'code · execution':'โค้ด · การประมวลผล',
  'coordination · handoff':'การประสานงาน · การส่งต่อ',
  'CAPSULE LANES':'เลนแคปซูล',
  'Inspect capsule state, routing, handoffs, and session history.':'ตรวจสอบสถานะแคปซูล เส้นทางการทำงาน การส่งต่องาน และประวัติเซสชัน',
  'AGENT RUNTIMES':'รันไทม์เอเจนต์',
  'Monitor Claude, Codex, and Hermes runtime availability.':'ตรวจสอบความพร้อมใช้งานของรันไทม์ Claude, Codex และ Hermes',
  'Monitor Claude, Codex, and Hermes runtime availability and active capsule load.':'ตรวจสอบความพร้อมใช้งานของรันไทม์ Claude, Codex และ Hermes รวมถึงภาระแคปซูลที่กำลังทำงาน',
  'Monitor runtime availability, authentication health, rate limits, and active capsule load.':'ตรวจสอบความพร้อมใช้งานของรันไทม์ สุขภาพการยืนยันตัวตน ข้อจำกัดอัตราใช้งาน และภาระแคปซูลที่กำลังทำงาน',
  'Manage approved projects, managed sessions, and workspace safety.':'จัดการโปรเจกต์ที่อนุมัติ เซสชันที่ HIRDA ดูแล และความปลอดภัยของเวิร์กสเปซ',
  'SETTINGS':'การตั้งค่า',
  'Appearance and interface preferences.':'รูปลักษณ์และการตั้งค่าหน้าจอ',
  'Configure appearance, language, text size, and color preferences.':'ตั้งค่ารูปลักษณ์ ภาษา ขนาดตัวอักษร และชุดสี',
  'Theme, language, text size, and color scheme.':'ธีม ภาษา ขนาดตัวอักษร และชุดสี',
  'SYSTEM':'ระบบ',
  'Runtime health, tunnels, alerts, SLO and operational diagnostics.':'สถานะรันไทม์ ทันเนล การแจ้งเตือน SLO และการวิเคราะห์ระบบ',
  'Monitor runtime health, ingress, reliability, and diagnostics.':'ตรวจสอบสถานะรันไทม์ ช่องทางเข้า ความเสถียร และการวิเคราะห์ระบบ',
  'Refresh status':'รีเฟรชสถานะ',
  'Tunnels':'ทันเนล',
  'Alerts & SLO':'การแจ้งเตือนและ SLO',
  'Workers & work queue':'เวิร์กเกอร์และคิวงาน',
  'Operations':'การดำเนินงานระบบ',
  'Activity & audit':'กิจกรรมและการตรวจสอบ',
  'Execution':'การประมวลผล',
  'Telemetry':'เทเลเมทรี',
  'COMPUTER USE':'การใช้งานคอมพิวเตอร์',
  'DESKTOPS':'เดสก์ท็อป',
  'Select a session':'เลือกเซสชัน',
  'Shared browser desktop for OAuth, login and consent screens.':'เดสก์ท็อปเบราว์เซอร์ร่วมสำหรับ OAuth การเข้าสู่ระบบ และหน้าจอยินยอม',
  'Open a shared browser desktop for OAuth, sign-in, consent, and other human-in-the-loop actions.':'เปิดเดสก์ท็อปเบราว์เซอร์ร่วมสำหรับ OAuth การเข้าสู่ระบบ การยินยอม และขั้นตอนที่ต้องมีผู้ใช้ดำเนินการ',
  'Approved project folders, managed sessions and workspace safety.':'โฟลเดอร์โปรเจกต์ที่อนุมัติ เซสชันที่ HIRDA ดูแล และความปลอดภัยของเวิร์กสเปซ',
  'Live capsule routing, active agent lanes, handoffs, and system health.':'ดูเส้นทางแคปซูล เลนเอเจนต์ที่กำลังทำงาน การส่งต่องาน และสถานะระบบ',
  'Appearance and interface preferences for HIRDA Studio.':'รูปลักษณ์และการตั้งค่าหน้าจอของ HIRDA Studio',
  'Search capsules, tasks, or workspaces…':'ค้นหาแคปซูล งาน หรือเวิร์กสเปซ…',
  'Notifications':'การแจ้งเตือน',
  'Show current open alerts.':'แสดงการแจ้งเตือนที่ยังเปิดอยู่',
  'Multi-Agent Orchestration':'ระบบประสานงานหลายเอเจนต์',
  'English':'อังกฤษ',
  'LIVE ORCHESTRATION':'การประสานงานแบบเรียลไทม์',
  'Live visual preview':'ตัวอย่างสถานะแบบเรียลไทม์',
  'CAPSULE PIPELINE':'เส้นทางแคปซูล',
  'Stage 1–2 · live-derived preview':'ขั้นที่ 1–2 · ตัวอย่างจากข้อมูลสด',
  'RECENT FLOW':'การทำงานล่าสุด',
  'View capsules':'ดูแคปซูล',
  'LANE LEGEND':'คำอธิบายเลน',
  'Show sessions that have been stopped but can still be resumed.':'แสดงเซสชันที่หยุดแล้วแต่ยังสามารถกลับมาใช้งานต่อได้',
  'Create a new isolated Serena session for one registered workspace.':'สร้างเซสชัน Serena แบบแยกสำหรับเวิร์กสเปซที่ลงทะเบียนไว้',
  'Run an immediate health poll of registered MCP servers.':'ตรวจสุขภาพ MCP server ที่ลงทะเบียนไว้ทันที',
  'Refresh Herdr agent and pane inventory.':'รีเฟรชรายการเอเจนต์และ pane จาก Herdr',
  'Detailed worker, queue, audit, telemetry and compatibility data. Usually not needed for normal operation.':'ข้อมูลเชิงลึกของเวิร์กเกอร์ คิว การตรวจสอบ เทเลเมทรี และความเข้ากันได้ โดยทั่วไปไม่จำเป็นสำหรับการใช้งานปกติ',
  'VNC transport':'การเชื่อมต่อ VNC',
  'noVNC assets':'ไฟล์ noVNC',
  'Token required':'ต้องใช้โทเคน',
  'checking':'กำลังตรวจสอบ',
  'Loading sessions…':'กำลังโหลดเซสชัน…',
  'Loading computer status…':'กำลังโหลดสถานะคอมพิวเตอร์…',
  'Desktop':'เดสก์ท็อป',
  'Managed session':'เซสชันที่ HIRDA ดูแล',
  'Token':'โทเคน',
  'Computer token':'โทเคนคอมพิวเตอร์',
  'Reconnect':'เชื่อมต่อใหม่',
  'Disconnect':'ตัดการเชื่อมต่อ',
  'Fullscreen':'เต็มหน้าจอ',
  'HIRDA Computer desktop':'เดสก์ท็อป HIRDA Computer',
  'Settings language':'ภาษาสำหรับการตั้งค่า',
  'Search':'ค้นหา',
  'Online':'ออนไลน์',
  'Offline':'ออฟไลน์',
  'Version':'เวอร์ชัน',
  'Model':'โมเดล',
  'Status':'สถานะ',
  'Authentication':'การยืนยันตัวตน',
  'Rate limit':'ข้อจำกัดอัตราใช้งาน',
  'Verified':'ยืนยันแล้ว',
  'Observed OK':'สังเกตแล้วว่าปกติ',
  'Credential configured':'ตั้งค่าข้อมูลยืนยันตัวตนแล้ว',
  'Auth error':'การยืนยันตัวตนผิดพลาด',
  'Logged in':'เข้าสู่ระบบแล้ว',
  'Healthy':'ปกติ',
  'Limited':'ถูกจำกัด',
  'Exhausted':'โควตาหมด',
  'Reported':'มีข้อมูลรายงาน',
  'Not reported':'ยังไม่มีข้อมูลรายงาน',
  'Interactive only':'ดูได้เฉพาะในโหมดโต้ตอบ',
  'Not available':'ไม่มีข้อมูล',
  'Not verified':'ยังไม่ได้ยืนยัน',
  'Available':'พร้อมเรียกใช้งาน',
  'Blocked':'ติดขัด',
  'Unavailable':'ไม่พร้อมใช้งาน',
  'Unknown':'ไม่ทราบ',
  'Active panes':'Pane ที่กำลังใช้งาน',
  'Active Capsules':'แคปซูลที่กำลังทำงาน',
  'AI Agents Online':'เอเจนต์ AI ออนไลน์',
  'Handoffs Today':'การส่งต่องานวันนี้',
  'Success Rate':'อัตราความสำเร็จ',
  'Queue Length':'ความยาวคิว',
  'Context Limit':'ขีดจำกัดบริบท',
  'Last Error':'ข้อผิดพลาดล่าสุด',
  'Check Runtime':'ตรวจรันไทม์',
  'View Details':'ดูรายละเอียด',
  'Ready':'พร้อม',
  'Running':'กำลังทำงาน',
  'Handoff source':'ต้นทางการส่งต่อ',
  'Received':'รับงานแล้ว',
  'Attention':'ต้องตรวจสอบ',
  'persisted capsule events':'เหตุการณ์แคปซูลที่บันทึกไว้',
  'session-derived preview':'ตัวอย่างจากข้อมูลเซสชัน',
  'control plane healthy':'control plane ปกติ',
  'No handoff yet':'ยังไม่มีการส่งต่อ',
  'Active Capsule':'แคปซูลที่กำลังทำงาน',
  'Completed':'เสร็จแล้ว',
  'Task':'งาน',
  'Workspace':'เวิร์กสเปซ',
  'Current lane':'เลนปัจจุบัน',
  'Current stage':'ขั้นตอนปัจจุบัน',
  'Source':'ต้นทาง',
  'Last handoff':'การส่งต่อล่าสุด',
  'A2A task':'งาน A2A',
  'Next fallback':'ตัวสำรองถัดไป',
  'View capsule details →':'ดูรายละเอียดแคปซูล →',
  'Workspace capsule preview':'ตัวอย่างแคปซูลของเวิร์กสเปซ',
  'Ingress':'ช่องทางเข้า',
  'Capsule Build':'สร้างแคปซูล',
  'Context':'บริบท',
  'Agent Runtime':'รันไทม์เอเจนต์',
  'Result':'ผลลัพธ์',
  'ingress':'ช่องทางเข้า',
  'capsule build':'สร้างแคปซูล',
  'context':'บริบท',
  'agent runtime':'รันไทม์เอเจนต์',
  'result':'ผลลัพธ์',
  'CONNECTED':'เชื่อมต่อแล้ว',
  'SESSIONS':'เซสชัน',
  'RECONNECTS':'การเชื่อมต่อใหม่',
  'View sessions →':'ดูเซสชัน →',
  'No tunnels configured.':'ยังไม่ได้ตั้งค่าทันเนล',
  'Healthy tunnels':'ทันเนลที่ปกติ',
  'Auto reconnect':'เชื่อมต่อใหม่อัตโนมัติ',
  'Public MCP gateway':'เกตเวย์ MCP สาธารณะ',
  'Last poll':'ตรวจล่าสุด',
  'Enabled':'เปิดใช้งาน',
  'Disabled':'ปิดใช้งาน',
  'tunnel id':'รหัสทันเนล',
  'desired state':'สถานะที่ต้องการ',
  'server id':'รหัสเซิร์ฟเวอร์',
  'Availability':'ความพร้อมใช้งาน',
  'MCP success':'ความสำเร็จของ MCP',
  'MCP P95 latency':'เวลาแฝง MCP P95',
  'Queue P95':'คิว P95',
  'Worker saturation':'ภาระเวิร์กเกอร์',
  'Reconnect / 100':'เชื่อมต่อใหม่ / 100',
  'Restore drill':'การทดสอบกู้คืน',
  'target':'เป้าหมาย',
  'budget left':'งบคงเหลือ',
  'Waiting for SLO samples.':'กำลังรอข้อมูลตัวอย่าง SLO',
  'Supervisor':'ตัวควบคุม',
  'Cancel pending':'รอยกเลิก',
  'Detached':'แยกการเชื่อมต่อ',
  'Open alerts':'การแจ้งเตือนที่เปิดอยู่',
  'Schema':'สคีมา',
  'DB integrity':'ความสมบูรณ์ของฐานข้อมูล',
  'No open operational alerts.':'ไม่มีการแจ้งเตือนระบบที่เปิดอยู่'

};
let currentLanguage='en';
const i18nTextNodes=new WeakMap();
const i18nAttrs=new WeakMap();
function tr(text){
  const raw=String(text??'');
  if(currentLanguage==='en')return raw;
  if(I18N_TH[raw]!=null)return I18N_TH[raw];
  let m;
  if((m=raw.match(/^(\d+)s ago$/)))return `${m[1]} วินาทีที่แล้ว`;
  if((m=raw.match(/^(\d+)m ago$/)))return `${m[1]} นาทีที่แล้ว`;
  if((m=raw.match(/^(\d+)h ago$/)))return `${m[1]} ชั่วโมงที่แล้ว`;
  if((m=raw.match(/^(\d+)d ago$/)))return `${m[1]} วันที่แล้ว`;
  if((m=raw.match(/^Updated (.+) · (.+)$/)))return `อัปเดต ${m[1]} · ${m[2]}`;
  if((m=raw.match(/^(\d+) running$/)))return `กำลังรัน ${m[1]}`;
  if((m=raw.match(/^(\d+) client transports?$/)))return `client transport ${m[1]}`;
  if((m=raw.match(/^(\d+) clients? · (.+)$/)))return `${m[1]} client · ${m[2]}`;
  if((m=raw.match(/^(\d+) transports?$/)))return `${m[1]} transport`;
  if((m=raw.match(/^(\d+) live$/)))return `เชื่อมต่อ ${m[1]}`;
  if((m=raw.match(/^(\d+) reconnects$/)))return `reconnect ${m[1]}`;
  if((m=raw.match(/^Show stopped \((\d+)\)$/)))return `แสดงที่หยุดแล้ว (${m[1]})`;
  if((m=raw.match(/^View (\d+) alerts?$/)))return `ดู alert ${m[1]}`;
  if((m=raw.match(/^(\d+) active · (\d+) sessions$/)))return `ใช้งาน ${m[1]} · รวม ${m[2]} เซสชัน`;
  return raw;
}
function captureAndTranslateText(root=document){
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode(node){
    if(!node.parentElement||['SCRIPT','STYLE','CODE'].includes(node.parentElement.tagName))return NodeFilter.FILTER_REJECT;
    return node.nodeValue.trim()?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT;
  }});
  const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  nodes.forEach(node=>{
    if(!i18nTextNodes.has(node))i18nTextNodes.set(node,node.nodeValue);
    const original=i18nTextNodes.get(node);
    const lead=original.match(/^\s*/)?.[0]||'';const tail=original.match(/\s*$/)?.[0]||'';const core=original.trim();
    node.nodeValue=`${lead}${tr(core)}${tail}`;
  });
  root.querySelectorAll?.('[data-help],[aria-label],[placeholder]').forEach(el=>{
    let saved=i18nAttrs.get(el);if(!saved){saved={};i18nAttrs.set(el,saved);}
    ['data-help','aria-label','placeholder'].forEach(attr=>{
      if(!el.hasAttribute(attr))return;
      if(saved[attr]==null)saved[attr]=el.getAttribute(attr);
      el.setAttribute(attr,tr(saved[attr]));
    });
  });
}
function applyLanguage(lang,{persist=true}={}){
  currentLanguage=SUPPORTED_LANGUAGES.includes(lang)?lang:'en';
  document.documentElement.lang=currentLanguage;
  const select=document.getElementById('languageSelect');if(select)select.value=currentLanguage;
  captureAndTranslateText(document);
  refreshLocalizedCommands(document);
  setView(currentView,false);
  if(latestData){renderOverview(latestData);renderReflexMetrics(latestData);renderManagedSessions(latestData);renderSessions(latestData);renderWorkspaces(latestData);renderWorkers(latestData);renderTunnels(latestData);renderReliability(latestData);renderActivity(latestData);renderDebug(latestData);captureAndTranslateText(document);}
  if(persist)localStorage.setItem('mcp-studio-language',currentLanguage);
}
function loadLanguage(){
  const saved=localStorage.getItem('mcp-studio-language');
  const browser=(navigator.language||'en').toLowerCase().startsWith('th')?'th':'en';
  applyLanguage(SUPPORTED_LANGUAGES.includes(saved)?saved:browser,{persist:false});
}

function refreshLocalizedCommands(root=document){
  root.querySelectorAll?.('[data-command-en][data-command-th]').forEach(el=>{
    el.textContent=currentLanguage==='th'?el.dataset.commandTh:el.dataset.commandEn;
  });
}
function workspaceChatGPTCommand(workspaceKey){
  return currentLanguage==='th'?`ใช้ workspace ${workspaceKey}`:`Use workspace ${workspaceKey}`;
}
async function copyText(text){
  const value=String(text||'').trim();
  if(!value)return;
  if(navigator.clipboard?.writeText){
    try{await navigator.clipboard.writeText(value);return;}catch(_err){}
  }
  const area=document.createElement('textarea');
  area.value=value;area.setAttribute('readonly','');area.style.position='fixed';area.style.opacity='0';
  document.body.appendChild(area);area.select();
  const ok=document.execCommand('copy');area.remove();
  if(!ok)throw new Error('Copy is unavailable in this browser.');
}
async function copyChatGPTCommand(button){
  const sourceId=button.dataset.copySource;
  const source=sourceId?document.getElementById(sourceId):null;
  const command=source?.textContent?.trim()||button.dataset.copyCommand||'';
  await copyText(command);
  const fallback=button.dataset.copyText||'Copy command';
  button.textContent=tr('Copied ✓');
  button.disabled=true;
  window.setTimeout(()=>{button.textContent=tr(fallback);button.disabled=false;},1400);
}

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const shortId = (v, n=10) => v ? `${String(v).slice(0,n)}…` : '—';
const when = (v) => v ? new Date(v).toLocaleString() : '—';
const ago = (v) => {
  if (!v) return '—';
  const s = Math.max(0, Math.floor((Date.now() - new Date(v).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ago`;
  return `${Math.floor(s/86400)}d ago`;
};


const FONT_SCALES=[1,1.12,1.24,1.36];
const DEFAULT_FONT_SCALE=1.12;
let fontScaleIndex=1;
function applyFontScale(index,{persist=true}={}){
  fontScaleIndex=Math.max(0,Math.min(FONT_SCALES.length-1,index));
  const scale=FONT_SCALES[fontScaleIndex];
  document.documentElement.style.setProperty('--font-scale',String(scale));
  const label=document.getElementById('fontScaleLabel');
  if(label)label.textContent=`${Math.round(scale*100)}%`;
  const down=document.getElementById('fontDecreaseBtn');
  const up=document.getElementById('fontIncreaseBtn');
  if(down)down.disabled=fontScaleIndex===0;
  if(up)up.disabled=fontScaleIndex===FONT_SCALES.length-1;
  if(persist)localStorage.setItem('mcp-studio-font-scale',String(scale));
}
function loadFontScale(){
  const version=localStorage.getItem('mcp-studio-font-scale-version');
  let saved=Number(localStorage.getItem('mcp-studio-font-scale'));
  if(version!=='2' && (!saved || Math.abs(saved-1)<0.001))saved=DEFAULT_FONT_SCALE;
  const idx=FONT_SCALES.findIndex(v=>Math.abs(v-saved)<0.001);
  applyFontScale(idx>=0?idx:FONT_SCALES.indexOf(DEFAULT_FONT_SCALE),{persist:false});
  localStorage.setItem('mcp-studio-font-scale-version','2');
}

const THEMES=['light','dark'];
let currentTheme='light';

function syncThemeControls(){
  const isLight=currentTheme==='light';
  const isDark=currentTheme==='dark';
  const light=document.getElementById('themeLightBtn');
  const dark=document.getElementById('themeDarkBtn');
  if(light){
    light.classList.toggle('active',isLight);
    light.setAttribute('aria-pressed',String(isLight));
  }
  if(dark){
    dark.classList.toggle('active',isDark);
    dark.setAttribute('aria-pressed',String(isDark));
  }
  document.querySelectorAll('[data-proxy-click="themeLightBtn"]').forEach(button=>{
    button.classList.toggle('active',isLight);
    button.setAttribute('aria-pressed',String(isLight));
  });
  document.querySelectorAll('[data-proxy-click="themeDarkBtn"]').forEach(button=>{
    button.classList.toggle('active',isDark);
    button.setAttribute('aria-pressed',String(isDark));
  });
}

function applyTheme(theme,{persist=true}={}){
  currentTheme=THEMES.includes(theme)?theme:'light';
  document.documentElement.dataset.theme=currentTheme;
  document.documentElement.style.colorScheme=currentTheme;
  const meta=document.querySelector('meta[name="theme-color"]');
  if(meta)meta.setAttribute('content',currentTheme==='dark'?'#0d1728':'#f4f7fb');
  syncThemeControls();
  if(persist)localStorage.setItem('mcp-studio-theme',currentTheme);
}

function loadTheme(){
  const saved=localStorage.getItem('mcp-studio-theme');
  applyTheme(THEMES.includes(saved)?saved:'light',{persist:false});
}

const COLOR_SCHEMES=['default','claude','codex','hermes'];
function applyColorScheme(scheme,{persist=true}={}){
  const next=COLOR_SCHEMES.includes(scheme)?scheme:'default';
  document.documentElement.dataset.colorScheme=next;
  document.querySelectorAll('[data-color-scheme]').forEach(button=>{
    const selected=button.dataset.colorScheme===next;
    button.classList.toggle('selected',selected);
    button.setAttribute('aria-pressed',String(selected));
  });
  if(persist)localStorage.setItem('mcp-studio-color-scheme',next);
}
function loadColorScheme(){
  applyColorScheme(localStorage.getItem('mcp-studio-color-scheme')||'default',{persist:false});
}


function openAppDialog({title,message='',icon='?',tone='info',confirmText='Continue',cancelText='Cancel',showCancel=true,fields=[]}={}){
  const dialog=document.getElementById('appDialog');
  const titleEl=document.getElementById('appDialogTitle');
  const messageEl=document.getElementById('appDialogMessage');
  const iconEl=document.getElementById('appDialogIcon');
  const fieldsEl=document.getElementById('appDialogFields');
  const confirmBtn=document.getElementById('appDialogConfirm');
  const cancelBtn=document.getElementById('appDialogCancel');
  titleEl.textContent=tr(title||'Confirm action');
  messageEl.textContent=tr(message||'');
  messageEl.hidden=!message;
  iconEl.textContent=icon;
  iconEl.className=`dialog-icon ${tone}`;
  confirmBtn.textContent=tr(confirmText);
  confirmBtn.className=`button ${tone==='danger'?'danger':''}`.trim();
  cancelBtn.textContent=tr(cancelText);
  cancelBtn.hidden=!showCancel;
  fieldsEl.innerHTML='';
  const inputs={};
  fields.forEach((field,index)=>{
    const label=document.createElement('label');
    label.className='dialog-field';
    const span=document.createElement('span');
    span.textContent=tr(field.label||field.name);
    let input;
    if(field.type==='textarea'){
      input=document.createElement('textarea');
    }else if(field.type==='select'){
      input=document.createElement('select');
      (field.options||[]).forEach(option=>{
        const el=document.createElement('option');
        if(typeof option==='string'){el.value=option;el.textContent=tr(option);}
        else{el.value=option.value;el.textContent=tr(option.label||option.value);}
        input.appendChild(el);
      });
    }else{
      input=document.createElement('input');
      input.type=field.type||'text';
    }
    input.id=`dialogField${index}`;
    input.name=field.name;
    input.value=field.value||'';
    if(field.placeholder)input.placeholder=tr(field.placeholder);
    if(field.required)input.required=true;
    if(field.readonly)input.readOnly=true;
    if(field.pattern)input.pattern=field.pattern;
    if(field.autocomplete)input.autocomplete=field.autocomplete;
    label.append(span,input);
    fieldsEl.append(label);
    inputs[field.name]=input;
  });
  return new Promise(resolve=>{
    let settled=false;
    const finish=(result)=>{
      if(settled)return;settled=true;
      confirmBtn.removeEventListener('click',onConfirm);
      cancelBtn.removeEventListener('click',onCancel);
      dialog.removeEventListener('cancel',onNativeCancel);
      if(dialog.open)dialog.close();
      resolve(result);
    };
    const onConfirm=()=>{
      const values={};
      for(const [name,input] of Object.entries(inputs)){
        if(!input.reportValidity())return;
        values[name]=input.value.trim();
      }
      finish({confirmed:true,values});
    };
    const onCancel=()=>finish({confirmed:false,values:{}});
    const onNativeCancel=e=>{e.preventDefault();onCancel();};
    confirmBtn.addEventListener('click',onConfirm);
    cancelBtn.addEventListener('click',onCancel);
    dialog.addEventListener('cancel',onNativeCancel);
    dialog.showModal();
    const first=fieldsEl.querySelector('input,textarea');
    (first||confirmBtn).focus();
    if(first && typeof first.select==='function')first.select();
  });
}
async function showUiError(err){
  await openAppDialog({title:tr('Action failed'),message:err?.message||String(err),icon:'!',tone:'danger',confirmText:'Close',showCancel:false});
}
async function uiAction(fn){try{return await fn();}catch(err){await showUiError(err);return undefined;}}

function badge(status) {
  const normalized = String(status || 'unknown').toLowerCase();
  const aliases = {running:'busy',active:'busy',queued:'degraded',completed:'healthy',failed:'down',cancelled:'unknown',detached:'degraded',cancel_pending:'degraded',assigned:'busy',dispatching:'busy',waiting_agent:'busy',reconnecting:'degraded',recovering:'degraded',stalled:'down',dispatch_uncertain:'degraded',idle:'healthy',stopped:'unknown'};
  const css = aliases[normalized] || normalized;
  return `<span class="pill ${esc(css)}">${esc(normalized.toUpperCase())}</span>`;
}
function workerBadge(state){ return badge(state || 'unknown'); }
function overviewCard(label, value, sub, cls='', help=''){ return `<article class="summary-card ${esc(cls)}" ${help?`data-help="${esc(help)}" tabindex="0"`:''}><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(sub || '')}</small></article>`; }
function detailBlock(rows){
  return `<details class="details"><summary>${esc(tr('Technical details'))}</summary><div class="details-grid">${rows.map(([k,v])=>`<span>${esc(k)}</span><code>${esc(v ?? '—')}</code>`).join('')}</div></details>`;
}
async function getJson(url, options){ const r = await fetch(url, options); if(!r.ok){let d='';try{const j=await r.json();d=j.detail||JSON.stringify(j)}catch{}throw new Error(`${r.status} ${d||url}`)} return r.json(); }
async function sendJson(url, body){ return getJson(url,{method:'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)}); }

const viewMeta = {
  home:['Dashboard','Dashboard','Live capsule routing, active agent lanes, handoffs, and system health.'],
  sessions:['Capsule Lanes','Capsule Lanes','Inspect capsule state, routing, handoffs, and session history.'],
  agents:['Agents','Agents','Monitor runtime availability, authentication health, rate limits, and active capsule load.'],
  reflex:['Reflex','Reflex','Observe HIRDA Reflex decisions, JEV teacher agreement, latency, risk, and dataset growth.'],
  actions:['Actions','JEV Action Adapter','Test one shared action schema across Browser, World, and Finance without executing candidates.'],
  workspaces:['Workspaces','Workspaces','Manage approved projects, managed sessions, and workspace safety.'],
  guide:['Guide','Guide','Daily use first, then setup and troubleshooting.'],
  computer:['Computer','Computer','Open a shared browser desktop for OAuth, sign-in, consent, and other human-in-the-loop actions.'],
  system:['System','System','Monitor runtime health, ingress, reliability, and diagnostics.'],
  settings:['Settings','Settings','Configure appearance, language, text size, and color preferences.']
};

function setView(view, updateHash=true){
  if(!viewMeta[view]) view='home';
  currentView=view;
  document.body.dataset.view=view;
  document.querySelectorAll('.view-page').forEach(x=>x.classList.toggle('active',x.dataset.page===view));
  document.querySelectorAll('.primary-nav a[data-view], .mobile-nav a[data-view]').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
  const [crumb,title,sub]=viewMeta[view];
  document.getElementById('viewBreadcrumb').textContent=tr(crumb);
  document.getElementById('viewTitle').textContent=tr(title);
  document.getElementById('viewSubtitle').textContent=tr(sub);
  if(updateHash && location.hash !== `#${view}`) history.replaceState(null,'',`#${view}`);
  if(view==='computer') setTimeout(refreshComputerView,0);
  if(view==='reflex') setTimeout(()=>uiAction(refreshReflexMetrics),0);
  if(view==='actions') setTimeout(()=>uiAction(refreshActionPlayground),0);
  window.scrollTo({top:0,behavior:'smooth'});
}

function tunnelNameMap(status){ return Object.fromEntries((status.tunnels||[]).map(t=>[t.id,t.name||t.id])); }
function logicalMap(sessions){ return Object.fromEntries((sessions.sessions||[]).map(s=>[s.id,s])); }
function connectedGateway(gatewaySessions){ return (gatewaySessions.sessions||[]).filter(s=>s.status==='connected'); }
function activeWorkspaces(workerData){
  const leaseByWorker=Object.fromEntries((workerData.leases||[]).map(l=>[l.worker_id,l]));
  return (workerData.workers||[]).filter(w=>w.workspace).map(w=>({worker:w,lease:leaseByWorker[w.id]}));
}
function clientLabel(g, logical, i){
  const type=String(g.client_type || logical?.client_type || '').toLowerCase();
  if(type.includes('chatgpt') || type.includes('openai')) return `ChatGPT session ${i+1}`;
  if(type && type!=='unknown') return `${type.replaceAll('_',' ')} session ${i+1}`;
  return `MCP session ${i+1}`;
}

function capsuleDisplayId(session){
  if(!session) return 'C-204';
  const raw=String(session.id||session.name||'204');
  let hash=0;
  for(let i=0;i<raw.length;i++) hash=((hash<<5)-hash+raw.charCodeAt(i))|0;
  return `C-${String(100+(Math.abs(hash)%900)).padStart(3,'0')}`;
}

function normalizeCapsuleStage(stage=''){
  return ({capsule_build:'build',agent_runtime:'runtime',completed:'result'}[stage]||stage||'');
}

function capsuleStageNodes(activeStage=''){
  const active=normalizeCapsuleStage(activeStage);
  const icon=(key)=>{
    const paths={
      ingress:'<path d="M5 12h12M13 8l4 4-4 4"/><path d="M5 7v10"/>',
      build:'<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 9h6M9 13h6"/>',
      context:'<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M8 8h3v3H8zM13 8h3v3h-3zM8 13h3v3H8zM13 13h3v3h-3z"/>',
      runtime:'<circle cx="12" cy="12" r="4"/><path d="M12 4v2M12 18v2M4 12h2M18 12h2M6.3 6.3l1.4 1.4M16.3 16.3l1.4 1.4M17.7 6.3l-1.4 1.4M7.7 16.3l-1.4 1.4"/>',
      result:'<circle cx="12" cy="12" r="7"/><path d="m9 12 2 2 4-4"/>'
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[key]}</svg>`;
  };
  const stages=[['ingress','Ingress'],['build','Capsule Build'],['context','Context'],['runtime','Agent Runtime'],['result','Result']];
  return stages.map(([key,label])=>`<div class="capsule-stage ${key===active?'active':''}"><span class="stage-node">${icon(key)}</span><small>${label}</small></div>`).join('');
}

function capsuleContextProfile(capsule){
  const raw=capsule?.context_profile||capsule?.metadata?.context_profile||{};
  const source=Number(raw.source_tokens||0);
  const retained=Number(raw.retained_tokens||0);
  let pct=Number(raw.retained_percent);
  if(!Number.isFinite(pct) && source>0) pct=(retained/source)*100;
  pct=Math.max(0,Math.min(100,Number.isFinite(pct)?pct:0));
  let type=String(capsule?.capsule_type||raw.capsule_type||'').toLowerCase();
  if(!['full','compact','minimal'].includes(type)) type=pct===100?'full':pct>=40?'compact':pct>0?'minimal':'unknown';
  return {source,retained,pct,type};
}

function renderCapsuleContextBar(capsule,{compact=false}={}){
  const profile=capsuleContextProfile(capsule);
  if(profile.type==='unknown') return compact?'':'<div class="capsule-context-empty">Context profile pending</div>';
  const fit=capsule?.last_context_fit||capsule?.last_handoff?.context_fit||null;
  const fitState=fit?.fit_state||'';
  const fitText=fit?`${fit.fit?'FIT':'BLOCKED'} · ${esc(fit.utilization_percent??'—')}% of target usable context`:'Fit not evaluated';
  const opacity=(0.35+(profile.pct/100)*0.65).toFixed(2);
  return `<div class="capsule-context-meter ${esc(profile.type)} ${compact?'compact':''}">
    <div class="capsule-context-head"><span class="capsule-type-badge ${esc(profile.type)}">${esc(profile.type.toUpperCase())}</span><strong>${esc(profile.pct.toFixed(1))}% retained</strong><small>${esc(fitText)}</small></div>
    <div class="capsule-context-track" aria-label="${esc(profile.type)} capsule retaining ${esc(profile.pct.toFixed(1))} percent of source context"><i style="width:${profile.pct}%;opacity:${opacity}"></i></div>
    ${compact?'':`<div class="capsule-context-foot"><span>${esc(profile.retained.toLocaleString())} / ${esc(profile.source.toLocaleString())} tokens</span>${fitState?`<em class="fit-${esc(fitState)}">${esc(fitState.toUpperCase())}</em>`:''}</div>`}
  </div>`;
}

function laneStatusVisual(state){
  const value=String(state||'').toLowerCase();
  if(value.includes('emergency'))return {key:'emergency',symbol:'!',label:'Emergency'};
  if(value.includes('disabled'))return {key:'disabled',symbol:'×',label:'Disabled'};
  if(value.includes('draining'))return {key:'draining',symbol:'◔',label:'Draining'};
  if(value.includes('awaiting ack'))return {key:'awaiting-ack',symbol:'↗',label:'Awaiting ACK'};
  if(value.includes('handoff'))return {key:'handoff',symbol:'⇄',label:'Handoff'};
  if(value.includes('received'))return {key:'received',symbol:'✓',label:'Received'};
  if(value.includes('running'))return {key:'running',symbol:'●',label:'Running'};
  if(value.includes('ready'))return {key:'ready',symbol:'✓',label:'Ready'};
  if(value.includes('standby'))return {key:'standby',symbol:'○',label:'Standby'};
  return {key:'unknown',symbol:'•',label:state||'Unknown'};
}

function renderCapsuleLane({agent,sub,colorClass,active=false,activeStage='',capsuleId='',muted=false,statusText='',capsule=null}){
  const state=statusText||(active?'Running':muted?'Standby':'Ready');
  const visual=laneStatusVisual(state);
  return `<div class="capsule-lane ${colorClass} ${active?'lane-active':''} ${muted?'lane-muted':''}">
    <div class="lane-agent">
      <span class="lane-agent-icon">${agent==='Claude'?'✦':agent==='Codex'?'⌁':'◆'}</span>
      <div><strong>${esc(agent)}</strong><small>${esc(sub||'')}</small></div>
    </div>
    <div class="lane-track">${capsuleStageNodes(activeStage)}</div>
    <div class="lane-status">
      <div class="lane-status-state ${esc(visual.key)}" aria-label="${esc(visual.label)}">
        <span class="lane-status-symbol" aria-hidden="true">${esc(visual.symbol)}</span>
        <span class="lane-status-text">${esc(state)}</span>
      </div>
      ${capsuleId?`<b>${esc(capsuleId)}</b>`:''}
      ${capsuleId&&capsule?`<div class="lane-context-mini ${esc(capsuleContextProfile(capsule).type)}"><i style="width:${capsuleContextProfile(capsule).pct}%"></i></div>`:''}
    </div>
  </div>`;
}

function runtimeHealthClass(status){
  const value=String(status||'unknown').toLowerCase();
  if(['logged_in','healthy'].includes(value))return 'fact-ready';
  if(['configured','reported','interactive_only'].includes(value))return 'fact-info';
  if(value==='limited')return 'fact-warning';
  if(['error','exhausted'].includes(value))return 'fact-danger';
  return 'fact-muted';
}

function authHealthLabel(health){
  const h=health||{};
  const status=String(h.status||'unknown').toLowerCase();
  if(['logged_in','healthy'].includes(status))return tr('Logged in');
  if(status==='configured')return tr('Credential configured');
  if(status==='error')return tr('Auth error');
  if(status==='unavailable')return tr('Unavailable');
  return tr('Not verified');
}

function limitHealthLabel(health){
  const h=health||{};
  const status=String(h.status||'unknown').toLowerCase();
  const labels={
    healthy:'Healthy',
    limited:'Limited',
    exhausted:'Exhausted',
    reported:'Reported',
    interactive_only:'Interactive only',
    unavailable:'Unavailable',
    unknown:'Not available'
  };
  const base=tr(labels[status]||'Not reported');
  return h.remaining!=null && h.remaining!==''?`${base} · ${h.remaining}`:base;
}

function runtimePresence(runtime){
  const r=runtime||{};
  const state=String(r.status||'unknown').toLowerCase();
  const observed=Boolean(r.observed || Number(r.pane_count||0)>0);
  if(['blocked','error','failed'].includes(state)){
    return {label:'Attention',className:'runtime-attention',online:false};
  }
  if(observed && ['running','ready','available'].includes(state)){
    return {label:'Online',className:'runtime-online',online:true};
  }
  if(r.installed!==false && state!=='unavailable'){
    return {label:'Available',className:'runtime-available',online:false};
  }
  return {label:'Offline',className:'runtime-offline',online:false};
}

function runtimeStateLabel(state){
  const raw=String(state||'unknown').toLowerCase();
  const labels={
    running:'Running',
    ready:'Ready',
    available:'Available',
    blocked:'Blocked',
    unavailable:'Unavailable',
    unknown:'Unknown'
  };
  return tr(labels[raw]||raw);
}

function renderAgentLanes(data){
  const grid=document.getElementById('agentRuntimeGrid');
  if(!grid) return;
  const discovered=data?.agentRuntimesData?.runtimes||[];
  const fallback=[
    {id:'claude',name:'Claude',brand:'Anthropic',provider:'Anthropic',status:'unknown',installed:false},
    {id:'codex',name:'Codex',brand:'OpenAI',provider:'OpenAI',status:'unknown',installed:false},
    {id:'hermes',name:'Hermes',brand:'Local/Custom',provider:'Local/Custom',status:'unknown',installed:false}
  ];
  const cards=discovered.length?discovered:fallback;
  const laneStateById=data?.laneStatesData?.lanes||{};
  grid.innerHTML=cards.map(r=>{
    const id=String(r.id||'').toLowerCase();
    const laneState=String(laneStateById[id]?.state||'normal').toLowerCase();
    const laneReason=laneStateById[id]?.reason||'';
    const icon=id==='claude'?'✷':id==='codex'?'◎':'⬡';
    const state=String(r.status||'unknown').toLowerCase();
    const version=r.version||tr('Not reported');
    const model=r.model||r.last_used_model||tr('Not reported');
    const provider=r.provider||r.brand||'Unknown provider';
    const presence=runtimePresence(r);
    const paneCount=Number(r.pane_count??0);
    const queueLength=r.queue_length;
    const contextLimit=r.context_limit||r.context_window;
    const lastError=r.last_error;
    const authHealth=r.auth_health||{status:'unknown'};
    const limitHealth=r.limit_health||{status:'unknown'};
    return `<article class="agent-runtime-card ${esc(id)}">
      <div class="agent-runtime-top">
        <span class="agent-runtime-icon">${icon}</span>
        <div class="agent-runtime-title"><h3>${esc(r.name||id)}</h3><small>${esc(provider)}</small></div>
        <span class="runtime-pill ${presence.className}"><i></i>${esc(tr(presence.label))}</span>
      </div>
      <div class="agent-reference-facts">
        <div><span>Version</span><strong>${esc(version)}</strong></div>
        <div><span>Model</span><strong>${esc(model)}</strong></div>
        <div><span>Status</span><strong class="${presence.online?'fact-ready':state==='blocked'?'fact-danger':'fact-muted'}">${esc(runtimeStateLabel(state))}</strong></div>
        <div><span>Authentication</span><strong class="${runtimeHealthClass(authHealth.status)}">${esc(authHealthLabel(authHealth))}</strong></div>
        <div><span>Rate limit</span><strong class="${runtimeHealthClass(limitHealth.status)}">${esc(limitHealthLabel(limitHealth))}</strong></div>
        <div><span>Lane routing</span><strong class="${laneState==='normal'?'fact-ready':laneState==='draining'?'fact-muted':'fact-danger'}">${esc(laneState.replaceAll('_',' ').replace(/\b\w/g,ch=>ch.toUpperCase()))}</strong></div>
        ${laneReason?`<div><span>Lane reason</span><strong>${esc(laneReason)}</strong></div>`:''}
        <div><span>Active panes</span><strong>${paneCount}</strong></div>
        ${queueLength==null?'':`<div><span>Queue Length</span><strong>${esc(queueLength)}</strong></div>`}
        ${contextLimit==null?'':`<div><span>Context Limit</span><strong>${esc(contextLimit)}</strong></div>`}
        ${lastError? `<div><span>Last Error</span><strong class="fact-danger">${esc(lastError)}</strong></div>` : ''}
      </div>
      <div class="agent-reference-actions">
        <select class="button secondary small" data-lane-state-select="${esc(id)}" aria-label="Lane routing state for ${esc(r.name||id)}">
          ${['normal','draining','disabled','emergency'].map(value=>`<option value="${value}"${value===laneState?' selected':''}>${value.replaceAll('_',' ').replace(/\b\w/g,ch=>ch.toUpperCase())}</option>`).join('')}
        </select>
        <button class="button secondary small" data-lane-state-apply="${esc(id)}" type="button">Apply Lane State</button>
        <button class="button secondary small" data-agent-check="${esc(id)}" type="button">Check Runtime</button>
        <button class="button secondary small agent-log-button" data-agent-details="${esc(id)}" type="button">View Details</button>
      </div>
    </article>`;
  }).join('');
}


function reflexPretty(value){
  return String(value??'')
    .replaceAll('_',' ')
    .replace(/\b\w/g,ch=>ch.toUpperCase());
}
function reflexPct(value){
  return value==null?'—':`${Number(value).toFixed(1)}%`;
}
function reflexMs(value){
  return value==null?'—':`${Number(value).toFixed(value<10?2:1)} ms`;
}
function reflexBars(values, order){
  const pairs=(order||Object.keys(values||{})).map(key=>[key,Number((values||{})[key]||0)]);
  const max=Math.max(1,...pairs.map(([,value])=>value));
  return pairs.map(([key,value])=>`<div class="reflex-bar-row"><span>${esc(reflexPretty(key))}</span><div class="reflex-bar-track"><i style="width:${Math.max(value?3:0,(value/max)*100)}%"></i></div><strong>${esc(value)}</strong></div>`).join('');
}
function reflexActionPill(action){
  const normalized=String(action||'unknown').toLowerCase();
  return `<span class="reflex-action-pill ${esc(normalized)}">${esc(normalized.toUpperCase())}</span>`;
}
function renderReflexMetrics(data){
  const root=document.getElementById('reflexMetricsPanel');
  if(!root)return;
  const metrics=data?.reflexMetricsData;
  if(!metrics){
    root.innerHTML='<div class="panel-card"><div class="empty">Reflex metrics are unavailable from this HIRDA process.</div></div>';
    return;
  }
  const summary=metrics.summary||{};
  const teacher=metrics.teacher||{};
  const latency=metrics.latency||{};
  const outcomes=metrics.outcomes||{};
  const dist=metrics.distributions||{};
  const config=metrics.config||{};
  const recent=metrics.recent||[];
  const signals=dist.signals||[];
  const timeline=metrics.timeline||[];
  const dataset=metrics.dataset||{};
  const maxTimeline=Math.max(1,...timeline.map(item=>Number(item.decisions||0)));
  const teacherStatus=config.teacher_enabled
    ? (config.teacher_api_key_configured===false?'Key missing · '+(teacher.availability_rate==null?'availability unknown':`${reflexPct(teacher.availability_rate)} available`):(teacher.availability_rate==null?'Configured · waiting for samples':`Configured · ${reflexPct(teacher.availability_rate)} available`))
    : 'Disabled';
  const teacherActions=teacher.actions||{};
  const disagreements=teacher.disagreement_diagnostics||{};
  const disagreementPairs=disagreements.reflex_to_teacher_actions||{};
  const disagreementTools=disagreements.tools||{};
  const disagreementPermissions=disagreements.permission_classes||{};
  const topEvidence=(values)=>Object.entries(values).slice(0,3).map(([key,value])=>`${reflexPretty(key)} ${value}`).join(' · ')||'None recorded';
  const agreement=config.teacher_enabled?reflexPct(teacher.agreement_rate):'—';
  const outcomeRate=reflexPct(outcomes.http_success_rate);
  const mode=String(config.reflex_mode||'unknown').toUpperCase();

  root.innerHTML=`
    <div class="reflex-status-strip panel-card">
      <div class="reflex-engine-identity">
        <span class="reflex-engine-mark">◇</span>
        <div><span class="eyebrow">LOCAL AUTHORITY</span><strong>HIRDA Reflex</strong><small>${esc(mode)} · static permission remains the hard boundary</small></div>
      </div>
      <div class="reflex-status-facts">
        <span><small>Reflex</small><strong>${config.reflex_enabled?'Enabled':'Disabled'}</strong></span>
        <span><small>JEV teacher</small><strong>${esc(teacherStatus)}</strong></span>
        <span><small>Dataset</small><strong>${esc(dataset.records||0)} records</strong></span>
      </div>
    </div>

    <div class="reflex-summary-grid">
      <article class="reflex-metric-card"><span>Decisions</span><strong>${esc(summary.decisions||0)}</strong><small>${esc(metrics.window||reflexWindow)} window</small></article>
      <article class="reflex-metric-card allow"><span>Reflex allow</span><strong>${esc(reflexPct(summary.allow_rate))}</strong><small>${esc(summary.allow||0)} allow</small></article>
      <article class="reflex-metric-card review"><span>Reflex review</span><strong>${esc(reflexPct(summary.review_rate))}</strong><small>${esc(summary.review||0)} review</small></article>
      <article class="reflex-metric-card deny"><span>Reflex deny</span><strong>${esc(reflexPct(summary.deny_rate))}</strong><small>${esc(summary.deny||0)} deny</small></article>
      <article class="reflex-metric-card teacher"><span>JEV agreement</span><strong>${esc(agreement)}</strong><small>${esc(teacher.evaluated||0)} evaluated</small></article>
      <article class="reflex-metric-card"><span>Rule confidence</span><strong>${esc(reflexPct(summary.average_rule_confidence==null?null:Number(summary.average_rule_confidence)*100))}</strong><small>rule coverage, not correctness probability</small></article>
      <article class="reflex-metric-card"><span>Reflex median</span><strong>${esc(reflexMs(latency.reflex?.median_ms))}</strong><small>P95 ${esc(reflexMs(latency.reflex?.p95_ms))}</small></article>
      <article class="reflex-metric-card"><span>JEV median</span><strong>${esc(reflexMs((latency.teacher_success||latency.teacher)?.median_ms))}</strong><small>P95 ${esc(reflexMs((latency.teacher_success||latency.teacher)?.p95_ms))}</small></article>
      <article class="reflex-metric-card"><span>Transport success</span><strong>${esc(outcomeRate)}</strong><small>HTTP only · not semantic correctness</small></article>
    </div>

    <div class="reflex-analysis-grid">
      <article class="panel-card reflex-chart-card">
        <div class="panel-head"><div><span class="eyebrow">DECISIONS</span><h3>Action mix</h3></div><small>allow / review / deny</small></div>
        <div class="reflex-bars">${reflexBars(dist.actions||{},['allow','review','deny'])}</div>
      </article>
      <article class="panel-card reflex-chart-card">
        <div class="panel-head"><div><span class="eyebrow">RISK</span><h3>Risk distribution</h3></div><small>threshold-aware</small></div>
        <div class="reflex-bars">${reflexBars(dist.risk||{},['low','elevated','review','deny'])}</div>
      </article>
      <article class="panel-card reflex-chart-card">
        <div class="panel-head"><div><span class="eyebrow">COMPUTE</span><h3>Fast vs deep</h3></div><small>local compute lane</small></div>
        <div class="reflex-bars">${reflexBars(dist.compute_lanes||{},['fast','deep'])}</div>
      </article>
      <article class="panel-card reflex-chart-card teacher-card">
        <div class="panel-head"><div><span class="eyebrow">TEACHER</span><h3>JEV comparison</h3></div><small>shadow evidence only</small></div>
        <div class="reflex-teacher-grid">
          <div><span>Available</span><strong>${esc(reflexPct(teacher.availability_rate))}</strong></div>
          <div><span>JEV Allow</span><strong>${esc(teacherActions.allow||0)} · ${esc(reflexPct(teacher.allow_rate))}</strong></div>
          <div><span>JEV Review</span><strong>${esc(teacherActions.review||0)} · ${esc(reflexPct(teacher.review_rate))}</strong></div>
          <div><span>JEV Deny</span><strong>${esc(teacherActions.deny||0)} · ${esc(reflexPct(teacher.deny_rate))}</strong></div>
          <div><span>Agreement</span><strong>${esc(agreement)}</strong></div>
          <div><span>Disagree</span><strong>${esc(teacher.disagreement||0)}</strong></div>
          <div><span>Top disagreements</span><strong>${esc(topEvidence(disagreementPairs))}</strong></div>
          <div><span>Evidence</span><strong>${esc(topEvidence(disagreementTools))}</strong><small>${esc(topEvidence(disagreementPermissions))}</small></div>
        </div>
        ${(teacher.errors||[]).length?`<div class="reflex-errors">${teacher.errors.slice(0,4).map(item=>`<span><code>${esc(item.error)}</code><b>${esc(item.count)}</b></span>`).join('')}</div>`:''}
      </article>
    </div>

    <div class="reflex-detail-grid">
      <article class="panel-card reflex-timeline-card">
        <div class="panel-head"><div><span class="eyebrow">VOLUME</span><h3>Decision activity</h3></div><small>${esc(metrics.window||reflexWindow)}</small></div>
        <div class="reflex-timeline">
          ${timeline.length?timeline.map(item=>`<div class="reflex-timeline-column" title="${esc(item.bucket)} · ${esc(item.decisions)} decisions"><i style="height:${Math.max(item.decisions?5:0,(Number(item.decisions||0)/maxTimeline)*100)}%"></i><small>${esc(item.decisions||0)}</small></div>`).join(''):'<div class="empty">No decision samples in this window.</div>'}
        </div>
      </article>
      <article class="panel-card reflex-signals-card">
        <div class="panel-head"><div><span class="eyebrow">TRIGGERS</span><h3>Top risk signals</h3></div><small>feature flags only</small></div>
        <div class="reflex-signal-list">
          ${signals.length?signals.map(item=>`<div><span>${esc(reflexPretty(item.signal))}</span><strong>${esc(item.count)}</strong></div>`).join(''):'<div class="empty">No elevated risk signals recorded.</div>'}
        </div>
      </article>
    </div>

    <article class="panel-card reflex-recent-card">
      <div class="panel-head"><div><span class="eyebrow">TRACE</span><h3>Recent decisions</h3></div><small>arguments and credentials are never shown</small></div>
      <div class="reflex-table-wrap">
        <table class="reflex-table">
          <thead><tr><th>Time</th><th>Workspace</th><th>Tool</th><th>Class</th><th>Reflex</th><th>Risk</th><th>Lane</th><th>JEV</th><th>Latency</th></tr></thead>
          <tbody>
            ${recent.length?recent.map(row=>{
              const teacherRow=row.teacher||{};
              const teacherLabel=!teacherRow.enabled?'disabled':!teacherRow.evaluated?(teacherRow.error||'unavailable'):(teacherRow.agreement===true?`✓ ${teacherRow.action}`:`≠ ${teacherRow.action}`);
              const stamp=row.recorded_at?new Date(row.recorded_at):null;
              const when=stamp&&!Number.isNaN(stamp.getTime())?stamp.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
              return `<tr>
                <td>${esc(when)}</td><td>${esc(row.workspace_key||'—')}</td><td><code>${esc(row.tool||'—')}</code></td><td>${esc(row.permission_class||'—')}</td>
                <td>${reflexActionPill(row.action)}</td><td>${esc(Number(row.risk||0).toFixed(2))}</td><td>${esc(row.compute_lane||'—')}</td>
                <td class="${teacherRow.agreement===false?'reflex-disagree':''}">${esc(teacherLabel)}</td><td>${esc(reflexMs(row.timings?.reflex_ms))} · ${esc(reflexMs(row.timings?.teacher_ms))}</td>
              </tr>`;
            }).join(''):'<tr><td colspan="9"><div class="empty">No Reflex decisions recorded yet.</div></td></tr>'}
          </tbody>
        </table>
      </div>
    </article>

    <div class="reflex-boundary-note">
      <strong>Authority boundary</strong>
      <span>Static permissions and workspace scope stay authoritative. Reflex may only narrow an already-allowed call. JEV is teacher/shadow evidence and never grants or blocks runtime authority.</span>
    </div>
  `;
}

async function refreshReflexMetrics(){
  const select=document.getElementById('reflexWindowSelect');
  reflexWindow=select?.value||reflexWindow;
  const metrics=await getJson(`/api/reflex/metrics?window=${encodeURIComponent(reflexWindow)}&recent=20`);
  if(!latestData)latestData={};
  latestData.reflexMetricsData=metrics;
  renderReflexMetrics(latestData);
  captureAndTranslateText(document.getElementById('reflexMetricsPanel')||document);
}


function actionRiskLabels(risk){
  const labels=[];
  if(risk?.consequential)labels.push('consequential');
  if(risk?.reversible===false)labels.push('irreversible');
  if(risk?.external_side_effect)labels.push('external side effect');
  if(risk?.destructive)labels.push('destructive');
  if(risk?.financial)labels.push('financial');
  if(risk?.requires_human_approval)labels.push('human approval');
  return labels;
}

function renderActionPlayground(){
  const providerSummary=document.getElementById('actionProviderSummary');
  const envelopePanel=document.getElementById('actionEnvelopePanel');
  const judgmentPanel=document.getElementById('actionJudgmentPanel');
  const provider=currentActionProvider;
  if(providerSummary){
    providerSummary.innerHTML=provider
      ? `<div class="action-provider-identity"><div><span class="eyebrow">${esc(provider.domain||'unknown')} · ${esc(provider.source||'runtime')}</span><strong>${esc(provider.title||provider.provider_id||'Provider')}</strong><p>${esc(provider.description||'')}</p></div><span class="pill healthy">${esc(provider.action_count||provider.envelope?.actions?.length||0)} actions</span></div>`
      : '<div class="empty">Choose a test provider.</div>';
  }

  if(envelopePanel){
    const envelope=provider?.envelope;
    if(!envelope){
      envelopePanel.innerHTML='<div class="empty">Load a provider sample to inspect its state and candidate actions.</div>';
    }else{
      const actions=envelope.actions||[];
      envelopePanel.innerHTML=`
        <div class="action-envelope-head">
          <div><span>Request</span><code>${esc(envelope.request_id||'—')}</code></div>
          <div><span>Provider</span><strong>${esc(envelope.provider||'—')}</strong></div>
          <div><span>Domain</span><strong>${esc(envelope.domain||'—')}</strong></div>
          <div><span>Executor</span><strong>Not attached</strong></div>
        </div>
        <div class="action-goal"><span>Goal</span><p>${esc(envelope.goal||'—')}</p></div>
        <div class="action-envelope-layout">
          <div class="action-candidate-list">
            ${actions.map((action,index)=>{
              const labels=actionRiskLabels(action.risk||{});
              return `<article class="action-candidate-card">
                <div class="action-candidate-title"><span class="action-token">a${index}</span><div><strong>${esc(action.title||action.action_id)}</strong><code>${esc(action.action_id||'')}</code></div><span class="pill ${action.permission_class==='destructive'?'down':action.permission_class==='execute'?'degraded':'healthy'}">${esc(action.permission_class||'unknown')}</span></div>
                <p>${esc(action.description||'')}</p>
                <div class="action-risk-tags">${labels.length?labels.map(label=>`<span>${esc(label)}</span>`).join(''):'<span class="quiet">no elevated risk flags</span>'}</div>
              </article>`;
            }).join('')}
          </div>
          <div class="action-state-card"><span class="eyebrow">STRUCTURED STATE</span><pre>${esc(JSON.stringify(envelope.state||{},null,2))}</pre></div>
        </div>
      `;
    }
  }

  if(judgmentPanel){
    const judgment=latestActionJudgment;
    if(!judgment){
      judgmentPanel.innerHTML='<div class="empty">Run an evaluation to see JEV choice, confidence, and risk signals.</div>';
    }else if(!judgment.evaluated){
      judgmentPanel.innerHTML=`
        <div class="action-judgment-state unavailable"><span>JEV unavailable</span><strong>${esc(judgment.error||'not evaluated')}</strong><small>No action was executed. Runtime authority remains outside this adapter.</small></div>
      `;
    }else{
      const probabilities=Object.entries(judgment.probabilities||{}).sort((a,b)=>Number(b[1])-Number(a[1]));
      const signals=[
        ['Human review',judgment.needs_human_review],
        ['More information',judgment.needs_more_information],
        ['Consequence risk',judgment.consequence_risk],
        ['Uncertainty',judgment.uncertainty],
      ];
      judgmentPanel.innerHTML=`
        <div class="action-selected-card">
          <span>Selected candidate</span>
          <strong>${esc(judgment.selected_action_id||'—')}</strong>
          <small>${esc(reflexPct(Number(judgment.confidence||0)*100))} confidence · ${esc(judgment.model||'JEV')}</small>
        </div>
        <div class="action-signal-grid">
          ${signals.map(([label,value])=>`<div><span>${esc(label)}</span><strong>${value==null?'—':esc((Number(value)*100).toFixed(0)+'%')}</strong></div>`).join('')}
        </div>
        <div class="action-probability-list">
          ${probabilities.length?probabilities.map(([actionId,value])=>`<div><span><code>${esc(actionId)}</code></span><div class="reflex-bar-track"><i style="width:${Math.max(2,Number(value||0)*100)}%"></i></div><strong>${esc((Number(value||0)*100).toFixed(0)+'%')}</strong></div>`).join(''):'<div class="empty">JEV did not return candidate probabilities.</div>'}
        </div>
        <div class="action-authority-lock">advisory_only=true · runtime_authorization_required=true · may_execute=false</div>
      `;
    }
  }
  captureAndTranslateText(document.querySelector('[data-page="actions"]')||document);
}

async function loadActionProvider(providerId){
  const normalized=providerId||document.getElementById('actionProviderSelect')?.value||'browser';
  currentActionProvider=await getJson(`/api/action-adapter/providers/${encodeURIComponent(normalized)}`);
  latestActionJudgment=null;
  renderActionPlayground();
}

async function refreshActionPlayground(){
  const data=await getJson('/api/action-adapter/providers');
  actionProviders=data.providers||[];
  const select=document.getElementById('actionProviderSelect');
  if(select){
    const previous=select.value;
    select.innerHTML=actionProviders.map(provider=>`<option value="${esc(provider.provider_id)}">${esc(provider.title)} · ${esc(provider.domain)}</option>`).join('');
    if(previous && actionProviders.some(provider=>provider.provider_id===previous))select.value=previous;
  }
  await loadActionProvider(select?.value||actionProviders[0]?.provider_id||'browser');
}

async function evaluateActionProvider(){
  const providerId=document.getElementById('actionProviderSelect')?.value||currentActionProvider?.provider_id;
  if(!providerId)throw new Error('Choose an action test provider first.');
  const result=await sendJson(`/api/action-adapter/providers/${encodeURIComponent(providerId)}/evaluate`);
  currentActionProvider={
    ...(result.provider||{}),
    envelope:result.envelope||{},
  };
  latestActionJudgment=result.judgment||null;
  renderActionPlayground();
}

function renderOverview(data){
  const {status,managedSessions,alertsData,capsulesData,agentRuntimesData,laneStatesData}=data;
  const items=managedSessions?.sessions||[];
  const running=items.filter(x=>x.status==='ready' || x.status==='running');
  const activeSessions=running.filter(x=>(x.connected_transports||0)>0 || x.lifecycle_state==='active');
  const openAlerts=(alertsData.alerts||[]).length;
  const notificationDot=document.querySelector('#notificationBtn i');
  if(notificationDot)notificationDot.hidden=openAlerts===0;
  const notificationButton=document.getElementById('notificationBtn');
  if(notificationButton)notificationButton.setAttribute('aria-label',openAlerts?`Notifications · ${openAlerts} open alert${openAlerts===1?'':'s'}`:'Notifications · no open alerts');
  const runtime=status.studio.status||'unknown';
  const healthy=runtime==='healthy' && openAlerts===0;

  const ledgerCapsules=capsulesData?.capsules||[];
  const liveCapsule=ledgerCapsules.find(x=>x.status==='active')||ledgerCapsules[0]||null;
  const hasLedger=Boolean(liveCapsule);
  const activeSession=activeSessions[0]||running[0]||items[0]||null;
  const capsuleId=hasLedger?liveCapsule.capsule_id:capsuleDisplayId(activeSession);
  const workspace=hasLedger?(liveCapsule.workspace||'—'):(activeSession?.workspace_key||'No active workspace');
  const sourcePane=hasLedger?(liveCapsule.source_pane||'—'):(activeSession?.metadata?.source_pane||'Herdr / ChatGPT');
  const currentAgent=String(hasLedger?(liveCapsule.current_agent||'claude'):'hermes').toLowerCase();
  const currentStage=hasLedger?(liveCapsule.current_stage||'ingress'):'agent_runtime';
  const lastHandoff=hasLedger?liveCapsule.last_handoff:null;
  const pendingHandoff=hasLedger?liveCapsule.pending_handoff:null;
  const routeHandoff=pendingHandoff||lastHandoff;
  const capsuleSummary=capsulesData?.summary||{};
  const runtimeSummary=agentRuntimesData?.summary||{};

  document.getElementById('overviewCards').innerHTML=[
    overviewCard('Active Capsules',String(hasLedger?(capsuleSummary.active||0):(activeSessions.length||running.length)),hasLedger?`${capsuleSummary.total||0} tracked in ledger`:`${running.length} managed sessions`,(capsuleSummary.active||activeSessions.length)?'busy':'healthy'),
    overviewCard('AI Agents Online',String(runtimeSummary.ready??3),`${runtimeSummary.installed??3} installed · Claude · Codex · Hermes`,'healthy'),
    overviewCard('Handoffs Today',String(hasLedger?(capsuleSummary.handoffs||0):Math.max(0,(activeSession?.use_count||0)-1)),hasLedger?'persisted capsule events':'session-derived preview','degraded'),
    overviewCard('Success Rate',healthy?'98%':'Attention',openAlerts?`${openAlerts} open alert${openAlerts===1?'':'s'}`:'control plane healthy',healthy?'healthy':'degraded')
  ].join('');

  const runtimeById=Object.fromEntries((agentRuntimesData?.runtimes||[]).map(r=>[r.id,r]));
  const laneDefs={
    claude:{name:'Claude',sub:runtimeById.claude?.model||'Anthropic',y:44,color:'#f26a2e'},
    codex:{name:'Codex',sub:runtimeById.codex?.model||'OpenAI Codex',y:142,color:'#10a37f'},
    hermes:{name:'Hermes',sub:runtimeById.hermes?.model||'Nous / custom',y:240,color:'#7c3aed'}
  };
  const fromAgent=String(routeHandoff?.from_agent||'').toLowerCase();
  const toAgent=String(routeHandoff?.to_agent||'').toLowerCase();
  const laneStateById=laneStatesData?.lanes||{};
  const lanes=Object.entries(laneDefs).map(([key,meta])=>{
    const participates=key===currentAgent||key===fromAgent||key===toAgent;
    const active=key===currentAgent && liveCapsule?.status!=='completed';
    const laneState=String(laneStateById[key]?.state||'normal').toLowerCase();
    const policyStatus=laneState==='normal'?null:laneState.replaceAll('_',' ').replace(/\b\w/g,ch=>ch.toUpperCase());
    const statusText=policyStatus||(
      pendingHandoff&&key===toAgent?'Awaiting ACK':active&&pendingHandoff&&key===fromAgent?'Running · handoff in flight':active?'Running':key===fromAgent&&lastHandoff?'Handoff source':key===toAgent&&lastHandoff?'Received':'Ready'
    );
    const eventStage=key===fromAgent?(routeHandoff?.from_stage||''):key===toAgent?(routeHandoff?.to_stage||''):'';
    return renderCapsuleLane({
      agent:meta.name,
      sub:meta.sub,
      colorClass:key,
      active,
      activeStage:active?currentStage:eventStage,
      capsuleId:participates?capsuleId:'',
      muted:!participates,
      statusText
    });
  }).join('');

  let connector='';
  if(routeHandoff && laneDefs[fromAgent] && laneDefs[toAgent]){
    const from=laneDefs[fromAgent], to=laneDefs[toAgent];
    const connectorId=routeHandoff.connector_id||capsuleId;
    const fromTop=(from.y/284*100).toFixed(2);
    const toTop=(to.y/284*100).toFixed(2);
    connector=`<svg class="capsule-connector-layer" viewBox="0 0 1000 284" preserveAspectRatio="none" aria-label="${pendingHandoff?'Capsule handoff awaiting ACK':'Capsule handoff committed'} from ${esc(from.name)} to ${esc(to.name)}">
      <defs>
        <linearGradient id="handoffGradientLive" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${from.color}"/><stop offset="100%" stop-color="${to.color}"/></linearGradient>
        <marker id="handoffArrowLive" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="${to.color}"/></marker>
      </defs>
      <path class="handoff-path live-handoff-path" style="stroke:url(#handoffGradientLive)" d="M758 ${from.y} C860 ${from.y}, 858 ${to.y}, 758 ${to.y}" marker-end="url(#handoffArrowLive)"/>
      <circle class="handoff-anchor" style="stroke:${from.color}" cx="758" cy="${from.y}" r="9"/>
      <circle class="handoff-anchor" style="stroke:${to.color}" cx="758" cy="${to.y}" r="9"/>
    </svg>
    <span class="connector-badge connector-from" style="top:calc(${fromTop}% - 12px);background:${from.color}">${esc(connectorId)}</span>
    <span class="connector-badge connector-to" style="top:calc(${toTop}% - 12px);bottom:auto;background:${to.color}">${esc(connectorId)}</span>`;
  }

  document.getElementById('capsulePipeline').innerHTML=`<div class="capsule-pipeline-inner">${lanes}${connector}</div>`;

  const hero=document.getElementById('heroStatus');
  hero.className='capsule-state-card';
  const laneName=laneDefs[currentAgent]?.name||currentAgent;
  const lastText=pendingHandoff?`${laneDefs[fromAgent]?.name||fromAgent} → ${laneDefs[toAgent]?.name||toAgent} · ${pendingHandoff.state||'pending'}`:lastHandoff?`${laneDefs[fromAgent]?.name||fromAgent} → ${laneDefs[toAgent]?.name||toAgent} · committed`:'No handoff yet';
  const nextFallback=currentAgent==='claude'?'Codex / Hermes':currentAgent==='codex'?'Hermes':'Codex';
  const stageLabel=String(currentStage||'').replaceAll('_',' ');
  hero.innerHTML=`<div class="capsule-card-heading compact"><div><span class="capsule-active-label">Active Capsule</span><h3>${esc(capsuleId)}</h3></div><span class="state-live-dot">${liveCapsule?.status==='completed'?'✓ Completed':'● Live'}</span></div>
    ${hasLedger?renderCapsuleContextBar(liveCapsule):''}
    <div class="capsule-state-list">
      <div><span>Task</span><strong>${esc(hasLedger?liveCapsule.title:(activeSession?.name||'Workspace capsule preview'))}</strong></div>
      <div><span>Workspace</span><strong>${esc(workspace)}</strong></div>
      <div><span>Current lane</span><strong class="state-${esc(currentAgent)}"><i></i>${esc(laneName)}</strong></div>
      <div><span>Current stage</span><strong>${esc(stageLabel)}</strong></div>
      <div><span>Source</span><strong>${esc(sourcePane)}</strong></div>
      <div><span>Last handoff</span><strong>${esc(lastText)}</strong></div>
      ${routeHandoff?.a2a?.task_id?`<div><span>A2A task</span><strong>${esc(routeHandoff.a2a.task_id)} · ${esc(routeHandoff.a2a.task?.status?.state||routeHandoff.state||'pending')}</strong></div>`:pendingHandoff?.a2a_task_id?`<div><span>A2A task</span><strong>${esc(pendingHandoff.a2a_task_id)} · ${esc(pendingHandoff.state||'pending')}</strong></div>`:''}
      ${pendingHandoff?`<div><span>Handoff state</span><strong>${esc(pendingHandoff.state||'pending')} · owner remains ${esc(laneName)}</strong></div>`:''}
      <div><span>Next fallback</span><strong>${esc(nextFallback)}</strong></div>
    </div>
    <button class="button capsule-detail-button" data-go-view="sessions" data-capsule-id="${esc(capsuleId)}"${!hasLedger&&activeSession?.id?` data-managed-session-id="${esc(activeSession.id)}"`:'' } type="button">View capsule details →</button>`;

  if(hasLedger){
    const ev=[...(liveCapsule.events||[])].reverse().slice(0,6);
    document.getElementById('activeSessionsPanel').innerHTML=ev.length?`<div class="capsule-event-list">${ev.map(e=>{
      const d=e.data||{};
      const isHandoff=e.kind.startsWith('capsule.handoff');
      const detail=isHandoff?`${d.from_agent||'?'} → ${d.to_agent||'?'} · ${e.kind.replace('capsule.handoff_','').replace('capsule.handoff','committed')}`:e.kind==='capsule.stage'?`Stage → ${d.stage}`:e.kind==='capsule.completed'?'Capsule completed':'Capsule created';
      const cls=isHandoff?'event-handoff':'event-success';
      return `<div class="capsule-event-row"><span class="event-time">${esc(ago(e.created_at))}</span><b>${esc(capsuleId)}</b><span>${esc(detail)}</span><em class="${cls}">${esc(e.kind.replace('capsule.',''))}</em></div>`;
    }).join('')}</div>`:'<div class="empty good">No capsule events yet.</div>';
  }else{
    const sorted=[...running].sort((a,b)=>new Date(b.last_used_at||b.updated_at||0)-new Date(a.last_used_at||a.updated_at||0)).slice(0,5);
    document.getElementById('activeSessionsPanel').innerHTML=sorted.length?`<div class="capsule-event-list">${sorted.map((x)=>{
      const id=capsuleDisplayId(x); const last=x.last_used_at||x.updated_at||x.last_started_at;
      return `<div class="capsule-event-row"><span class="event-time">${esc(ago(last))}</span><b>${id}</b><span>Managed session ready</span><em class="event-success">preview</em></div>`;
    }).join('')}</div>`:'<div class="empty good">No active capsules yet. Open a workspace from ChatGPT to start the flow.</div>';
  }

  renderAgentLanes(data);
}

function focusCapsuleLedgerItem(capsuleId){
  if(!capsuleId)return false;
  const item=[...document.querySelectorAll('.capsule-ledger-item[data-capsule-id]')]
    .find(el=>el.dataset.capsuleId===String(capsuleId));
  if(!item)return false;
  document.querySelectorAll('.capsule-ledger-item[open]').forEach(el=>{if(el!==item)el.open=false;});
  item.open=true;
  item.scrollIntoView({behavior:'smooth',block:'center'});
  return true;
}

function renderCapsuleLedger(data){
  const panel=document.getElementById('capsuleLedgerPanel');
  const summaryEl=document.getElementById('capsuleLedgerSummary');
  if(!panel||!summaryEl) return;
  const payload=data?.capsulesData||{capsules:[],summary:{}};
  const capsules=payload.capsules||[];
  const summary=payload.summary||{};
  summaryEl.innerHTML=`<span class="mini-stat primary">Active <strong>${esc(summary.active||0)}</strong></span><span class="mini-stat">Tracked <strong>${esc(summary.total||0)}</strong></span><span class="mini-stat">Handoffs <strong>${esc(summary.handoffs||0)}</strong></span>`;
  if(!capsules.length){
    panel.innerHTML='<div class="panel-card empty good">No persisted capsules yet. A capsule appears here as soon as the broker creates one.</div>';
    return;
  }
  panel.innerHTML=capsules.map((capsule,index)=>{
    const handoffs=capsule.handoffs||[];
    const pending=capsule.pending_handoff||null;
    const events=[...(capsule.events||[])].reverse();
    const current=String(capsule.current_agent||'unknown').toLowerCase();
    const last=capsule.last_handoff||null;
    const route=pending?`${pending.from_agent} → ${pending.to_agent} · ${pending.state||'pending'}`:last?`${last.from_agent} → ${last.to_agent} · committed`:`${current} · no handoff yet`;
    const pendingRow=pending?`<div class="capsule-trace-row"><span class="trace-connector">${esc(pending.connector_id||capsule.capsule_id)}</span><div><strong>${esc(pending.from_agent)} → ${esc(pending.to_agent)}</strong><small>IN FLIGHT · ${esc(pending.state||'pending')} · ownership remains ${esc(current)}</small></div><code>${esc(pending.handoff_id||'—')}</code></div>`:'';
    const committedRows=handoffs.length?handoffs.slice().reverse().map(h=>`<div class="capsule-trace-row">
      <span class="trace-connector">${esc(h.connector_id||capsule.capsule_id)}</span>
      <div><strong>${esc(h.from_agent)} → ${esc(h.to_agent)}</strong><small>${esc(h.reason||'manual')} · ${esc(when(h.created_at))}${h.a2a?.task_id?` · A2A ${esc(h.a2a.task_id)}`:''}</small></div>
      <code>${esc(h.handoff_id||'—')}</code>
    </div>`).join(''):'';
    const handoffRows=pendingRow||committedRows?`${pendingRow}${committedRows}`:'<div class="empty compact">No handoff recorded.</div>';
    const timeline=events.length?events.map(e=>{
      const d=e.data||{};
      let text=e.message||e.kind;
      if(e.kind==='capsule.stage') text=`Stage → ${d.stage||'unknown'}`;
      if(e.kind==='capsule.handoff') text=`Legacy committed ${d.from_agent||'?'} → ${d.to_agent||'?'} · ${d.reason||'handoff'}`;
      if(e.kind==='capsule.handoff_requested') text=`Requested ${d.from_agent||'?'} → ${d.to_agent||'?'}`;
      if(e.kind==='capsule.handoff_dispatched') text=`Dispatched to ${d.to_agent||'?'} · awaiting ACK`;
      if(e.kind==='capsule.handoff_acknowledged') text=`ACK received from ${d.to_agent||'?'}`;
      if(e.kind==='capsule.handoff_committed') text=`Committed ${d.from_agent||'?'} → ${d.to_agent||'?'}`;
      if(e.kind==='capsule.handoff_failed') text=`Delivery failed ${d.from_agent||'?'} → ${d.to_agent||'?'} · ${d.error||d.reason||'failure'}`;
      if(e.kind==='capsule.handoff_dispatch_uncertain') text=`Dispatch uncertain ${d.from_agent||'?'} → ${d.to_agent||'?'}`;
      if(e.kind==='capsule.handoff_blocked') text=`Blocked ${d.from_agent||'?'} → ${d.to_agent||'?'} · ${d.reason||'context fit'}`;
      if(e.kind==='capsule.completed') text='Capsule completed';
      return `<div class="capsule-timeline-row"><span class="timeline-dot ${esc(e.kind.replace('capsule.',''))}"></span><div><strong>${esc(text)}</strong><small>${esc(when(e.created_at))} · ${esc(e.kind)}</small></div></div>`;
    }).join(''):'<div class="empty compact">No events recorded.</div>';
    return `<details class="capsule-ledger-item ${esc(current)}" data-capsule-id="${esc(capsule.capsule_id)}" ${index===0?'open':''}>
      <summary>
        <span class="capsule-ledger-id">${esc(capsule.capsule_id)}</span>
        <div class="capsule-ledger-title"><strong>${esc(capsule.title||capsule.capsule_id)}</strong><small>${esc(capsule.workspace||'No workspace')} · ${esc(route)}</small></div>
        <span class="capsule-ledger-stage">${esc(String(capsule.current_stage||'unknown').replaceAll('_',' '))}</span>
        <span class="capsule-ledger-status ${esc(capsule.status||'active')}">${esc(capsule.status||'active')}</span>
      </summary>
      <div class="capsule-ledger-body">
        ${renderCapsuleContextBar(capsule,{compact:true})}
        <div class="capsule-ledger-facts">
          <div><span>Current lane</span><strong class="state-${esc(current)}"><i></i>${esc(current)}</strong></div>
          <div><span>Source pane</span><strong>${esc(capsule.source_pane||'—')}</strong></div>
          <div><span>Created</span><strong>${esc(when(capsule.created_at))}</strong></div>
          <div><span>Updated</span><strong>${esc(when(capsule.updated_at))}</strong></div>
        </div>
        <div class="capsule-ledger-columns">
          <section><div class="capsule-subhead"><span>CONNECTORS</span><strong>${esc(handoffs.length)}</strong></div>${handoffRows}</section>
          <section><div class="capsule-subhead"><span>TIMELINE</span><strong>${esc(events.length)}</strong></div><div class="capsule-timeline">${timeline}</div></section>
        </div>
      </div>
    </details>`;
  }).join('');
}

function renderManagedSessions(data){
  const managed=data.managedSessions||{sessions:[],status:{}}, workspaces=data.managedWorkspaces||{workspaces:[]};
  const items=managed.sessions||[], ws=workspaces.workspaces||[], status=managed.status||{};
  const active=items.filter(x=>x.lifecycle_state==='active').length;
  const idle=items.filter(x=>x.lifecycle_state==='idle').length;
  const stopped=items.filter(x=>x.status==='stopped').length;
  const errors=items.filter(x=>x.status==='error').length;
  const contextWarnings=items.filter(x=>x.context_usage?.recommended).length;
  document.getElementById('managedSessionSummary').innerHTML=`<span class="mini-stat primary">Active <strong>${active}</strong></span><span class="mini-stat">Idle <strong>${idle}</strong></span><span class="mini-stat">Errors <strong>${errors}</strong></span>${contextWarnings?`<span class="mini-stat warning">Context alerts <strong>${contextWarnings}</strong></span>`:''}${(status.cutover?.unbound_transports??0)?`<span class="mini-stat warning">Unbound clients <strong>${status.cutover.unbound_transports}</strong></span>`:''}`;
  const select=document.getElementById('managedSessionWorkspace');
  const old=select.value;
  select.innerHTML=ws.map(w=>`<option value="${esc(w.key)}">${esc(w.name||w.key)} · ${esc(w.project_path)}</option>`).join('')||'<option value="">Register a workspace first</option>';
  if(old && ws.some(w=>w.key===old)) select.value=old;
  document.getElementById('newManagedSessionBtn').disabled=!status.enabled||!ws.length;
  document.getElementById('registerWorkspaceBtn').disabled=!status.enabled;
  const toggle=document.getElementById('managedHistoryToggle');
  toggle.textContent=showManagedHistory?'Hide stopped':`Show stopped (${stopped})`;
  const visible=items.filter(x=>showManagedHistory || x.status!=='stopped');
  document.getElementById('managedSessionsPanel').innerHTML=visible.length?`<div class="managed-session-row header"><span>SESSION</span><span>PINNED PROJECT</span><span>STATE</span><span>ACTIVITY</span><span>ACTIONS</span></div>${visible.map(x=>{
    const lifecycle=x.lifecycle_state||x.status;
    const providers=(x.ingress_providers||[]).join(', ')||'—';
    const last=x.last_used_at||x.last_transport_seen_at||x.last_started_at||x.updated_at;
    const ctx=x.context_usage||{};
    const ctxPct=ctx.telemetry_available?Number(ctx.context_usage_percent):null;
    const ctxClass=!ctx.telemetry_available?'unknown':ctx.urgency==='critical'?'critical':ctx.urgency==='recommended'?'warning':'ok';
    const ctxText=ctxPct==null?'Context telemetry unavailable':`Context ${ctxPct.toFixed(1)}% · ${ctx.urgency==='critical'?'rollover now':ctx.urgency==='recommended'?'prepare rollover':'healthy'}`;
    const resume=x.status==='stopped'?`<button class="button small" data-managed-resume="${esc(x.id)}" type="button">Resume</button>`:`<button class="button secondary small" data-managed-restart="${esc(x.id)}" type="button" ${x.connected_transports?'disabled':''}>Restart</button>`;
    const stop=x.status!=='stopped'?`<button class="button danger small" data-managed-stop="${esc(x.id)}" type="button" ${x.connected_transports?'disabled':''}>Stop</button>`:'';
    const rollover=ctx.recommended&&ctx.gateway_session_id?`<button class="button small context-rollover-button ${ctx.urgency==='critical'?'critical':''}" data-managed-rollover="${esc(ctx.gateway_session_id)}" data-managed-rollover-name="${esc(x.name)}" data-managed-rollover-workspace="${esc(x.workspace_key)}" type="button">${ctx.urgency==='critical'?'Rollover now':'Prepare rollover'}</button>`:'';
    return `<div class="managed-session-row"><div><strong>${esc(x.name)}</strong><small>${esc(shortId(x.id,18))}</small><span class="project-pin">🔒 PINNED</span></div><div><strong>${esc(x.workspace_key)}</strong><small>${esc(x.project_path)}</small></div><div>${badge(lifecycle)}<small>${esc(x.status)} · port ${esc(x.port||'—')}</small></div><div><strong>${esc(x.connected_transports||0)} transport${x.connected_transports===1?'':'s'}</strong><small>${esc(providers)} · ${esc(ago(last))}</small><span class="context-usage ${ctxClass}">${esc(ctxText)}</span></div><div class="managed-actions">${rollover}${resume}<button class="button secondary small" data-managed-rename="${esc(x.id)}" data-managed-name="${esc(x.name)}" type="button">Rename</button><button class="text-button" data-managed-history="${esc(x.id)}" type="button">History</button>${stop}</div></div>`;
  }).join('')}`:`<div class="empty ${status.enabled?'':'good'}">${status.enabled?'No managed sessions in this view.':'Managed session isolation is disabled in config.'}</div>`;
  document.getElementById('managedWorkspacesPanel').innerHTML=ws.length?`<div class="workspace-reference-table">
    <div class="workspace-reference-row header"><span>Name</span><span>Path</span><span>Active Sessions</span><span>Status</span><span>Actions</span></div>
    ${ws.map(w=>{
      const sessionCount=items.filter(x=>x.workspace_key===w.key).length;
      const activeCount=items.filter(x=>x.workspace_key===w.key&&x.status!=='stopped').length;
      const chatCommand=workspaceChatGPTCommand(w.key);
      return `<div class="workspace-reference-row">
        <div class="workspace-reference-name"><span class="workspace-reference-icon">◇</span><strong>${esc(w.name||w.key)}</strong></div>
        <code>${esc(w.project_path)}</code>
        <strong>${activeCount}</strong>
        <span class="workspace-reference-status ${activeCount?'active':'idle'}"><i></i>${activeCount?'Active':'Idle'}</span>
        <div class="workspace-reference-actions"><button class="button secondary small" data-go-view="sessions" type="button">Open</button><button class="reference-kebab workspace-copy-command" data-copy-command="${esc(chatCommand)}" data-copy-text="Copy ChatGPT command" data-help="Copy the workspace selection command for ChatGPT." type="button" aria-label="Copy ChatGPT command">•••</button></div>
      </div>`;
    }).join('')}
  </div>`:'<div class="empty">No approved workspaces registered.</div>';
  const activeWorkspaceKeys=new Set(items.filter(x=>x.status!=='stopped').map(x=>x.workspace_key));
  const leaseCount=data.workerData?.leases?.length||0;
  document.getElementById('workspaceSummary').innerHTML=`<span class="mini-stat primary">Approved <strong>${ws.length}</strong></span><span class="mini-stat">In use <strong>${activeWorkspaceKeys.size}</strong></span><span class="mini-stat ${leaseCount?'warning':''}">Write leases <strong>${leaseCount}</strong></span>`;
}

function renderManagedHistory(payload){
  const panel=document.getElementById('managedSessionHistoryPanel');
  const session=payload.session||{}; const audit=payload.audit||[]; const transports=payload.transports||[];
  panel.hidden=false;
  panel.innerHTML=`<div class="history-head"><div><span class="eyebrow">SESSION HISTORY</span><h3>${esc(session.name||session.id)}</h3><p>${esc(session.workspace_key||'')} · ${esc(session.project_path||'')}</p></div><button class="text-button" data-managed-history-close type="button">Close</button></div><div class="history-grid"><div><h4>Lifecycle</h4>${audit.length?audit.slice(0,30).map(a=>`<div class="history-item"><strong>${esc(a.action)}</strong><small>${esc(ago(a.created_at))} · ${esc(a.actor||'system')}</small></div>`).join(''):'<div class="empty">No lifecycle audit yet.</div>'}</div><div><h4>Transport history</h4>${transports.length?transports.slice(0,30).map(t=>`<div class="history-item"><strong>${esc(t.ingress_provider||t.last_tunnel_id||'direct')}</strong><small>${esc(t.status)} · ${esc(ago(t.last_seen_at))} · ${esc(shortId(t.id,14))}</small></div>`).join(''):'<div class="empty">No transport history yet.</div>'}</div></div>`;
  panel.scrollIntoView({behavior:'smooth',block:'nearest'});
}

function renderSessions(data){
  const {status,sessions,gatewaySessions}=data; const tnames=tunnelNameMap(status); const logicalById=logicalMap(sessions); const managedItems=data.managedSessions?.sessions||[]; const managedById=Object.fromEntries(managedItems.map(x=>[x.id,x]));
  const filter=document.getElementById('sessionTunnelFilter');
  const previous=sessionTunnelFilter;
  filter.innerHTML=`<option value="">All ingress</option>${(status.tunnels||[]).map(t=>`<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('')}<option value="__unattributed__">Direct / unattributed</option>`;
  filter.value=previous;
  let all=gatewaySessions.sessions||[];
  const connected=all.filter(s=>s.status==='connected').length;
  const stale=all.filter(s=>s.status==='stale').length;
  const closed=all.filter(s=>s.status==='closed').length;
  document.getElementById('sessionSummary').innerHTML=`<span class="mini-stat">Connected <strong>${connected}</strong></span><span class="mini-stat">Historical <strong>${stale+closed}</strong></span><span class="mini-stat">Reconnects <strong>${gatewaySessions.summary?.reconnects||0}</strong></span>`;
  let visible=all.filter(s=>{
    const tunnelMatch=!sessionTunnelFilter || (sessionTunnelFilter==='__unattributed__' ? !s.last_tunnel_id : s.last_tunnel_id===sessionTunnelFilter);
    const stateMatch=showSessionHistory || s.status==='connected';
    return tunnelMatch&&stateMatch;
  });
  visible.sort((a,b)=>new Date(b.last_seen_at||b.created_at)-new Date(a.last_seen_at||a.created_at));
  document.getElementById('sessionHistoryToggle').textContent=showSessionHistory?'Hide history':`Show history (${stale+closed})`;
  document.getElementById('sessionsPanel').innerHTML=visible.length?visible.map((g,i)=>{
    const logical=logicalById[g.studio_session_id]; const tunnel=tnames[g.last_tunnel_id]||g.last_tunnel_id||'Direct / unattributed';
    const bound=managedById[g.managed_session_id]; const options=managedItems.filter(x=>x.status==='ready').map(x=>`<option value="${esc(x.id)}" ${x.id===g.managed_session_id?'selected':''}>${esc(x.name)} · ${esc(x.workspace_key)}</option>`).join('');
    const bindControls=g.status==='connected'?`<div class="transport-bind">${badge('healthy')}<select data-gateway-select="${esc(g.id)}"><option value="">Choose project session…</option>${options}</select><button class="button secondary small" data-gateway-attach="${esc(g.id)}" type="button">Attach</button>${g.managed_session_id?`<button class="text-button" data-gateway-detach="${esc(g.id)}" type="button">Unbind</button>`:''}</div>`:badge(g.status==='stale'?'degraded':g.status);
    return `<div class="data-row"><div class="row-main"><strong>${esc(clientLabel(g,logical,i))}</strong><small>${esc(g.server_id||'MCP')} · last seen ${esc(ago(g.last_seen_at))}</small>${bound?`<span class="project-pin">🔒 ${esc(bound.workspace_key)}</span>`:''}${detailBlock([['gateway session',g.id],['studio session',g.studio_session_id],['managed session',g.managed_session_id||'—'],['generation',g.generation],['reconnects',g.reconnect_count],['attribution',g.attribution_method||'—']])}</div><div class="row-secondary">${esc(tunnel)}<small>${esc(g.ingress_host||'—')}${g.ingress_path?esc(g.ingress_path):''}</small></div>${bindControls}</div>`;
  }).join(''):'<div class="empty">No sessions match this view.</div>';
}

function renderWorkspaces(data){
  const {workerData}=data; const ws=activeWorkspaces(workerData);
  document.getElementById('workspacesPanel').innerHTML=ws.length?`<div class="workspace-row header"><span>WORKSPACE</span><span>WORKER</span><span>STATE</span><span>AGENT / PANE</span><span>OWNERSHIP</span></div>${ws.map(({worker:w,lease})=>`<div class="workspace-row"><div class="row-main"><strong>${esc(w.workspace)}</strong><small>${esc(w.owner_session_id||'')}</small></div><strong>${esc(w.id)}</strong>${workerBadge(w.state)}<div>${esc(w.agent||'—')}<small>${esc(w.pane||'—')}</small></div><div>${lease?'<span class="lease write">WRITE ACTIVE</span>':(w.lease_mode==='write'?'<span class="lease">WRITE INTENT</span>':'<span class="lease">BOUND</span>')}<small>${lease?esc(lease.work_id||'manual writer'):'no active writer'}</small></div></div>`).join('')}`:'<div class="empty">No workspace bindings.</div>';
  document.getElementById('leasesPanel').innerHTML=(workerData.leases||[]).length?(workerData.leases||[]).map(l=>`<div class="lease-row"><div><strong>${esc(l.workspace)}</strong><small>${esc(l.worker_id)} · ${esc(l.work_id||'manual')}</small></div><span class="lease write">WRITE ACTIVE</span></div>`).join(''):'<div class="empty good">No active write leases.</div>';
}

function renderWorkers(data){
  const {workerData,workData}=data; const leaseByWorker=Object.fromEntries((workerData.leases||[]).map(l=>[l.worker_id,l]));
  document.getElementById('workersPanel').innerHTML=(workerData.workers||[]).length?`<div class="worker-row header"><span>WORKER</span><span>STATE</span><span>WORKSPACE</span><span>AGENT / PANE</span><span>LEASE</span><span>WORK</span></div>${workerData.workers.map(w=>{const lease=leaseByWorker[w.id];return `<div class="worker-row"><div><strong>${esc(w.id)}</strong><small>${esc(w.server_id||'')}</small></div>${workerBadge(w.state)}<div><strong>${esc(w.workspace||'Unbound')}</strong></div><div>${esc(w.agent||'—')}<small>${esc(w.pane||'—')}</small></div><div>${lease?'<span class="lease write">WRITE ACTIVE</span>':(w.lease_mode==='write'&&w.workspace?'<span class="lease">WRITE INTENT</span>':'<span class="lease">NONE</span>')}</div><div>${esc(w.work_label||'—')}</div></div>`}).join('')}`:'<div class="empty">Worker pool not bootstrapped.</div>';
  const live=(workData.work||[]).filter(x=>['running','queued','assigned','dispatching','waiting_agent','reconnecting','recovering'].includes(x.state));
  document.getElementById('workPanel').innerHTML=live.length?`<div class="work-row header"><span>WORK</span><span>STATE</span><span>PRIORITY</span><span>WORKSPACE</span><span>WORKER</span><span>AGENT / PANE</span></div>${live.map(x=>`<div class="work-row"><div><strong>${esc(x.label||x.id)}</strong><small>${esc(shortId(x.id,14))}</small></div>${badge(x.state)}<strong>${esc(x.priority)}</strong><div><strong>${esc(x.workspace||'—')}</strong></div><div>${esc(x.worker_id||'QUEUE')}</div><div>${esc(x.agent||'—')}<small>${esc(x.pane||x.requested_pane||'—')}</small></div></div>`).join('')}`:'<div class="empty good">No queued or running work.</div>';
}

function renderTunnels(data){
  const {status}=data; const tmap=status.tunnel_sessions?.by_tunnel||{};
  document.getElementById('tunnelsPanel').innerHTML=(status.tunnels||[]).length?(status.tunnels||[]).map(t=>{const ts=tmap[t.id]||{counts:{},total:0,reconnects:0};return `<article class="tunnel-card"><div class="tunnel-head"><div><h3>${esc(t.name)}</h3><small>${esc(t.provider)} · ${esc(t.managed?'managed':'external/direct')}</small></div>${badge(t.status||'unknown')}</div><div class="tunnel-metrics"><div><span>CONNECTED</span><strong>${esc(ts.counts?.connected||0)}</strong></div><div><span>SESSIONS</span><strong>${esc(ts.total||0)}</strong></div><div><span>RECONNECTS</span><strong>${esc(ts.reconnects||0)}</strong></div></div><div class="endpoint">${esc(t.endpoint||t.origin||'—')}</div><button class="text-button" data-tunnel-session="${esc(t.id)}" type="button">View sessions →</button>${detailBlock([['tunnel id',t.id],['desired state',t.desired_state||'—'],['server id',t.metadata?.server_id||'—']])}</article>`}).join(''):'<div class="empty">No tunnels configured.</div>';
  const conn=status.connectivity||{}; const counts=conn.summary?.counts||{};
  document.getElementById('connectivityPanel').innerHTML=`<div class="keyline"><span>Status</span>${badge(conn.status||'unknown')}</div><div class="keyline"><span>Healthy tunnels</span><strong>${esc(counts.healthy||0)} / ${esc(conn.summary?.total||0)}</strong></div><div class="keyline"><span>Auto reconnect</span><strong>${status.studio.connectivity_auto_reconnect?'Enabled':'Disabled'}</strong></div><div class="keyline"><span>Public MCP gateway</span><strong>${status.studio.gateway_enabled?'Enabled':'Disabled'}</strong></div><div class="keyline"><span>Last poll</span><small>${esc(when(conn.last_poll_at))}</small></div>${conn.last_error?`<div class="warning">${esc(conn.last_error)}</div>`:''}`;
}

function renderReliability(data){
  const {observabilityData,operationsData,alertsData}=data; const slo=observabilityData||{}, m=slo.metrics||{}, objectives=slo.objectives||{}, restore=slo.restore_drill||{}; const fmt=(v,s='')=>v==null?'—':`${v}${s}`;
  document.getElementById('sloPanel').innerHTML=`<div class="metrics"><div><span>Availability</span><strong>${fmt(m.availability_percent,'%')}</strong></div><div><span>MCP success</span><strong>${fmt(m.mcp_success_percent,'%')}</strong></div><div><span>MCP P95 latency</span><strong>${fmt(m.mcp_p95_latency_ms,' ms')}</strong></div><div><span>Queue P95</span><strong>${fmt(m.queue_p95_ms,' ms')}</strong></div><div><span>Worker saturation</span><strong>${fmt(m.worker_saturation_current_percent,'%')}</strong></div><div><span>Reconnect / 100</span><strong>${fmt(m.reconnect_rate_per_100_requests)}</strong></div></div><div class="keyline"><span>Restore drill</span><div>${badge(restore.status==='pass'?'healthy':restore.status==='fail'?'down':'unknown')}<small>${esc(when(restore.created_at))}</small></div></div>${Object.entries(objectives).map(([name,x])=>`<div class="slo-row"><div><strong>${esc(name.replaceAll('_',' ').toUpperCase())}</strong><small>${esc(x.message||'')}</small></div><div><strong>${esc(fmt(x.value))}</strong><small>target ${esc(fmt(x.target))}</small></div><div><strong>${x.error_budget_remaining_percent==null?'—':esc(x.error_budget_remaining_percent+'%')}</strong><small>budget left</small></div>${badge(x.state||'unknown')}</div>`).join('')||'<div class="empty">Waiting for SLO samples.</div>'}`;
  const ops=operationsData.supervisor||{}, sum=operationsData.summary||{}, schema=operationsData.schema||{}, alerts=alertsData.alerts||[];
  document.getElementById('operationsPanel').innerHTML=`<div class="metrics"><div><span>Supervisor</span><strong>${esc((ops.status||'unknown').toUpperCase())}</strong></div><div><span>Cancel pending</span><strong>${esc(sum.cancel_pending||0)}</strong></div><div><span>Detached</span><strong>${esc(sum.detached||0)}</strong></div><div><span>Open alerts</span><strong>${esc(alerts.length)}</strong></div><div><span>Schema</span><strong>v${esc(schema.current_version||0)}</strong></div><div><span>DB integrity</span><strong>${esc(schema.integrity||'unknown')}</strong></div></div>${alerts.length?alerts.slice(0,8).map(a=>`<div class="data-row"><div class="row-main"><strong>${esc(a.message||a.kind)}</strong><small>${esc(when(a.created_at))}</small></div><div class="row-secondary">${esc(a.kind||'operations')}</div>${badge(a.severity==='error'||a.severity==='critical'?'down':'degraded')}</div>`).join(''):'<div class="empty good">No open operational alerts.</div>'}`;
}

function renderActivity(data){
  const {events,auditData}=data;
  document.getElementById('eventsPanel').innerHTML=(events.events||[]).length?(events.events||[]).slice(0,40).map(e=>`<div class="event-row"><span>${esc(when(e.created_at))}</span><span class="severity ${esc(e.severity)}">${esc(e.severity)}</span><strong>${esc(e.kind)}</strong><span>${esc(e.message)}</span></div>`).join(''):'<div class="empty">No events yet.</div>';
  document.getElementById('auditPanel').innerHTML=(auditData.audit||[]).length?(auditData.audit||[]).slice(0,30).map(a=>`<div class="data-row"><div class="row-main"><strong>${esc(a.action)}</strong><small>${esc(a.actor)} · ${esc(when(a.created_at))}</small></div><div class="row-secondary">${esc(a.target_type||'—')}<small>${esc(a.target_id||'')}</small></div>${badge(a.outcome==='success'?'healthy':'degraded')}</div>`).join(''):'<div class="empty">No audit records.</div>';
}

function renderDebug(data){
  const {status,openaiCompat}=data; const h=status.herdr||{}, ex=status.execution||{}, telemetry=status.telemetry||{};
  document.getElementById('executionPanel').innerHTML=`<div class="keyline"><span>Status</span>${badge(ex.status||'unknown')}</div><div class="keyline"><span>Automatic dispatch</span><strong>${status.studio.execution_enabled?'Enabled':'Disabled'}</strong></div><div class="keyline"><span>Parallelism</span><strong>${esc(status.studio.execution_parallelism??ex.parallelism??1)}</strong></div><div class="keyline"><span>Active / peak dispatch</span><strong>${esc(ex.active_dispatches??0)} / ${esc(ex.peak_parallel_dispatches??0)}</strong></div><div class="keyline"><span>Recoveries</span><strong>${esc(ex.recoveries??0)}</strong></div><div class="keyline"><span>Last tick</span><small>${esc(when(ex.last_tick_at))}</small></div>`;
  document.getElementById('herdrPanel').innerHTML=`<div class="keyline"><span>Status</span>${badge(h.status||'unknown')}</div><div class="keyline"><span>Server</span><code>${esc(h.server_id||'—')}</code></div><div class="keyline"><span>Agents</span><strong>${esc(h.agent_count??'—')}</strong></div><div class="keyline"><span>Panes</span><strong>${esc(h.pane_count??'—')}</strong></div><div class="keyline"><span>Last refresh</span><small>${esc(when(h.last_refreshed_at))}</small></div>`;
  document.getElementById('serverCount').textContent=`${status.servers?.length||0} configured`;
  document.getElementById('serversGrid').innerHTML=(status.servers||[]).map(s=>`<article class="server-card"><div class="server-head"><div><h3>${esc(s.server_name)}</h3><small>${esc(s.server_id)}</small></div>${badge(s.status)}</div><div class="tunnel-metrics"><div><span>TOOLS</span><strong>${esc(s.tool_count)}</strong></div><div><span>SCHEMA</span><strong>${esc(shortId(s.schema_hash,8))}</strong></div><div><span>CHANGED</span><strong>${s.schema_changed?'YES':'NO'}</strong></div></div><div>${(s.layers||[]).map(l=>`<div class="layer"><span class="dot ${esc(l.status)}"></span><strong>${esc(l.name)}</strong><span>${esc(l.detail||'')}</span></div>`).join('')}</div></article>`).join('')||'<div class="empty">No server data.</div>';
  document.getElementById('telemetryPanel').innerHTML=`<div class="metrics"><div><span>Success</span><strong>${telemetry.success_rate==null?'—':esc(telemetry.success_rate+'%')}</strong></div><div><span>Avg runtime</span><strong>${telemetry.avg_runtime_ms==null?'—':esc(Math.round(telemetry.avg_runtime_ms)+' ms')}</strong></div><div><span>Avg queue wait</span><strong>${telemetry.avg_queue_wait_ms==null?'—':esc(Math.round(telemetry.avg_queue_wait_ms)+' ms')}</strong></div></div>${(telemetry.per_worker||[]).map(x=>`<div class="data-row"><div><strong>${esc(x.worker_id)}</strong><small>${esc(x.total)} total</small></div><div>${esc(x.completed)} done · ${esc(x.failed)} failed<small>${esc(x.recoveries)} recoveries</small></div>${badge(x.failed?'degraded':'healthy')}</div>`).join('')||'<div class="empty">No execution telemetry.</div>'}`;
  const obs=openaiCompat.observations||[];
  document.getElementById('openaiPanel').innerHTML=`<div class="metrics"><div><span>Observed clients</span><strong>${esc(openaiCompat.summary?.observed||0)}</strong></div><div><span>OpenAI-like</span><strong>${esc(openaiCompat.summary?.openai_like||0)}</strong></div><div><span>Connector reclaim</span><strong>${openaiCompat.connector_reclaim_enabled?'Enabled':'Disabled'}</strong></div></div>${obs.slice(0,20).map(o=>`<div class="data-row"><div class="row-main"><strong>${esc(o.client_info_name||'Unknown MCP client')}</strong><small>${esc(o.user_agent||'')}</small></div><div class="row-secondary">${esc(o.identity_scope||'—')}<small>${esc(o.identity_source||'')}</small></div>${badge(o.openai_like?'healthy':'unknown')}</div>`).join('')||'<div class="empty">No client observations.</div>'}`;
}

async function load(){
  try{
    const [status,workerData,workData,sessions,gatewaySessions,managedSessions,managedWorkspaces,openaiCompat,operationsData,observabilityData,alertsData,auditData,events,capsulesData,agentRuntimesData,laneStatesData,reflexMetricsData]=await Promise.all([
      getJson('/api/status'),getJson('/api/workers'),getJson('/api/work?limit=100'),getJson('/api/sessions'),getJson('/api/gateway/sessions'),getJson('/api/managed/sessions'),getJson('/api/managed/workspaces'),getJson('/api/openai/compatibility'),getJson('/api/operations'),getJson('/api/observability'),getJson('/api/alerts?status=open&limit=20'),getJson('/api/audit?limit=30'),getJson('/api/events?limit=40'),getJson('/api/capsules?limit=100'),getJson('/api/agents/runtimes'),getJson('/api/lanes'),getJson(`/api/reflex/metrics?window=${encodeURIComponent(reflexWindow)}&recent=20`).catch(()=>null)
    ]);
    latestData={status,workerData,workData,sessions,gatewaySessions,managedSessions,managedWorkspaces,openaiCompat,operationsData,observabilityData,alertsData,auditData,events,capsulesData,agentRuntimesData,laneStatesData,reflexMetricsData};
    const studio=document.getElementById('studioStatus'); studio.className=`pill ${status.studio.status}`; studio.textContent=String(status.studio.status||'unknown').toUpperCase();
    document.getElementById('lastUpdated').textContent=`Updated ${new Date().toLocaleTimeString()} · ${status.studio.version}`;
    renderOverview(latestData); renderReflexMetrics(latestData); renderCapsuleLedger(latestData); renderManagedSessions(latestData); renderSessions(latestData); renderWorkspaces(latestData); renderWorkers(latestData); renderTunnels(latestData); renderReliability(latestData); renderActivity(latestData); renderDebug(latestData); captureAndTranslateText(document);
  }catch(err){
    const studio=document.getElementById('studioStatus'); studio.className='pill down'; studio.textContent='UI ERROR';
    document.getElementById('lastUpdated').textContent=err.message;
  }
}


function runtimeDetailMessage(runtime){
  if(!runtime)return 'Runtime information is unavailable.';
  const auth=runtime.auth_health||{};
  const limit=runtime.limit_health||{};
  return [
    `Status: ${runtime.status||'unknown'}`,
    `Installed: ${runtime.installed===false?'No':'Yes'}`,
    `Provider: ${runtime.provider||runtime.brand||'—'}`,
    `Model: ${runtime.model||runtime.last_used_model||'—'}`,
    `Version: ${runtime.version||'—'}`,
    `Authentication: ${authHealthLabel(auth)}`,
    `Auth source: ${auth.source||'none'}`,
    `Auth detail: ${auth.detail||'—'}`,
    `Rate limit: ${limitHealthLabel(limit)}`,
    `Limit source: ${limit.source||'none'}`,
    `Limit reset: ${limit.reset_at||'—'}`,
    `Limit detail: ${limit.detail||'—'}`,
    `Active panes: ${runtime.pane_count||0}`,
    `Binary: ${runtime.binary||'not reported'}`
  ].join('\n');
}

async function applyLaneState(id){
  const select=document.querySelector(`[data-lane-state-select="${CSS.escape(String(id))}"]`);
  const state=String(select?.value||'normal').toLowerCase();
  let reason='';
  const autoHandoff=state==='disabled'||state==='emergency';
  if(state!=='normal'){
    const result=await openAppDialog({
      title:`Set ${id} lane to ${state}?`,
      message:autoHandoff
        ? 'New work will stop routing to this lane. Active portable capsules may be handed off automatically; Guarded capsules wait for approval and Pinned capsules stay put.'
        : 'New work will stop routing to this lane, while active work can finish.',
      icon:autoHandoff?'!':'i',
      tone:autoHandoff?'danger':'info',
      confirmText:'Apply lane state',
      fields:[{name:'reason',label:'Reason',placeholder:'quota near limit / maintenance / operator choice'}]
    });
    if(!result.confirmed)return;
    reason=result.values.reason||'';
  }
  await sendJson(`/api/lanes/${encodeURIComponent(id)}/state`,{
    state,
    reason:reason||null,
    actor:'web-ui',
    auto_handoff:autoHandoff
  });
  await load();
}

async function checkAgentRuntime(id){
  const inventory=await getJson('/api/agents/runtimes?refresh=true');
  if(latestData){
    latestData.agentRuntimesData=inventory;
    renderAgentLanes(latestData);
  }
  const runtime=(inventory.runtimes||[]).find(r=>String(r.id).toLowerCase()===String(id).toLowerCase());
  await openAppDialog({
    title:`${runtime?.name||id} runtime check`,
    message:runtimeDetailMessage(runtime),
    icon:runtime&&runtime.installed!==false?'✓':'!',
    tone:runtime&&runtime.installed!==false?'info':'danger',
    confirmText:'Close',
    showCancel:false
  });
}

async function showAgentRuntimeDetails(id){
  const inventory=latestData?.agentRuntimesData||await getJson('/api/agents/runtimes');
  const runtime=(inventory.runtimes||[]).find(r=>String(r.id).toLowerCase()===String(id).toLowerCase());
  await openAppDialog({
    title:`${runtime?.name||id} details`,
    message:runtimeDetailMessage(runtime),
    icon:'i',
    tone:'info',
    confirmText:'Close',
    showCancel:false
  });
}

async function handleSettingsAction(action){
  if(action==='appearance'){
    document.querySelector('.reference-settings-content')?.scrollIntoView({behavior:'smooth',block:'start'});
    return;
  }
  if(['agents','sessions','workspaces','computer'].includes(action)){
    setView(action);
    return;
  }
  if(action==='system-status'){
    setView('system');
    return;
  }
  if(action==='about'){
    const status=latestData?.status?.studio||{};
    await openAppDialog({
      title:'About HIRDA',
      message:`HIRDA · Multi-Agent Orchestration\nVersion: ${status.version||'—'}\nStatus: ${status.status||'unknown'}`,
      icon:'H',
      tone:'info',
      confirmText:'Close',
      showCancel:false
    });
  }
}

async function showNotifications(){
  const alertsData=latestData?.alertsData||await getJson('/api/alerts?status=open&limit=20');
  const alerts=alertsData?.alerts||[];
  const message=alerts.length
    ? alerts.slice(0,6).map(a=>`• ${String(a.severity||'info').toUpperCase()} · ${a.message||a.kind||'Alert'}`).join('\n')
    : 'No open alerts.';
  await openAppDialog({
    title:alerts.length?`Open alerts (${alerts.length})`:'Notifications',
    message,
    icon:alerts.length?'!':'✓',
    tone:alerts.length?'danger':'info',
    confirmText:'Close',
    showCancel:false
  });
}

async function runGlobalSearch(raw){
  const query=String(raw||'').trim().toLowerCase();
  if(!query)return;
  const routes=[
    [/(dashboard|home|overview)/,'home'],
    [/(capsule|session|handoff)/,'sessions'],
    [/(agent|claude|codex|hermes)/,'agents'],
    [/(action adapter|action schema|action provider|action playground)/,'actions'],
    [/(reflex|jev|decision engine|risk|teacher|agreement)/,'reflex'],
    [/(workspace|project)/,'workspaces'],
    [/(guide|help|tutorial|how to|quick start)/,'guide'],
    [/(computer|browser|vnc|tool)/,'computer'],
    [/(system|health|slo|tunnel|alert|diagnostic|telemetry)/,'system'],
    [/(setting|theme|appearance|language|font|color scheme)/,'settings']
  ];
  const hit=routes.find(([pattern])=>pattern.test(query));
  if(hit){setView(hit[1]);return;}
  await openAppDialog({
    title:'Search',
    message:`No section matched “${raw}”. Try capsule, agent, Reflex, workspace, guide, computer, or settings.`,
    icon:'⌕',
    tone:'info',
    confirmText:'Close',
    showCancel:false
  });
}

document.querySelectorAll('.primary-nav a[data-view], .mobile-nav a[data-view]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();setView(a.dataset.view);}));
document.addEventListener('click',e=>uiAction(async()=>{
  const copy=e.target.closest('[data-copy-command],[data-copy-source]'); if(copy){await copyChatGPTCommand(copy);return;}
  const settingsAction=e.target.closest('[data-settings-action]'); if(settingsAction){await handleSettingsAction(settingsAction.dataset.settingsAction);return;}
  const scheme=e.target.closest('button[data-color-scheme]'); if(scheme){applyColorScheme(scheme.dataset.colorScheme);return;}
  const laneApply=e.target.closest('[data-lane-state-apply]'); if(laneApply){await applyLaneState(laneApply.dataset.laneStateApply);return;}
  const agentCheck=e.target.closest('[data-agent-check]'); if(agentCheck){await checkAgentRuntime(agentCheck.dataset.agentCheck);return;}
  const agentDetails=e.target.closest('[data-agent-details]'); if(agentDetails){await showAgentRuntimeDetails(agentDetails.dataset.agentDetails);return;}
  const go=e.target.closest('[data-go-view]'); if(go){setView(go.dataset.goView);if(go.dataset.capsuleId){if(latestData)renderCapsuleLedger(latestData);const focused=focusCapsuleLedgerItem(go.dataset.capsuleId);if(!focused&&go.dataset.managedSessionId){renderManagedHistory(await getJson(`/api/managed/sessions/${encodeURIComponent(go.dataset.managedSessionId)}/history`));}}return;}
  const tunnel=e.target.closest('[data-tunnel-session]'); if(tunnel){sessionTunnelFilter=tunnel.dataset.tunnelSession;showSessionHistory=false;setView('sessions');if(latestData)renderSessions(latestData);return;}
  const restart=e.target.closest('[data-managed-restart]'); if(restart){await sendJson(`/api/managed/sessions/${encodeURIComponent(restart.dataset.managedRestart)}/restart`);await load();return;}
  const resume=e.target.closest('[data-managed-resume]'); if(resume){await sendJson(`/api/managed/sessions/${encodeURIComponent(resume.dataset.managedResume)}/resume`);await load();return;}
  const rename=e.target.closest('[data-managed-rename]'); if(rename){
    const result=await openAppDialog({title:'Rename session',message:'Choose a clear name for this pinned project session.',icon:'✎',tone:'info',confirmText:'Rename',fields:[{name:'name',label:'Session name',value:rename.dataset.managedName||'',required:true}]});
    if(result.confirmed&&result.values.name){await sendJson(`/api/managed/sessions/${encodeURIComponent(rename.dataset.managedRename)}/rename`,{name:result.values.name});await load();}
    return;
  }
  const rollover=e.target.closest('[data-managed-rollover]'); if(rollover){
    const gatewayId=rollover.dataset.managedRollover;
    const name=rollover.dataset.managedRolloverName||'managed session';
    const workspace=rollover.dataset.managedRolloverWorkspace||'';
    const result=await openAppDialog({
      title:'Prepare context rollover',
      message:'Create a bounded handoff packet before moving to a fresh ChatGPT conversation. The current conversation remains authoritative until the token is claimed.',
      icon:'↗',tone:'info',confirmText:'Prepare rollover',
      fields:[{name:'summary',label:'Handoff summary',type:'textarea',required:true,value:`Continue ${name}${workspace?` on workspace ${workspace}`:''}. Preserve current decisions, blockers, and next action from this conversation.`}]
    });
    if(!result.confirmed)return;
    const prepared=await sendJson(`/api/gateway/sessions/${encodeURIComponent(gatewayId)}/handoff`,{summary:result.values.summary,reason:'context near full',ttl_seconds:1800});
    const token=prepared.claim_token||'';
    const tokenResult=await openAppDialog({
      title:'Rollover ready',
      message:'Open a fresh ChatGPT conversation, reconnect HIRDA, then claim this one-time token. The old conversation stays attached until the claim succeeds.',
      icon:'✓',tone:'info',confirmText:'Copy token',cancelText:'Close',
      fields:[{name:'claim_token',label:'One-time claim token',value:token,readonly:true}]
    });
    if(tokenResult.confirmed&&token)await copyText(token);
    await load();
    return;
  }
  const history=e.target.closest('[data-managed-history]'); if(history){renderManagedHistory(await getJson(`/api/managed/sessions/${encodeURIComponent(history.dataset.managedHistory)}/history`));return;}
  const historyClose=e.target.closest('[data-managed-history-close]'); if(historyClose){document.getElementById('managedSessionHistoryPanel').hidden=true;return;}
  const stop=e.target.closest('[data-managed-stop]'); if(stop){
    const result=await openAppDialog({title:'Stop isolated Serena session?',message:'The pinned project stays registered and can be resumed later. Connected clients must be detached first.',icon:'!',tone:'danger',confirmText:'Stop session'});
    if(result.confirmed){await sendJson(`/api/managed/sessions/${encodeURIComponent(stop.dataset.managedStop)}/stop`);await load();}
    return;
  }
  const attach=e.target.closest('[data-gateway-attach]'); if(attach){
    const id=attach.dataset.gatewayAttach;
    const sel=document.querySelector(`[data-gateway-select="${CSS.escape(id)}"]`);
    if(!sel?.value){
      await openAppDialog({
        title:'Choose a project session',
        message:'Select a managed project session before attaching this client transport.',
        icon:'!',
        tone:'info',
        confirmText:'Close',
        showCancel:false
      });
      return;
    }
    await sendJson(`/api/gateway/sessions/${encodeURIComponent(id)}/managed/attach`,{managed_session_id:sel.value});
    await load();
    return;
  }
  const detach=e.target.closest('[data-gateway-detach]'); if(detach){await sendJson(`/api/gateway/sessions/${encodeURIComponent(detach.dataset.gatewayDetach)}/managed/detach`);await load();return;}
}));
document.getElementById('languageSelect').addEventListener('change',e=>applyLanguage(e.target.value));
document.getElementById('themeLightBtn').addEventListener('click',()=>applyTheme('light'));
document.getElementById('themeDarkBtn').addEventListener('click',()=>applyTheme('dark'));
document.getElementById('fontDecreaseBtn').addEventListener('click',()=>applyFontScale(fontScaleIndex-1));
document.getElementById('fontResetBtn').addEventListener('click',()=>applyFontScale(1));
document.getElementById('fontIncreaseBtn').addEventListener('click',()=>applyFontScale(fontScaleIndex+1));
document.getElementById('newManagedSessionBtn').addEventListener('click',()=>{document.getElementById('managedSessionForm').hidden=false;document.getElementById('managedSessionName').focus();});
document.getElementById('cancelManagedSessionBtn').addEventListener('click',()=>{document.getElementById('managedSessionForm').hidden=true;});
document.getElementById('managedSessionForm').addEventListener('submit',e=>uiAction(async()=>{e.preventDefault();const name=document.getElementById('managedSessionName').value.trim();const workspace_key=document.getElementById('managedSessionWorkspace').value;if(!name||!workspace_key)return;await sendJson('/api/managed/sessions',{name,workspace_key});document.getElementById('managedSessionName').value='';document.getElementById('managedSessionForm').hidden=true;await load();}));
document.getElementById('registerWorkspaceBtn').addEventListener('click',()=>uiAction(async()=>{
  const result=await openAppDialog({title:'Register workspace',message:'Only approved workspaces can receive isolated project sessions.',icon:'+',tone:'info',confirmText:'Register',fields:[
    {name:'key',label:'Workspace key',placeholder:'oriverse',required:true,pattern:'[A-Za-z0-9._-]+'},
    {name:'project_path',label:'Absolute project path',placeholder:'/data/oriverse',required:true},
    {name:'name',label:'Display name',placeholder:'Oriverse'}
  ]});
  if(!result.confirmed)return;
  const {key,project_path}=result.values;const name=result.values.name||key;
  await sendJson('/api/managed/workspaces',{key,project_path,name});
  await load();
}));
document.getElementById('managedHistoryToggle').addEventListener('click',()=>{showManagedHistory=!showManagedHistory;if(latestData)renderManagedSessions(latestData);});
document.getElementById('sessionTunnelFilter').addEventListener('change',e=>{sessionTunnelFilter=e.target.value||'';if(latestData)renderSessions(latestData);});
document.getElementById('sessionHistoryToggle').addEventListener('click',()=>{showSessionHistory=!showSessionHistory;if(latestData)renderSessions(latestData);});
document.getElementById('reflexWindowSelect')?.addEventListener('change',e=>uiAction(async()=>{reflexWindow=e.target.value||'24h';await refreshReflexMetrics();}));
document.getElementById('reflexRefreshBtn')?.addEventListener('click',()=>uiAction(async()=>{const b=document.getElementById('reflexRefreshBtn');b.disabled=true;b.textContent=tr('Refreshing…');try{await refreshReflexMetrics();}finally{b.disabled=false;b.textContent=tr('Refresh metrics');}}));
document.getElementById('actionProviderSelect')?.addEventListener('change',e=>uiAction(()=>loadActionProvider(e.target.value)));
document.getElementById('actionLoadBtn')?.addEventListener('click',()=>uiAction(async()=>{const b=document.getElementById('actionLoadBtn');b.disabled=true;try{await loadActionProvider();}finally{b.disabled=false;}}));
document.getElementById('actionEvaluateBtn')?.addEventListener('click',()=>uiAction(async()=>{const b=document.getElementById('actionEvaluateBtn');b.disabled=true;b.textContent='Evaluating…';try{await evaluateActionProvider();}finally{b.disabled=false;b.textContent='Evaluate with JEV';}}));
document.getElementById('refreshBtn').addEventListener('click',()=>uiAction(async()=>{const b=document.getElementById('refreshBtn');b.disabled=true;b.textContent=tr('Refreshing…');try{await load();}finally{b.disabled=false;b.textContent=tr('Refresh');}}));
document.getElementById('pollBtn').addEventListener('click',()=>uiAction(async()=>{const b=document.getElementById('pollBtn');b.disabled=true;b.textContent=tr('Polling…');try{await getJson('/api/health/poll',{method:'POST'});await load();}finally{b.disabled=false;b.textContent=tr('Poll health');}}));
document.getElementById('herdrBtn').addEventListener('click',()=>uiAction(async()=>{const b=document.getElementById('herdrBtn');b.disabled=true;b.textContent=tr('Refreshing…');try{await getJson('/api/herdr/refresh',{method:'POST'});await load();}finally{b.disabled=false;b.textContent=tr('Refresh Herdr');}}));
document.getElementById('notificationBtn')?.addEventListener('click',()=>uiAction(showNotifications));
document.getElementById('globalSearch')?.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();uiAction(()=>runGlobalSearch(e.currentTarget.value));}});
window.addEventListener('hashchange',()=>setView((location.hash||'#home').slice(1),false));


const helpTooltip=document.getElementById('helpTooltip');
let helpTarget=null;
function positionHelpTooltip(target){
  if(!helpTooltip||!target||helpTooltip.hidden)return;
  const rect=target.getBoundingClientRect();
  const gap=10;
  const width=helpTooltip.offsetWidth||280;
  const height=helpTooltip.offsetHeight||80;
  let left=rect.left+(rect.width/2)-(width/2);
  left=Math.max(10,Math.min(window.innerWidth-width-10,left));
  let top=rect.bottom+gap;
  if(top+height>window.innerHeight-10) top=rect.top-height-gap;
  top=Math.max(10,top);
  helpTooltip.style.left=`${Math.round(left)}px`;
  helpTooltip.style.top=`${Math.round(top)}px`;
}
function showHelpTooltip(target){
  if(!helpTooltip||!target?.dataset?.help)return;
  helpTarget=target;
  helpTooltip.textContent=target.dataset.help;
  helpTooltip.hidden=false;
  requestAnimationFrame(()=>positionHelpTooltip(target));
}
function hideHelpTooltip(target=null){
  if(!helpTooltip)return;
  if(target&&helpTarget&&target!==helpTarget)return;
  helpTooltip.hidden=true;
  helpTarget=null;
}
document.addEventListener('mouseover',e=>{
  const target=e.target.closest('[data-help]');
  if(target)showHelpTooltip(target);
});
document.addEventListener('mouseout',e=>{
  const target=e.target.closest('[data-help]');
  if(target&&!target.contains(e.relatedTarget))hideHelpTooltip(target);
});
document.addEventListener('focusin',e=>{
  const target=e.target.closest?.('[data-help]');
  if(target)showHelpTooltip(target);
});
document.addEventListener('focusout',e=>{
  const target=e.target.closest?.('[data-help]');
  if(target)hideHelpTooltip(target);
});
window.addEventListener('scroll',()=>hideHelpTooltip(),{passive:true});
window.addEventListener('resize',()=>hideHelpTooltip());

loadTheme();
loadColorScheme();
loadFontScale();
loadLanguage();
setView(currentView,false);
load();
setInterval(load,5000);


/* MCP_STUDIO_COMPUTER_USE_MVP */
const computerUiState={descriptor:null,sessions:[],selectedSessionId:null,status:null};

function computerMessage(text,kind=''){
  const el=document.getElementById('computerMessage');
  if(!el)return;
  el.textContent=text;
  el.dataset.kind=kind;
}

function computerMonitorSvg(){
  return '<svg viewBox="0 0 64 52" aria-hidden="true"><rect x="6" y="5" width="52" height="34" rx="5" fill="none" stroke="currentColor" stroke-width="3"/><path d="M25 46h14M32 39v7" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><circle cx="52" cy="11" r="2" fill="currentColor"/></svg>';
}

function renderComputerSessions(sessions){
  const grid=document.getElementById('computerSessionGrid');
  const count=document.getElementById('computerSessionCount');
  if(!grid)return;
  computerUiState.sessions=sessions;
  if(count)count.textContent=sessions.length+' session'+(sessions.length===1?'':'s');
  if(!sessions.length){
    grid.innerHTML='<div class="computer-session-empty">No managed sessions are available yet.</div>';
    return;
  }
  const accents=['#5f8cff','#9b72ff','#32c48d','#e8a838','#e86f8f','#46b8d8'];
  grid.innerHTML=sessions.map(function(s,index){
    const id=esc(s.id);
    const name=esc(s.workspace_key||s.name||s.id);
    const workspace=esc((s.name&&s.name!==s.workspace_key)?s.name:(s.project_path||'Managed session'));
    const status=esc(s.status||'unknown');
    const runtimeDisplays=(computerUiState.status&&computerUiState.status.runtime_displays)||{};
    const runtimeDisplay=runtimeDisplays[s.id];
    const runtimeLabel=Number.isInteger(runtimeDisplay)?('VNC :'+runtimeDisplay):'VNC unpaired';
    const selected=s.id===computerUiState.selectedSessionId;
    const accent=accents[index%accents.length];
    const permissions=s.tool_permissions||{};
    const permissionBadge=function(key,label){
      const allowed=permissions[key]!==false;
      return '<span class="computer-permission-badge '+(allowed?'allowed':'blocked')+'" title="'+label+': '+(allowed?'allowed':'blocked')+'">'+label+'</span>';
    };
    const permissionRow=s.tool_permissions_enabled===false?'':('<span class="computer-session-permissions">'
      +permissionBadge('read','R')+permissionBadge('write','W')+permissionBadge('execute','X')+permissionBadge('destructive','D')+'</span>');
    return '<button class="computer-session-card'+(selected?' selected':'')+'" style="--session-accent:'+accent+'" type="button" data-computer-session="'+id+'" data-status="'+status+'" aria-pressed="'+(selected?'true':'false')+'">'
      +'<span class="computer-monitor-icon">'+computerMonitorSvg()+'<span class="computer-monitor-glow"></span></span>'
      +'<span class="computer-session-copy"><strong>'+name+'</strong><small>'+workspace+'</small><small class="computer-session-runtime">'+esc(runtimeLabel)+'</small></span>'
      +permissionRow
      +'<span class="computer-session-status"><i></i>'+status+'</span>'
      +'</button>';
  }).join('');
}

function updateComputerStatus(status){
  const pill=document.getElementById('computerStatusPill');
  const bridge=document.getElementById('computerBridgeLine');
  const novnc=document.getElementById('computerNovncLine');
  const token=document.getElementById('computerTokenLine');
  const tokenWrap=document.getElementById('computerTokenWrap');
  const transportReady=status.runtime_mode==='session-isolated'?status.transport_ready:status.websockify_reachable;
  const ready=!!(status.enabled&&status.configured&&transportReady&&status.novnc_available);
  if(pill){pill.textContent=ready?'ready':(status.enabled?'needs setup':'disabled');pill.dataset.state=ready?'ready':'warning';}
  if(bridge)bridge.textContent=transportReady?'online':'offline';
  if(novnc)novnc.textContent=status.novnc_available?'available':'missing';
  if(token)token.textContent=status.auth_required?'yes':'no';
  if(tokenWrap)tokenWrap.hidden=!status.auth_required;
  return ready;
}

async function refreshComputerView(){
  if((location.hash||'#home')!=='#computer')return;
  try{
    const data=await Promise.all([getJson('/api/computer/status'),getJson('/api/managed/sessions')]);
    const status=data[0]||{};
    const sessions=(data[1]&&data[1].sessions)||[];
    computerUiState.status=status;
    const ready=updateComputerStatus(status);
    renderComputerSessions(sessions);
    if(!status.enabled)computerMessage('Computer Use is disabled in HIRDA config.','warning');
    else if(!status.novnc_available)computerMessage('Local noVNC assets are missing.','warning');
    else if(!(status.runtime_mode==='session-isolated'?status.transport_ready:status.websockify_reachable))computerMessage(status.runtime_mode==='session-isolated'?'Session VNC transport is offline.':'Local websockify bridge is offline.','warning');
    else if(!sessions.length)computerMessage('Computer desktop is ready, but no managed sessions are available.','warning');
    else if(ready&&!computerUiState.selectedSessionId)computerMessage('Choose a monitor to open that session desktop.','good');
  }catch(err){
    updateComputerStatus({enabled:false});
    renderComputerSessions([]);
    computerMessage('Computer status failed: '+err.message,'error');
  }
}

async function connectComputerView(id){
  if(!id){computerMessage('Choose a managed session first.','warning');return;}
  try{
    const status=computerUiState.status||await getJson('/api/computer/status');
    if(!status.enabled)throw new Error('Computer Use is disabled');
    if(!status.novnc_available)throw new Error('Local noVNC assets are missing');
    if(!(status.runtime_mode==='session-isolated'?status.transport_ready:status.websockify_reachable))throw new Error(status.runtime_mode==='session-isolated'?'Session VNC transport is offline':'Local websockify bridge is offline');
    const descriptor=await getJson('/api/computer/descriptor/'+encodeURIComponent(id));
    computerUiState.descriptor=descriptor;
    computerUiState.selectedSessionId=id;
    renderComputerSessions(computerUiState.sessions);
    const session=computerUiState.sessions.find(function(s){return s.id===id;})||{};
    const name=document.getElementById('computerSelectedSessionName');
    const meta=document.getElementById('computerSelectedSessionMeta');
    if(name)name.textContent=session.name||session.workspace_key||id;
    if(meta)meta.textContent=(session.workspace_key||'managed session')+' · '+(session.status||'unknown')+' · VNC '+(descriptor.desktop_display||'unpaired')+(descriptor.cdp_port?' · CDP '+descriptor.cdp_port:'');
    const websocketPath=descriptor.websocket_path||('/api/computer/vnc/ws/'+encodeURIComponent(id));
    // noVNC resolves its `path` setting relative to vnc.html. The viewer is
    // mounted at /computer/novnc/, so climb back to the application root first.
    let path='../..'+websocketPath;
    const tokenInput=document.getElementById('computerTokenInput');
    const token=(tokenInput&&tokenInput.value||'').trim();
    if(token)path+=(path.includes('?')?'&':'?')+'token='+encodeURIComponent(token);

    const vncPasswordInput=document.getElementById('computerVncPasswordInput');
    const vncPassword=(vncPasswordInput&&vncPasswordInput.value||'').trim();
    const panel=document.getElementById('computerViewerPanel');
    const iframe=document.getElementById('computerViewer');
    if(panel)panel.hidden=false;

    if(descriptor.runtime_mode==='session-isolated'&&!vncPassword){
      if(iframe)iframe.src='about:blank';
      if(vncPasswordInput)vncPasswordInput.focus();
      computerMessage('Enter the VNC password, then press Reconnect.','warning');
      return;
    }

    const url=new URL(descriptor.viewer_url||'/computer/novnc/vnc.html',location.origin);
    const viewerParams=new URLSearchParams();
    viewerParams.set('autoconnect','true');
    viewerParams.set('reconnect','0');
    viewerParams.set('resize','scale');
    viewerParams.set('path',path);
    if(vncPassword)viewerParams.set('password',vncPassword);
    // Use the fragment so credentials and viewer parameters are never sent to
    // the static-file server or reverse proxy and cannot poison noVNC caches.
    url.hash=viewerParams.toString();
    if(iframe)iframe.src=url.pathname+url.hash;
    if(panel)panel.scrollIntoView({behavior:'smooth',block:'start'});
    computerMessage('Opened '+(session.name||session.workspace_key||'session')+' desktop. If noVNC asks for credentials, enter the existing VNC password.','good');
  }catch(err){
    computerMessage('Could not connect: '+err.message,'error');
  }
}

async function repairComputerView(){
  const id=computerUiState.selectedSessionId;
  if(!id){computerMessage('Choose a managed session first.','warning');return;}

  let targetInfo;
  try{
    targetInfo=await getJson('/api/computer/re-pair-targets/'+encodeURIComponent(id));
  }catch(err){
    computerMessage('Could not load re-pair targets: '+err.message,'error');
    return;
  }

  const availableTargets=(targetInfo.targets||[]).filter(target=>target.available);
  const targetOptions=[
    {value:'auto',label:'Auto · next available desktop'},
    ...availableTargets.map(target=>({value:String(target.display),label:'VNC :'+target.display}))
  ];
  const adopted=!!targetInfo.current_adopted;
  const modeOptions=adopted
    ? [{value:'fresh',label:'Fresh desktop'}]
    : [
        {value:'keep',label:'Keep browser session'},
        {value:'fresh',label:'Fresh desktop'}
      ];
  const currentLabel=targetInfo.current_display==null?'unpaired':(':'+targetInfo.current_display);
  const result=await openAppDialog({
    title:'Re-pair Computer runtime',
    message:adopted
      ? 'Current desktop '+currentLabel+' is externally adopted. Choose a new target; Fresh desktop avoids sharing a live browser profile with the adopted runtime.'
      : 'Move '+currentLabel+' to a specific free VNC desktop or let HIRDA choose automatically.',
    icon:'↻',
    tone:'info',
    confirmText:'Re-pair',
    fields:[
      {
        name:'mode',
        label:'Browser state',
        type:'select',
        value:adopted?'fresh':'keep',
        options:modeOptions
      },
      {
        name:'target_display',
        label:'Target desktop',
        type:'select',
        value:'auto',
        options:targetOptions
      }
    ]
  });
  if(!result.confirmed)return;

  const mode=result.values.mode||(adopted?'fresh':'keep');
  const targetValue=result.values.target_display||'auto';
  const targetDisplay=targetValue==='auto'?null:Number(targetValue);
  const iframe=document.getElementById('computerViewer');
  if(iframe)iframe.src='about:blank';
  computerMessage('Re-pairing Computer runtime…','');

  try{
    const descriptor=await sendJson(
      '/api/computer/re-pair/'+encodeURIComponent(id),
      {mode,target_display:targetDisplay}
    );
    computerUiState.descriptor=descriptor;
    computerUiState.selectedSessionId=id;
    if(computerUiState.status){
      computerUiState.status.runtime_displays=computerUiState.status.runtime_displays||{};
      const nextDisplay=Number(String(descriptor.desktop_display||'').replace(':',''));
      if(Number.isInteger(nextDisplay))computerUiState.status.runtime_displays[id]=nextDisplay;
    }
    renderComputerSessions(computerUiState.sessions);

    const repair=descriptor.re_pair||{};
    const from=repair.previous_display?('VNC '+repair.previous_display):'unpaired';
    const to=descriptor.desktop_display?('VNC '+descriptor.desktop_display):'new desktop';
    const passwordInput=document.getElementById('computerVncPasswordInput');
    const hasPassword=!!(passwordInput&&passwordInput.value.trim());

    if(hasPassword){
      await connectComputerView(id);
      computerMessage('Re-paired '+from+' → '+to+(mode==='keep'?' with browser session preserved.':' with a fresh browser profile.'),'good');
    }else{
      computerMessage('Re-paired '+from+' → '+to+'. Enter the VNC password and press Reconnect.','good');
      if(passwordInput)passwordInput.focus();
    }
  }catch(err){
    computerMessage('Re-pair failed: '+err.message,'error');
  }
}

function disconnectComputerView(){
  const iframe=document.getElementById('computerViewer');
  const panel=document.getElementById('computerViewerPanel');
  if(iframe)iframe.src='about:blank';
  if(panel)panel.hidden=true;
  computerUiState.descriptor=null;
  computerUiState.selectedSessionId=null;
  renderComputerSessions(computerUiState.sessions);
  computerMessage('Viewer disconnected. Choose another monitor when ready.','');
}

async function fullscreenComputerView(){
  const shell=document.getElementById('computerViewerShell');
  if(!shell)return;
  try{
    if(document.fullscreenElement)await document.exitFullscreen();
    else await shell.requestFullscreen();
  }catch(err){computerMessage('Fullscreen failed: '+err.message,'error');}
}

document.addEventListener('click',function(event){
  const sessionCard=event.target.closest&&event.target.closest('[data-computer-session]');
  if(sessionCard){connectComputerView(sessionCard.dataset.computerSession);return;}
  const target=event.target.closest&&event.target.closest('button');
  if(!target)return;
  if(target.id==='computerRePairBtn')repairComputerView();
  else if(target.id==='computerReconnectBtn')connectComputerView(computerUiState.selectedSessionId);
  else if(target.id==='computerDisconnectBtn')disconnectComputerView();
  else if(target.id==='computerFullscreenBtn')fullscreenComputerView();
});

window.addEventListener('hashchange',function(){if(location.hash==='#computer')refreshComputerView();});
if((location.hash||'#home')==='#computer')setTimeout(refreshComputerView,0);

/* Reference settings proxies keep the compact topbar faithful without removing controls. */
document.addEventListener('click',function(event){
  const proxy=event.target.closest&&event.target.closest('[data-proxy-click]');
  if(!proxy)return;
  const target=document.getElementById(proxy.dataset.proxyClick||'');
  if(target)target.click();
});
