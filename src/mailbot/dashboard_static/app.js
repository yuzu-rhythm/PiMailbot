'use strict';
const $ = id => document.getElementById(id);
const format = (date, options) => new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', ...options}).format(date);
const dayKey = date => format(date, {year:'numeric',month:'2-digit',day:'2-digit'}).replaceAll('/','-');
const node = (tag, text='', cls='') => {const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
const number = value => new Intl.NumberFormat('ja-JP').format(value);
let lastSnapshot=null;
let selectedDay=dayKey(new Date());
function changeDay(delta){selectedDay=delta===0?dayKey(new Date()):dayKey(new Date(new Date(selectedDay+'T00:00:00+09:00').getTime()+delta*86400000));if(lastSnapshot)render(lastSnapshot);}
let hidden=new Set();
try {const value=JSON.parse(localStorage.getItem('hiddenCalendars')||'[]');if(Array.isArray(value))hidden=new Set(value);}catch{}
function tick(){const d=new Date();DayTimeline.updateClock();$('clock').textContent=format(d,{hour:'2-digit',minute:'2-digit',hour12:false});$('mail-alert-clock').textContent=$('clock').textContent;$('year').textContent=format(d,{year:'numeric'});$('date').textContent=format(d,{month:'long',day:'numeric'})+'（'+format(d,{weekday:'short'})+'）';}
function status(s){return ({LOADING:'取得しています',STALE:'更新できません。前回の内容です',ERROR:'取得できません',NOT_CONFIGURED:'未連携'})[s.state]||'';}
function empty(root,title,description){const d=node('div','','empty');d.append(node('strong',title),node('p',description));root.append(d);}
function dueText(t){if(!t.due)return '期限なし';const key=t.due.slice(0,10);let result=key===dayKey(new Date())?'今日':format(new Date(key+'T00:00:00+09:00'),{month:'numeric',day:'numeric'});if(t.due.includes('T'))result+=' '+format(new Date(t.due),{hour:'2-digit',minute:'2-digit'});return result;}
function renderTasks(s){const root=$('tasks');root.replaceChildren();$('tasks-status').textContent=status(s);
if(!Array.isArray(s.items)){empty(root,s.state==='NOT_CONFIGURED'?'Todoistを連携してください':'タスクを取得しています','取得したタスクを期限ごとに表示します。');return;}
if(!s.items.length){empty(root,'未完了のタスクはありません','Todoistに追加すると、ここに表示されます。');return;}
const today=dayKey(new Date());const byPriority=(a,b)=>b.priority-a.priority||(a.due||'').localeCompare(b.due||'');
const groups=[{label:'今日まで',rows:s.items.filter(t=>t.due&&t.due.slice(0,10)===today).sort(byPriority)},
{label:'期限が過ぎたタスク',rows:s.items.filter(t=>t.due&&t.due.slice(0,10)<today),overdue:true},
{label:'そのほかのタスク',rows:s.items.filter(t=>!t.due||t.due.slice(0,10)>today)}];
let shown=0;
for(const g of groups){if(!g.rows.length||shown>=4)continue;root.append(node('h3',g.label,g.overdue?'overdue':''));const allowance=Math.min(g.rows.length,shown===0?3:4-shown);
for(const t of g.rows.slice(0,allowance)){const row=node('div','','task-row');const marker=node('span','□','task-marker');marker.setAttribute('aria-hidden','true');const content=node('div','','task-content');content.append(node('div',t.title,'title'),node('div',dueText(t),'meta'+(g.overdue?' overdue':'')));row.append(marker,content);root.append(row);shown++;}}
const remaining=s.count-shown;root.append(node('p',`未完了 ${number(s.count)}件${remaining>0?' · ほか'+number(remaining)+'件':''}${s.partial?' · 一部のみ取得':''}`,'rest'));
}
function eventDay(e){return e.all_day?e.start.slice(0,10):dayKey(new Date(e.start));}
function eventOnDay(e,key){const start=key+'T00:00:00+09:00';const end=new Date(new Date(start).getTime()+86400000);return e.all_day?e.start.slice(0,10)<=key&&(!e.end||key<e.end.slice(0,10)):new Date(e.start)<end&&new Date(e.end||e.start)>new Date(start);}
function eventLong(e){return e.end&&new Date(e.end).getTime()-new Date(e.start).getTime()>=(e.all_day?2:1)*86400000;}
function dayEvents(rows,key){return rows.filter(e=>eventOnDay(e,key)).sort((a,b)=>Number(Boolean(eventLong(a)))-Number(Boolean(eventLong(b)))||a.start.localeCompare(b.start));}
function eventActive(e){if(!e.end)return false;return e.all_day?eventOnDay(e,dayKey(new Date())):new Date(e.start)<=new Date()&&new Date(e.end)>new Date();}
function renderCalendar(s){$('calendar-status').textContent=status(s);$('calendar-filters').replaceChildren();
for(const c of s.selected||[]){const b=node('button',c.label,'filter');b.dataset.calendar=c.key;b.setAttribute('aria-pressed',String(!hidden.has(c.key)));b.onclick=()=>{hidden.has(c.key)?hidden.delete(c.key):hidden.add(c.key);try{localStorage.setItem('hiddenCalendars',JSON.stringify([...hidden]));}catch{}render(lastSnapshot);};$('calendar-filters').append(b);}
const next=$('next-event'),events=$('events');next.replaceChildren();events.replaceChildren();
const rows=Array.isArray(s.items)?s.items.filter(e=>!hidden.has(e.calendar)).sort((a,b)=>a.start.localeCompare(b.start)):null;
next.hidden=true;
const todayKey=dayKey(new Date());
if(selectedDay<todayKey||selectedDay>dayKey(new Date(Date.now()+6*86400000)))selectedDay=todayKey;
DayTimeline.render(events,{rows,day:selectedDay,today:todayKey,onDayChange:changeDay});
$('week').replaceChildren();$('week-status').textContent=rows?s.state==='STALE'?'前回の内容を表示中':s.partial?'全ての予定は取得できていません':'':'カレンダーの連携待ち';
const start=new Date(dayKey(new Date())+'T00:00:00+09:00');for(let i=0;i<7;i++){const d=new Date(start.getTime()+i*86400000);const cell=node('button','','day');cell.type='button';cell.setAttribute('aria-pressed',String(selectedDay===dayKey(d)));cell.onclick=()=>{selectedDay=dayKey(d);render(lastSnapshot);};cell.append(node('p',format(d,{month:'numeric',day:'numeric',weekday:'short'}),'day-label'));const matches=rows?dayEvents(rows,dayKey(d)):null;
if(matches===null)cell.append(node('p','—','day-none'));else if(!matches.length)cell.append(node('p','予定なし','day-none'));else{for(const e of matches.slice(0,1))cell.append(node('p',e.title,'day-item'));if(matches.length>1)cell.append(node('p','ほか'+(matches.length-1)+'件','day-none'));}$('week').append(cell);}
}
// Keep the last positive count visible through a temporary connection failure.
function renderMailAlert(mails){
 const triage=mails.some(s=>'unprocessed' in s);
 const value=s=>triage?s.unprocessed:s.unread;
 const known=mails.filter(s=>Number.isSafeInteger(value(s))&&value(s)>=0);
 const total=known.reduce((n,s)=>n+value(s),0);
 const partial=known.some(s=>s.unprocessed_partial);
 const complete=mails.length>0&&known.length===mails.length&&mails.every(s=>s.state==='READY');
 const alert=$('mail-alert');
 if(total===0&&!complete){
   if(!alert.hidden)$('mail-alert-note').textContent='更新を確認できません。前回の未読通知を表示しています。';
   return;
 }
 alert.hidden=total===0;
 document.body.classList.toggle('has-unread-mail',total>0);
 if(total===0)return;
 $('mail-alert-count').textContent=number(total)+(partial?'以上':'');
 $('mail-alert-heading').textContent=triage?'未処理のメールがあります':'未読メールがあります';
 $('mail-alert-accounts').replaceChildren();
 for(const s of known.filter(s=>value(s)>0)){
   $('mail-alert-accounts').append(node('span',`${s.label}　${number(value(s))}件${s.unprocessed_partial?'以上':''}`,'mail-alert-account'));
 }
 $('mail-alert-note').textContent=complete?(triage?'Discordで分類するか受信トレイから移動すると通知が解除されます。過去メールは見直し対象にしたものだけ含みます。':'既読・アーカイブ・ゴミ箱への移動で、受信トレイの未読がなくなると通常表示に戻ります。'):'一部のメールを更新できません。確認できた件数を表示しています。';
}
function render(data){lastSnapshot=data;const sources=data.sources;renderTasks(sources.tasks);renderCalendar(sources.calendar);$('mailboxes').replaceChildren();const mails=Object.values(sources).filter(s=>s.kind==='mail');renderMailAlert(mails);
for(const s of mails){const row=node('div','','mail-row');const label=node('div');const at=s.label.lastIndexOf('@');label.append(node('div',at>0?s.label.slice(0,at):s.label,'mail-name'));if(at>0)label.append(node('div',s.label.slice(at),'mail-domain'));if(status(s))label.append(node('div',status(s),'mail-error'));row.append(label,node('span',s.unread===undefined?'—':number(s.unread),'mail-number'));$('mailboxes').append(row);}
$('mail-status').textContent='受信トレイの未読メール';
const failed=Object.values(sources).some(s=>s.state==='STALE'||s.state==='ERROR');$('health').textContent=failed?'一部の情報を更新できていません':'';$('health').className=failed?'bad':'';
const timestamps=Object.values(sources).filter(s=>s.state==='READY'&&s.updated_at).map(s=>s.updated_at);$('updated').textContent=timestamps.length?'最終更新 '+format(new Date(Math.min(...timestamps)*1000),{hour:'2-digit',minute:'2-digit'}):'データを取得しています';}
async function refresh(){try{const r=await fetch('/api/snapshot',{cache:'no-store'});if(!r.ok)throw Error();render(await r.json());}catch{if(!$('mail-alert').hidden)$('mail-alert-note').textContent='接続が切れています。前回の未読通知を表示しています。';$('health').textContent='接続が切れています。表示中の情報は更新されていません。';$('health').className='bad';}}
tick();setInterval(tick,1000);refresh();setInterval(refresh,15000);
