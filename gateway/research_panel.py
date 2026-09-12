"""Credential-free browser panel for retained Deep Research progress.

Positioning is deliberate: the panel is docked to the top of the viewport on
every breakpoint, never to the bottom.  The chat composer lives at the bottom
of the page, so a bottom-docked overlay would sit on top of the one control a
research turn cannot be operated without -- and no CSS can know how tall the
composer is on a given build.  Top-docking makes "the panel never covers the
composer" a geometric fact rather than a measurement that drifts.

``internal://deep-research``
---------------------------
That string is not a URL.  It is one half of a *model identifier comparison*
inside the official React bundle (the rendered research region tests the active
model against ``deep-research`` or ``internal://deep-research``), so a normal
browser never resolves it, and nothing in the mirror can make the bundle render
its own research component.  The mirror therefore does not navigate to it, does
not rewrite it, and does not pretend to satisfy it.  This panel is the
supported mirror path: it is driven by the server-side projection of the real
upstream stream, so progress is visible whether or not the official component
decides to render.  A regression test asserts no module in the gateway rewrites
or fetches the sentinel.

The panel also renders the answer itself.  A real turn measured on 2026-09-13
streamed its answer and completed, yet the page showed no report at all: the
official region never resolved, and the mirror's own panel had nothing but an
event count.  The projection now carries the assistant's own visible text, and
this panel writes it with ``textContent`` -- never as markup -- bounded and
labelled by whether upstream itself marked the frame as the end of the turn.
When upstream sent no answer body, the panel says exactly that instead of
showing an empty box.
"""

from fastapi.responses import Response

from app import app


# ``?v=`` is a cache key for a browser that already holds an older copy; the
# response itself is no-cache, so the two only ever reinforce each other.
PANEL_TAGS = (
    '<link rel="stylesheet" href="/_chat-share/research-panel.css?v=4">'
    '<script src="/_chat-share/research-panel.js?v=4" defer></script>'
)


PANEL_CSS = r"""
#c2a-rp{position:fixed;top:72px;right:20px;bottom:auto;left:auto;z-index:2147483000;
width:min(380px,calc(100vw - 32px));max-height:min(52vh,420px);overflow:auto;
overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
background:rgba(20,20,20,.94);color:#f5f5f5;border:1px solid rgba(255,255,255,.13);
border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.28);padding:16px 18px;
font:13px/1.45 ui-sans-serif,system-ui,sans-serif;backdrop-filter:blur(18px)}
#c2a-rp[hidden]{display:none}
#c2a-rp header{display:flex;align-items:center;gap:10px}
#c2a-rp strong{font-size:14px;flex:1}
#c2a-rp button{width:44px;height:44px;margin:-12px -12px -12px 0;border:0;
background:transparent;color:#aaa;font-size:20px;cursor:pointer}
#c2a-rp .c2a-rp-state{margin:10px 0 2px;font-weight:600;color:#fff}
#c2a-rp .c2a-rp-action{margin:0 0 10px;color:#c9c9c9}
#c2a-rp .c2a-rp-evidence{margin:0 0 10px;color:#8f8f8f;font-size:11px;
word-break:break-word}
#c2a-rp .c2a-rp-note{margin:10px 0 0;color:#7d7d7d;font-size:11px}
#c2a-rp .c2a-rp-report{margin:10px 0;padding:10px 12px;border-radius:12px;
background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08)}
#c2a-rp .c2a-rp-report[hidden]{display:none}
#c2a-rp .c2a-rp-report-title{margin:0 0 6px;font-size:11px;font-weight:600;color:#c9c9c9}
#c2a-rp .c2a-rp-body{margin:0;color:#e8e8e8;font-size:12px;white-space:pre-wrap;
word-break:break-word}
#c2a-rp .c2a-rp-report-note{margin:6px 0 0;color:#8f8f8f;font-size:11px}
#c2a-rp dl{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0}
#c2a-rp dt{color:#929292;font-size:11px}
#c2a-rp dd{margin:2px 0 0;font-variant-numeric:tabular-nums}
#c2a-rp .c2a-rp-live{width:8px;height:8px;border-radius:50%;background:#20c997;
box-shadow:0 0 0 4px rgba(32,201,151,.14);flex:0 0 auto}
#c2a-rp[data-finished="true"] .c2a-rp-live{background:#8b8b8b;box-shadow:none}
#c2a-rp[data-state="failed"] .c2a-rp-live{background:#e5484d;box-shadow:0 0 0 4px rgba(229,72,77,.16)}
#c2a-rp[data-state="cancelled"] .c2a-rp-live{background:#f5a524;box-shadow:0 0 0 4px rgba(245,165,36,.16)}
@media(max-width:720px){#c2a-rp{top:56px;right:12px;left:12px;bottom:auto;
width:auto;max-height:40vh;border-radius:16px;padding:14px 16px}
#c2a-rp dl{grid-template-columns:1fr 1fr}}
"""


PANEL_JS = r"""
(()=>{'use strict';
const ACTIVE='/backend-api/research-progress/active';
const POLL_ACTIVE=1800,POLL_IDLE=3000,POLL_BACKOFF=2500,MAX_FAILURES=6;
let timer=null,failures=0,closed=false,source=ACTIVE,lastState='';
const node=(tag,name,text)=>{const e=document.createElement(tag);if(name)e.className=name;
if(text!==undefined&&text!==null)e.textContent=String(text);return e};
function duration(ms){if(!Number.isFinite(ms)||ms<0)return '—';const s=Math.floor(ms/1000);
return s<60?s+' 秒':Math.floor(s/60)+' 分 '+String(s%60).padStart(2,'0')+' 秒'}
/* The conversation being viewed, when the URL names one.  Reading it is what
   makes a refresh land on the right record instead of "newest research turn". */
function viewedConversation(){const m=/^\/c\/([A-Za-z0-9_-]{1,128})/.exec(location.pathname);
return m?m[1]:''}
function part(tag,name){const w=node('div');w.append(node('dt','',tag));const d=node('dd','');
d.dataset.key=name;w.append(d);return w}
function ensure(){let root=document.getElementById('c2a-rp');if(root)return root;
root=node('section');root.id='c2a-rp';root.hidden=true;root.setAttribute('role','status');
root.setAttribute('aria-live','polite');root.setAttribute('aria-label','深度研究进度');
const h=node('header');h.append(node('span','c2a-rp-live'));h.append(node('strong','','深度研究'));
const b=node('button','','×');b.type='button';b.setAttribute('aria-label','关闭研究进度');
b.onclick=()=>{closed=true;root.remove();stop()};h.append(b);root.append(h);
root.append(node('p','c2a-rp-state','研究中'));
root.append(node('p','c2a-rp-action','等待上游事件'));
root.append(node('p','c2a-rp-evidence',''));
const dl=node('dl');dl.append(part('来源','sources'));dl.append(part('事件','events'));
dl.append(part('耗时','elapsed'));root.append(dl);
root.append(node('p','c2a-rp-note','镜像仅显示上游实际报告的活动，不构成官方进度。'));
document.body.append(root);return root}
/* The report region exists because the official one does not render in a normal
   browser (see the asset header): the answer the upstream already sent is shown
   here instead, as text.  It is created lazily so a turn with no answer keeps
   exactly the panel it had before. */
function reportBox(root){let box=root.querySelector('.c2a-rp-report');if(box)return box;
box=node('section','c2a-rp-report');box.hidden=true;
box.append(node('p','c2a-rp-report-title','研究报告'));
box.append(node('p','c2a-rp-body',''));
box.append(node('p','c2a-rp-report-note',''));
root.insertBefore(box,root.querySelector('.c2a-rp-evidence'));return box}
/* Wording is chosen by what upstream actually sent, never by what the mirror
   would like to have received: a body upstream did not mark as the end of the
   turn is described as what it is -- the last body received -- rather than
   being called a finished report, and a turn that streamed no body at all says
   so instead of showing an empty box. */
function reportTitle(p){if(p.report_final===true)return '研究报告';
return '研究报告（最后收到的正文）'}
function applyReport(root,p){const box=reportBox(root);
const text=typeof p.report==='string'?p.report:'';
if(!text&&p.finished!==true){box.hidden=true;return}
box.hidden=false;
setText(box.querySelector('.c2a-rp-report-title'),text?reportTitle(p):'研究报告');
setText(box.querySelector('.c2a-rp-body'),text||'上游未提供可显示的报告正文');
setText(box.querySelector('.c2a-rp-report-note'),
text&&p.report_truncated===true?'报告过长，此处只显示开头部分':'')}
/* Terminal wording is derived from the state the mirror recorded, not from the
   last activity label: a turn can end while its last activity was still
   "writing", and showing that as the outcome is what made cancellation and
   failure unreadable. */
function headline(state){if(state==='complete')return '研究已完成';
if(state==='cancelled')return '研究已取消';if(state==='failed')return '研究失败';
return '研究正在进行'}
function sourcesText(p){if(p.sources>0)return p.sources+' 个';
return p.sources_evidenced?'暂无来源':'等待来源'}
function evidenceText(p){const parts=[];if(p.tool)parts.push(p.tool);
(p.markers||[]).forEach((m)=>{if(parts.indexOf(m)<0)parts.push(m)});
(p.content_types||[]).forEach((c)=>{if(parts.indexOf(c)<0)parts.push(c)});
return parts.length?'上游事件：'+parts.join(' · '):''}
function setText(el,text){if(el&&el.textContent!==text)el.textContent=text}
function hide(){const root=document.getElementById('c2a-rp');if(root)root.hidden=true}
/* Returns 'active' while a turn is running, 'finished' once the mirror recorded
   a terminal state, and 'idle' when no research turn is retained at all. */
function render(data){if(closed||!data||data.research!==true||!data.projection){hide();return 'idle'}
const p=data.projection,root=ensure(),state=p.finished?(data.state||'complete'):'active';
root.hidden=false;root.dataset.finished=String(p.finished===true);root.dataset.state=state;
if(state!==lastState){lastState=state;setText(root.querySelector('.c2a-rp-state'),headline(state))}
setText(root.querySelector('.c2a-rp-action'),'最近活动：'+(p.action||'等待上游事件'));
applyReport(root,p);
setText(root.querySelector('.c2a-rp-evidence'),evidenceText(p));
setText(root.querySelector('[data-key="sources"]'),sourcesText(p));
setText(root.querySelector('[data-key="events"]'),Number.isFinite(data.events_seen)?data.events_seen:'—');
/* Elapsed comes from the server and is frozen there once the turn is terminal,
   so it neither drifts while polling nor restarts after a refresh. */
setText(root.querySelector('[data-key="elapsed"]'),duration(p.elapsed_ms));
return p.finished===true?'finished':'active'}
function stop(){if(timer){clearTimeout(timer);timer=null}}
function schedule(ms){stop();if(!closed)timer=setTimeout(poll,ms)}
/* After a terminal state the timer stops -- but a new turn almost always starts
   with the user touching the page, so one passive listener re-arms discovery
   without polling an idle tab forever. */
function armResume(){const resume=()=>{if(closed)return;failures=0;poll()};
document.addEventListener('pointerdown',resume,{passive:true,once:true});
document.addEventListener('keydown',resume,{once:true})}
async function poll(){if(closed)return;
if(document.hidden){schedule(POLL_IDLE);return}
try{const r=await fetch(source,{credentials:'same-origin',cache:'no-store',
headers:{accept:'application/json'}});
if(r.status===404&&source!==ACTIVE){source=ACTIVE;failures=0;schedule(0);return}
if(!r.ok)throw new Error('status');
const verdict=render(await r.json());failures=0;
if(verdict==='active')schedule(POLL_ACTIVE);
else if(verdict==='idle')schedule(POLL_IDLE);
else{stop();armResume()}}catch(_){failures++;
if(failures<MAX_FAILURES)schedule(POLL_BACKOFF);else{stop();
const root=document.getElementById('c2a-rp');if(root)root.remove()}}}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!closed){failures=0;poll()}});
const viewed=viewedConversation();
if(viewed)source='/backend-api/research-progress/'+encodeURIComponent(viewed)+'/projection';
poll()})();
"""


@app.get("/_chat-share/research-panel.js")
async def research_panel_js():
    return Response(PANEL_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/_chat-share/research-panel.css")
async def research_panel_css():
    return Response(PANEL_CSS, media_type="text/css",
                    headers={"Cache-Control": "no-cache, must-revalidate"})
