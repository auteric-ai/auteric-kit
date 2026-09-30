const $ = (id) => document.getElementById(id);
const state = { scan: null, scanId: null, lastTarget: null, platforms: [], findingFilter: 'all', testFilter: 'all', catalog: {products:[],page:1,page_count:0,total:0}, catalogExpanded: false, catalogSearchTimer: null, catalogDraggable: null, resultTimeline: null, landingTimeline: null, progressTimeline: null, progressStage: -1, gaugeMotionReady: false, scrollTriggers: [] };
const motion = {
  reduce: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  ready: false,
  ease: 'expo.out'
};

function esc(v=''){ return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function pretty(v){ return JSON.stringify(v ?? {}, null, 2); }
function hasEvidenceData(value){
  if(value===null||value===undefined)return false;
  if(Array.isArray(value))return value.some(hasEvidenceData);
  if(typeof value==='object')return Object.values(value).some(hasEvidenceData);
  return typeof value==='string'?value.trim().length>0:true;
}
function host(url){ try{return new URL(url).hostname}catch{return url || 'target'} }
function clamp(n){ n=Number(n||0); return Math.max(0,Math.min(100,n)); }
function scoreText(v){ return v===null || v===undefined ? 'N/V' : String(clamp(v)); }
function statusLabel(s){ return ({pass:'PASS',fail:'FAIL',warning:'WARNING',unknown:'UNKNOWN',unsupported:'N/A'}[s]||String(s||'UNKNOWN').toUpperCase()); }
function grade(n){ n=Number(n||0); return n>=90?'A':n>=80?'B':n>=70?'C':n>=55?'D':'F'; }
function scoreGrade(n){ if(n>=90)return 'Excellent AI shopping readiness'; if(n>=80)return 'Strong foundation with limited gaps'; if(n>=70)return 'Good start with clear improvements'; if(n>=55)return 'Readiness needs attention'; return 'Important foundations are missing'; }
function dateText(v){ if(!v)return 'Unknown'; try{return new Date(v).toLocaleString()}catch{return v} }
function formatPrice(p){ if(p.price===null||p.price===undefined)return 'Price not verified'; if(p.currency){ try{return new Intl.NumberFormat(undefined,{style:'currency',currency:p.currency,maximumFractionDigits:2}).format(p.price);}catch{} } return String(p.price); }
function safeUrl(v){ try{ const u=new URL(v); return ['http:','https:'].includes(u.protocol) ? u.href : ''; }catch{return '';} }
function favicon(domain){ return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=128`; }
function showToast(text){ const t=$('toast'); if(!t)return; t.textContent=text; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2200); }
function track(name,properties={}){ const detail={name,properties:{...properties,path:location.pathname}}; window.dataLayer=window.dataLayer||[]; window.dataLayer.push({event:name,...detail.properties}); window.dispatchEvent(new CustomEvent('auteric:analytics',{detail})); }
function isLocalScanner(){ return ['localhost','127.0.0.1','::1'].includes(location.hostname) || location.hostname.endsWith('.localhost'); }
function isLoopbackHost(host){ return host==='localhost'||host.endsWith('.localhost')||host==='::1'||/^127(?:\.\d{1,3}){3}$/.test(host); }
function normalizeStoreUrl(value){ const raw=String(value||'').trim(); if(!raw)throw new Error('Enter your store URL.'); const localInput=/^(?:localhost|(?:[\w-]+\.)+localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(raw); let url; try{url=new URL(/^https?:\/\//i.test(raw)?raw:`${localInput?'http':'https'}://${raw}`)}catch{throw new Error('Enter a valid store URL.')} if(!['http:','https:'].includes(url.protocol)||!url.hostname||(!isLoopbackHost(url.hostname)&&!url.hostname.includes('.'))|| (isLoopbackHost(url.hostname)&&!isLocalScanner()))throw new Error(isLocalScanner()?'Enter a valid store URL.':'Enter a valid public store domain, such as example.com.'); url.hash=''; return url.href; }

async function loadPlatformConfig(){
  try{
    const data=await api('/api/platforms');
    state.platforms=Array.isArray(data.platforms)?data.platforms:[];
    const shopify=state.platforms.find(item=>item.id==='shopify');
    if($('shopifyStatus'))$('shopifyStatus').textContent=shopify?.status==='install_available'?'Auteric protection · installation available':'Auteric protection · installation access coming soon';
  }catch{
    state.platforms=[];
  }
}

function initMotion(){
  if(!window.gsap)return;
  const plugins=[window.ScrollTrigger,window.Flip,window.Draggable,window.InertiaPlugin,window.DrawSVGPlugin,window.CustomEase].filter(Boolean);
  if(plugins.length)window.gsap.registerPlugin(...plugins);
  if(window.CustomEase){ window.CustomEase.create('autericEase','M0,0 C0.16,1 0.3,1 1,1'); motion.ease='autericEase'; }
  motion.ready=true;
}

function animateLandingStage(){
  if(!motion.ready||motion.reduce||!document.querySelector('.hero-copy'))return;
  state.landingTimeline?.kill?.();
  const gsap=window.gsap;
  const sceneLines=gsap.utils.toArray('.scanner-storefront-scene .scene-line');
  if(window.DrawSVGPlugin)gsap.set(sceneLines,{drawSVG:'0% 0%'});
  else gsap.set(sceneLines,{autoAlpha:0});
  state.landingTimeline=gsap.timeline({defaults:{ease:motion.ease}})
    .fromTo('.hero-edition',{autoAlpha:0,y:14},{autoAlpha:1,y:0,duration:.4},0)
    .fromTo('.hero-copy h1',{autoAlpha:0,y:34,rotation:.6},{autoAlpha:1,y:0,rotation:0,duration:.8},.06)
    .fromTo('.hero-lede',{autoAlpha:0,y:18},{autoAlpha:1,y:0,duration:.52},.28)
    .fromTo('.hero-scanner',{autoAlpha:0,y:20,scale:.985},{autoAlpha:1,y:0,scale:1,duration:.62},.38)
    .fromTo('.scene-shop',{autoAlpha:0,y:8},{autoAlpha:1,y:0,duration:.42,stagger:.055},.48)
    .fromTo('.platform-support',{autoAlpha:0,y:10},{autoAlpha:1,y:0,duration:.38},.52)
    .fromTo('.hero-signal,.hero-spark',{autoAlpha:0,scale:.3,rotation:-18},{autoAlpha:1,scale:1,rotation:0,duration:.72,stagger:.07,ease:'back.out(1.35)'},.18)
    .fromTo('.scene-scan-beam',{x:0,autoAlpha:.76},{x:584,autoAlpha:1,duration:3.2,ease:'sine.inOut',repeat:-1,yoyo:true,repeatDelay:.18},.82);
  if(window.DrawSVGPlugin)state.landingTimeline.to(sceneLines,{drawSVG:'0% 100%',duration:.5,stagger:.025,ease:'power2.out'},.4);
  else state.landingTimeline.to(sceneLines,{autoAlpha:1,duration:.35,stagger:.02},.4);
  gsap.to('.signal-ucp',{y:-18,rotation:4,duration:3.4,ease:'sine.inOut',repeat:-1,yoyo:true});
  gsap.to('.signal-mcp',{y:14,rotation:-5,duration:4.1,ease:'sine.inOut',repeat:-1,yoyo:true});
  gsap.to('.signal-agent',{y:-12,x:8,rotation:5,duration:3.7,ease:'sine.inOut',repeat:-1,yoyo:true});
}

function clearReportMotion(){
  state.resultTimeline?.kill?.(); state.resultTimeline=null;
  state.scrollTriggers.forEach(trigger=>trigger?.kill?.()); state.scrollTriggers=[];
}

function clearProgressMotion(){
  state.progressTimeline?.kill?.(); state.progressTimeline=null;
  if(motion.ready)window.gsap.set(['.progress-scan-beam','.progress-window'],{clearProps:'transform,opacity,visibility,willChange'});
}

function startProgressMotion(){
  clearProgressMotion();
  if(!motion.ready||motion.reduce)return;
  const gsap=window.gsap;
  gsap.set('.progress-scan-beam',{x:0,autoAlpha:.8,willChange:'transform'});
  state.progressTimeline=gsap.timeline({repeat:-1,yoyo:true,repeatDelay:.16})
    .to('.progress-scan-beam',{x:580,duration:3.05,ease:'sine.inOut'},0)
    .fromTo('.progress-window',{autoAlpha:.52},{autoAlpha:1,duration:.3,stagger:{each:.13,from:'start'},ease:'power1.out'},.08);
}

function setupGaugeMotion(){
  if(state.gaugeMotionReady||!motion.ready||motion.reduce)return;
  state.gaugeMotionReady=true;
  document.querySelectorAll('.auteric-gauge').forEach(gauge=>{
    const instrument=gauge.querySelector('.gauge-instrument'), ticks=gauge.querySelector('.gauge-ticks');
    const enter=()=>{
      window.gsap.to(instrument,{scale:1.01,y:-2,duration:.28,ease:motion.ease,overwrite:'auto'});
      window.gsap.to(ticks,{rotation:2,svgOrigin:'120 120',duration:.38,ease:motion.ease,overwrite:'auto'});
    };
    const leave=()=>{
      window.gsap.to(instrument,{scale:1,y:0,duration:.24,ease:'power2.out',overwrite:'auto'});
      window.gsap.to(ticks,{rotation:0,svgOrigin:'120 120',duration:.3,ease:'power2.out',overwrite:'auto'});
    };
    gauge.addEventListener('pointerenter',enter); gauge.addEventListener('pointerleave',leave);
    gauge.addEventListener('focus',enter); gauge.addEventListener('blur',leave);
  });
}

function animateLandingExit(){
  if(!motion.ready||motion.reduce)return;
  window.gsap.timeline({defaults:{ease:'power2.inOut'}})
    .fromTo('#progressCard',{autoAlpha:0,y:32,scale:.965},{autoAlpha:1,y:0,scale:1,duration:.62,ease:motion.ease},.13)
    .fromTo('#progressCard .scan-core',{scale:.94,y:18,autoAlpha:0},{scale:1,y:0,autoAlpha:1,duration:.68,ease:motion.ease},.22)
    .fromTo('#scanStages > div',{autoAlpha:0,y:12},{autoAlpha:1,y:0,duration:.34,stagger:.055,ease:motion.ease},.36);
}

function effectiveChecks(scan){
  const direct=scan?.checks||[];
  if(direct.length){ state.checksDerived=false; return direct; }
  const findings=scan?.findings||[];
  state.checksDerived=!!findings.length;
  return findings.map((f,idx)=>({
    id:f.id||`legacy-${idx}`,
    category:f.category==='trust'?'agent':f.category==='security'?'web':f.category==='product'?'catalog':f.category==='transport'?'transport':'ucp',
    title:f.title||'Stored finding',
    method:'Reconstructed from a stored legacy finding. Rescan for the full current test matrix.',
    status:f.status||'unknown', severity:f.severity||'medium',
    exposure:f.summary||'Stored finding requires review.', result:f.summary||'',
    evidence:f.evidence||[], recommendation:f.recommendation?.how||null, confidence:f.confidence||0.6, source:'inferred'
  }));
}
function resultMeaning(status){
  return ({fail:'Confirmed gap',warning:'Hardening / readiness gap',unknown:'Not verified',pass:'Verified by scan',unsupported:'Not applicable'}[status]||'Needs review');
}

async function api(url, opts={}){
  const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});
  const body=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(body.detail || body.error || `Request failed (${r.status})`);
  return body;
}

function showProgress(target){
  $('landingView').classList.add('hidden'); $('directoryView').classList.add('hidden'); $('dashboardView').classList.add('hidden'); $('siteHeader').classList.remove('hidden'); $('progressView').classList.remove('hidden');
  $('progressCard').classList.remove('error'); $('progressActions').classList.add('hidden'); $('progressNote').textContent='Read-only scan. Your storefront is never changed.';
  $('progressView').classList.remove('scan-entering'); void $('progressView').offsetWidth; $('progressView').classList.add('scan-entering');
  state.progressStage=-1; $('progressUrl').textContent=target; setProgress(2,'Discovering store');
  animateLandingExit();
  startProgressMotion();
}
function setProgress(n,msg){
  const value=clamp(n), stageTitle=value>=90?'Building report':value>=70?'Reviewing security':value>=52?'Checking AI commerce':value>=28?'Analyzing products':'Discovering store';
  const nextTitle=stageTitle || msg || 'Scanning', progressBar=$('progressBar');
  $('progressPercent').textContent=`${value}%`; progressBar.style.width='100%'; $('progressTrack').setAttribute('aria-valuenow',String(value));
  if(motion.ready&&!motion.reduce)window.gsap.to(progressBar,{scaleX:value/100,transformOrigin:'0% 50%',duration:.36,ease:motion.ease,overwrite:'auto'}); else progressBar.style.transform=`scaleX(${value/100})`;
  const stages=[...document.querySelectorAll('#scanStages [data-stage]')]; let active=-1;
  stages.forEach((stage,index)=>{ if(value>=Number(stage.dataset.stage))active=index; });
  stages.forEach((stage,index)=>{ const done=value>=100||index<active; stage.classList.toggle('complete',done); stage.classList.toggle('active',!done&&index===Math.max(0,active)); stage.setAttribute('aria-current',!done&&index===Math.max(0,active)?'step':'false'); });
  const nextStage=Math.max(0,active);
  document.querySelectorAll('.progress-shop').forEach((shop,index)=>shop.classList.toggle('is-current',index===nextStage));
  if(state.progressStage!==nextStage){
    state.progressStage=nextStage; $('progressTitle').textContent=nextTitle;
    if(motion.ready&&!motion.reduce){
      window.gsap.fromTo($('progressTitle'),{autoAlpha:0,y:8},{autoAlpha:1,y:0,duration:.32,ease:motion.ease,overwrite:'auto'});
      window.gsap.fromTo(stages[nextStage],{scale:.96,autoAlpha:.55},{scale:1,autoAlpha:1,duration:.3,ease:motion.ease,overwrite:'auto',clearProps:'transform,opacity,visibility'});
      window.gsap.fromTo(document.querySelector(`.progress-shop[data-shop-stage="${nextStage}"]`),{autoAlpha:.56},{autoAlpha:1,duration:.34,ease:motion.ease,overwrite:'auto',clearProps:'opacity,visibility'});
    }
  }else $('progressTitle').textContent=nextTitle;
}
function showScanError(message){
  clearProgressMotion(); $('progressCard').classList.add('error'); $('progressTitle').textContent='We could not complete this scan'; $('progressNote').textContent=message || 'Check that the storefront is public, then try again.'; $('progressActions').classList.remove('hidden');
}
function showDashboard(){
  clearProgressMotion(); $('landingView').classList.add('hidden'); $('directoryView').classList.add('hidden'); $('progressView').classList.add('hidden'); $('siteHeader').classList.add('hidden'); $('dashboardView').classList.remove('hidden');
  $('dashboardView').classList.remove('result-ready');
  requestAnimationFrame(()=>requestAnimationFrame(()=>{ $('dashboardView').classList.add('result-ready'); animateResultReveal(); setupReportScrollMotion(); }));
}
function showLanding(){ document.getElementById('publicReport')?.remove(); clearProgressMotion(); $('dashboardView').classList.add('hidden'); $('directoryView').classList.add('hidden'); $('progressView').classList.add('hidden'); $('siteHeader').classList.remove('hidden'); $('landingView').classList.remove('hidden'); requestAnimationFrame(animateLandingStage); }

function readinessLabel(n){ const tone=window.ScoreGauge.toneFor(clamp(n)); return tone==='attention'?'Needs attention':tone[0].toUpperCase()+tone.slice(1); }
function localAutericConnected(scan){ return scan?.observations?.local_auteric?.status==='connected'; }
function autericProtectionScore(scan){
  if(localAutericConnected(scan))return 100;
  const runtime=scan?.observations?.protocol_summary?.runtime||{};
  const measured=scan?.readiness?.protected;
  const serverVerified=Boolean(scan?.target_url)&&document.body.dataset.autericProtected==='true'&&document.body.dataset.storeDomain===host(scan.target_url);
  const scanVerified=runtime.status==='enforcement_verified'&&runtime.enforcement_verified===true&&measured===100;
  return serverVerified||scanVerified ? 100 : null;
}
function prepareGauge(kind, finalScore){
  if(finalScore===null||finalScore===undefined){
    const status=$(`${kind}GaugeStatus`), button=$(`${kind}Gauge`);
    if(!status||!button)return;
    status.textContent='Safe test needed';
    button.setAttribute('aria-label','Auteric Protection is not measured yet. Open details to run one safe cart check.');
    button.dataset.tone='unknown'; button.dataset.score='unverified';
    window.ScoreGauge.render(button.querySelector('.score-meter'),null,'Auteric Protection');
    return;
  }
  const target=Math.max(5,clamp(finalScore));
  const status=$(`${kind}GaugeStatus`), button=$(`${kind}Gauge`);
  if(!status||!button)return;
  const name=kind==='ai'?'AI Shopping Readiness':kind==='catalog'?'Catalog Readiness':(localAutericConnected(state.scan)?'Auteric Local Connection':'Auteric Protection');
  status.textContent=readinessLabel(target);
  button.setAttribute('aria-label',`${name} ${target} out of 100, ${readinessLabel(target)}. Open details.`);
  button.dataset.tone=window.ScoreGauge.toneFor(target);
  button.dataset.score=String(target);
  window.ScoreGauge.render(button.querySelector('.score-meter'),target,name);
  button.style.setProperty('--gauge-color',window.ScoreGauge.colors[button.dataset.tone]);
}
function animateGauge(kind, finalScore, delay=0){
  const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const meter=$(`${kind}Gauge`)?.querySelector('.score-meter');
  if(!meter)return;
  if(finalScore===null||finalScore===undefined){window.ScoreGauge.update(meter,null);return;}
  const target=clamp(finalScore);
  if(reduce){window.ScoreGauge.update(meter,target);return;}
  window.ScoreGauge.update(meter,0);
  const run=()=>{
    const start=performance.now(), duration=720;
    const frame=now=>{ const p=Math.min(1,(now-start)/duration), eased=1-Math.pow(1-p,4); window.ScoreGauge.update(meter,target*eased); if(p<1)requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
  };
  setTimeout(run,delay);
}
function animatePrimaryGauges(scores){
  animateGauge('ai',Number(scores.overall??scores.security??0),80);
  animateGauge('catalog',Number(scores.product_quality??0),180);
  animateGauge('security',autericProtectionScore(state.scan),280);
}

function animateResultReveal(){
  const scores=state.scan?.scores||{};
  if(!motion.ready||motion.reduce){ animatePrimaryGauges(scores); return; }
  clearReportMotion();
  const gsap=window.gsap, gauges=[$('aiGauge'),$('catalogGauge'),$('securityGauge')];
  const scoreMap=[['ai',Number(scores.overall??scores.security??0)],['catalog',Number(scores.product_quality??0)],['security',autericProtectionScore(state.scan)]];
  scoreMap.forEach(([kind,target])=>window.ScoreGauge.update($(`${kind}Gauge`).querySelector('.score-meter'),target===null?null:0));
  state.resultTimeline=gsap.timeline({defaults:{ease:motion.ease}})
    .fromTo('.report-store-heading',{autoAlpha:0,y:-12},{autoAlpha:1,y:0,duration:.38},0)
    .fromTo('.score-stage-head',{autoAlpha:0,y:22},{autoAlpha:1,y:0,duration:.48},.05)
    .fromTo('.score-instrument-panel',{autoAlpha:0,y:24,scale:.985},{autoAlpha:1,y:0,scale:1,duration:.56},.11)
    .fromTo('.catalog-teaser',{autoAlpha:0,x:24,rotation:1.2},{autoAlpha:1,x:0,rotation:0,duration:.62},.18)
    .fromTo('.primary-gauges',{autoAlpha:0,scale:.985},{autoAlpha:1,scale:1,duration:.46},.12)
    .fromTo(gauges,{autoAlpha:0,scale:.92},{autoAlpha:1,scale:1,duration:.64,stagger:.09,clearProps:'transform,opacity,visibility'},.18);
  scoreMap.forEach(([kind,target],index)=>{
    const meter=$(`${kind}Gauge`).querySelector('.score-meter'), counter={value:0}, at=.34+(index*.09);
    if(target===null){window.ScoreGauge.update(meter,null);return;}
    state.resultTimeline.to(counter,{value:target,duration:.9,ease:'power3.out',onUpdate:()=>window.ScoreGauge.update(meter,counter.value)},at);
  });
  state.resultTimeline.fromTo('.score-breakdown',{autoAlpha:0,y:10},{autoAlpha:1,y:0,duration:.32},.82);
}

function setupReportScrollMotion(){
  if(!motion.ready||motion.reduce||!window.ScrollTrigger)return;
  const gsap=window.gsap;
  ['#privacyNotice','#findingsSection','#protocolSection','#catalogSection','.technical-section','#protectionCallout'].forEach(selector=>{
    const element=document.querySelector(selector); if(!element||element.classList.contains('hidden'))return;
    const tween=gsap.fromTo(element,{autoAlpha:0,y:26},{autoAlpha:1,y:0,duration:.52,ease:motion.ease,scrollTrigger:{trigger:element,start:'top 86%',once:true}});
    if(tween.scrollTrigger)state.scrollTriggers.push(tween.scrollTrigger);
  });
  window.ScrollTrigger.refresh();
}

async function createScan(target, adapter='auto'){
  state.lastTarget=target;
  showProgress(target);
  try{
    track('scan_started',{target_domain:host(target),adapter,landing_page:location.pathname});
    const res = await api('/api/scans',{method:'POST',body:JSON.stringify({target_url:target,adapter})});
    state.scanId=res.scan_id; const reportHost=host(target); history.replaceState({},'',`/store/${encodeURIComponent(reportHost)}?id=${encodeURIComponent(state.scanId)}`); await pollScan();
  }catch(e){ track('scan_failed',{message:e.message||'unknown'}); showScanError(e.message); }
}
async function pollScan(){
  for(let i=0;i<240;i++){
    const scan=await api(`/api/scans/${state.scanId}?include_products=false`); setProgress(scan.progress||0,scan.progress_message||'Scanning');
    if(scan.status==='completed'){ location.replace(`/store/${encodeURIComponent(host(scan.target_url))}?id=${encodeURIComponent(state.scanId)}`); return; }
    if(scan.status==='failed') throw new Error(scan.error || 'Scan failed');
    await new Promise(r=>setTimeout(r,350));
  }
  throw new Error('Scan did not finish in time');
}

function metric(label,value,sub=''){ return `<div class="metric-card"><span>${esc(label)}</span><strong>${esc(value)}</strong>${sub?`<small>${esc(sub)}</small>`:''}</div>`; }
function findingCard(f){
  const meta=[f.category,`confidence ${Math.round(Number(f.confidence||0)*100)}%`].filter(Boolean).join(' · ');
  const fix=f.recommendation?.how||''; const why=f.recommendation?.why||'';
  const detailAvailable=!['pass','unsupported'].includes(f.status)||Boolean(fix)||hasEvidenceData(f.evidence);
  return `<details class="finding-card ${esc(f.severity)}" data-severity="${esc(f.severity)}" data-status="${esc(f.status)}" data-category="${esc(f.category)}"><summary><i aria-hidden="true"></i><span><span class="finding-title-row"><strong>${esc(f.title)}</strong><span class="finding-state ${esc(f.status)}">${esc(resultMeaning(f.status))}</span></span><small>${esc(f.summary)}</small></span><b class="finding-toggle" aria-hidden="true">+</b></summary><div class="finding-body"><p><b>What Auteric observed:</b> ${esc(f.summary)}</p>${why?`<p><b>Why it matters:</b> ${esc(why)}</p>`:''}${fix?`<p class="finding-fix"><b>Auteric action:</b> ${esc(fix)}</p>`:''}<div class="finding-foot"><span class="meta">${esc(meta)}</span>${detailAvailable?`<button type="button" data-evidence="${esc(f.id)}">Review finding</button>`:''}</div></div></details>`;
}
function checkCard(c){
  const detailAvailable=!['pass','unsupported'].includes(c.status)||Boolean(c.recommendation)||hasEvidenceData(c.evidence);
  return `<article class="test-card ${esc(c.status)}" data-test-status="${esc(c.status)}" data-test-category="${esc(c.category)}" data-test-search="${esc(`${c.title} ${c.method} ${c.exposure}`.toLowerCase())}"><div class="test-card-main"><div class="test-title-row"><h3>${esc(c.title)}</h3><span class="finding-state ${esc(c.status)}">${esc(resultMeaning(c.status))}</span></div><p class="method"><b>How Auteric checked it:</b> ${esc(c.method)}</p><p class="test-result"><b>Observed:</b> ${esc(c.result||statusLabel(c.status))}</p></div><div class="test-exposure"><span>Impact on AI shopping</span><p>${esc(c.exposure)}</p>${c.recommendation?`<small><b>Auteric action:</b> ${esc(c.recommendation)}</small>`:''}</div>${detailAvailable?`<button data-check-evidence="${esc(c.id)}">Review finding</button>`:''}</article>`;
}
function attackCard(a){
  const label=a.status==='confirmed_gap'?'Confirmed gap':a.status==='needs_review'?'Needs review':'Observed exposure';
  return `<article class="attack-card ${esc(a.severity||'medium')}"><div class="attack-top"><span class="exposure-label">${esc(label)}</span></div><h3>${esc(a.title)}</h3><p>${esc(a.summary)}</p><div class="attack-control"><span>Based on control</span><strong>${esc(a.control||'Evidence-backed control')}</strong></div></article>`;
}
function lockedAttackCard(a, idx){
  const visible=idx<4;
  if(visible){
    return `<article class="locked-attack teaser"><div class="runtime-teaser"><span class="runtime-number">0${idx+1}</span><div><h3>${esc(a.title)}</h3><p>${esc(a.summary)}</p></div></div><div class="runtime-lock-row"><span>Not tested by the public scan</span><strong>Gateway required</strong></div></article>`;
  }
  return `<article class="locked-attack"><div class="locked-content"><h3>${esc(a.title)}</h3><p>${esc(a.summary)}</p><small>Runtime evidence has not been collected by this public scan.</small></div><div class="lock-overlay"><strong>Additional runtime control</strong><span>Merchant-authorized verification is required</span></div></article>`;
}

function priorityActionCard(f, idx){
  const stateLabel=f.status==='fail'?'Confirmed gap':f.status==='warning'?'Needs attention':'Not verified';
  return `<article class="priority-action"><span class="priority-index">${String(idx+1).padStart(2,'0')}</span><div><strong>${esc(f.title)}</strong><p>${esc(f.summary)}</p><span class="priority-meta">${esc(stateLabel)} · ${esc(f.category)}</span></div><button data-evidence="${esc(f.id)}">Review with Auteric</button></article>`;
}
function renderBusinessRisks(business){
  const data=business||{}; const items=data.items||[];
  $('businessRiskHeadline').textContent=data.headline||'Can you trust the final AI transaction?';
  $('businessRiskNarrative').textContent=data.narrative||'The public scan separates what it can prove from what still requires runtime evidence.';
  $('businessRiskGrid').innerHTML=items.slice(0,4).map((r,idx)=>{
    const cls=r.status||'not_verified';
    const icon=cls==='protected'?'✓':cls==='confirmed_gap'?'!':'○';
    return `<article class="business-risk-card ${esc(cls)}"><div class="risk-top"><span class="risk-icon">${icon}</span><span class="risk-state">${esc(r.status_label||'Protection not verified')}</span></div><span class="risk-number">0${idx+1}</span><h3>${esc(r.title)}</h3><p>${esc(r.summary)}</p><div class="risk-impact"><span>Why a merchant cares</span><strong>${esc(r.business_impact)}</strong></div>${cls==='protected'?'':`<button class="text-btn" data-gateway-cta>${esc(r.cta||'Protect this path')} →</button>`}</article>`;
  }).join('')||'<div class="empty-state"><strong>Business-risk summary unavailable.</strong><p>Rescan to generate the current merchant-facing risk model.</p></div>';
  document.querySelectorAll('#businessRiskGrid [data-gateway-cta]').forEach(b=>b.onclick=openGateway);
}

function render(scan){
  const scores=scan.scores||{}, o=scan.observations||{}, u=o.ucp_analysis||{}, protocol=o.protocol_summary||{}, protocolUcp=protocol.ucp||{}, runtime=protocol.runtime||{}, findings=scan.findings||[], checks=effectiveChecks(scan), attacks=scan.attack_surface||{public:[],locked:[]}, business=scan.business_risks||{};
  const domain=host(scan.target_url), sec=Number(scores.overall??scores.security??0);
  $('reportTitle').textContent=domain;
  $('reportTitle').title=domain;
  requestAnimationFrame(fitReportDomain);
  prepareGauge('ai',Number(scores.overall??scores.security??0));
  prepareGauge('catalog',Number(scores.product_quality??0));
  prepareGauge('security',autericProtectionScore(scan));
  const catalogEvidence=scan.catalog_summary?.catalog||o.catalog||{};
  if(catalogEvidence.complete!==true && $('catalogGaugeStatus'))$('catalogGaugeStatus').textContent=`${readinessLabel(Number(scores.product_quality||0))} · sample`;
  $('verdictState').textContent=checks.some(c=>c.status==='fail'&&['critical','high'].includes(c.severity))?'Action required':'Publicly scanned';
  $('siteFavicon').innerHTML=`<img src="${favicon(domain)}" alt="" width="48" height="48" onerror="this.parentElement.innerHTML='<span>${esc(domain[0]?.toUpperCase()||'V')}</span>'">`;
  const highImpact=findings.filter(f=>['critical','high'].includes(f.severity)&&['fail','warning'].includes(f.status)).length;
  const unknownHigh=checks.filter(c=>c.status==='unknown'&&['critical','high'].includes(c.severity)).length;
  $('reportSummary').textContent=scan.security_details_locked?'Public readiness results are available. Detailed security evidence requires ownership verification.':business.headline||`${highImpact} high-impact findings · ${unknownHigh} high-risk controls not verified`;
  const productTotal=Number(catalogEvidence.total_products ?? catalogEvidence.products_discovered ?? scan.products_scanned ?? scan.catalog_summary?.products ?? (scan.products||[]).length);
  const catalogScore=Number(scores.product_quality||0), discoveryScore=Number(scores.discovery||0), checkoutScore=Number(scores.checkout||0);
  const localConnection=localAutericConnected(scan);
  const runtimeProtected=!localConnection&&autericProtectionScore(scan)===100;
  const discoverable=productTotal>0 && (catalogScore>=75 || discoveryScore>=75);
  const keyShoppingBlocked=checkoutScore<75;
  const relevantGaps=checks.filter(c=>['fail','warning','unknown'].includes(c.status)&&['catalog','agent','ucp'].includes(c.category)).slice(0,2).map(c=>c.title);
  const catalogBlocked=Boolean(catalogEvidence.blocked);
  const discoveryTitle=discoverable?'Your catalog is discoverable.':catalogBlocked?'Catalog access needs verification.':'Your catalog needs visibility.';
  const protectionTitle=localConnection?'Auteric connected locally.':runtimeProtected?'Protected by Auteric.':(attacks.public||[]).length?'Agent attack exposure needs attention.':'Agent attack protection is unverified.';
  $('gaugeHeading').innerHTML=`<span>${esc(discoveryTitle)}</span><br><em>${esc(protectionTitle)}</em>`;
  $('reportSummary').textContent=catalogBlocked?'Bot protection prevented catalog verification. Connect a permitted catalog source.':`${discoverable?`${productTotal.toLocaleString()} public products found.`:'Public product discovery is not verified.'}${!protocolUcp.detected?' UCP was not detected.':''}${keyShoppingBlocked?' Cart or checkout actions are not verified.':''}`;
  $('verdictState').textContent=!discoverable||!runtimeProtected||keyShoppingBlocked?'Action required':'Publicly scanned';
  $('verdictState').dataset.state=!discoverable||!runtimeProtected||keyShoppingBlocked?'attention':'ready';
  const overall=Number(scores.overall??scores.security??0);
  $('merchantScoreReason')?.replaceChildren(document.createTextNode(overall>=100?'The public scan verified every scored signal in this model.':`Why ${overall}/100: ${relevantGaps.length?`the scan did not verify ${relevantGaps.join(' and ')}.`:'some catalog, shopping or protection evidence was not verified.'}`));
  const localScan=isLoopbackHost(domain);
  $('merchantConnectCta').hidden=localScan;
  if(localScan){
    $('merchantConnectCta').removeAttribute('href');
    $('merchantConnectCta').removeAttribute('target');
    $('merchantConnectCta').removeAttribute('rel');
  }
  if(!localScan){
    $('merchantConnectCta').href=`/onboarding/${encodeURIComponent(domain)}`;
    $('merchantConnectCta').textContent=runtimeProtected?'Review verified controls':discoverable?'Prepare safer AI checkout':'Make my store discoverable safely';
    $('merchantConnectCta').target='_blank'; $('merchantConnectCta').rel='noopener noreferrer';
  }
  document.querySelectorAll('[data-report-onboarding]').forEach(link=>{ link.hidden=localScan; if(!localScan){link.href=`/onboarding/${encodeURIComponent(domain)}`; link.target='_blank'; link.rel='noopener noreferrer';} });
  $('securityGaugeName').textContent=localConnection?'Auteric Local Connection':'Auteric Protection';
  if(localConnection){
    const tools=(o.local_auteric?.active_operations||[]).length;
    $('securityGaugeStatus').textContent=`${tools} tool${tools===1?'':'s'} tested locally`;
  }else if(!runtimeProtected)$('securityGaugeStatus').textContent='Safe test needed';
  if($('catalogTitleCount'))$('catalogTitleCount').textContent=productTotal.toLocaleString();
  $('lastScanLabel').textContent=scan.completed_at ? `Checked ${dateText(scan.completed_at)}` : 'Scan complete';
  const chips=[['Platform',scan.platform||'Storefront'],['UCP',o.ucp_status||'Not detected'],['Checked',scan.completed_at?new Date(scan.completed_at).toLocaleDateString():'Complete']];
  $('vitalsChips').innerHTML=chips.map(([a,b])=>`<span>${esc(a)} <b>${esc(b)}</b></span>`).join('');
  $('statusVitals').innerHTML=[['Latency',o.latency_ms!==null&&o.latency_ms!==undefined?`${o.latency_ms}ms`:'N/V'],['UCP',o.ucp_status||'unknown'],['Coverage',`${scores.coverage||0}%`]].map(([a,b])=>`<div><span>${esc(a)}</span><strong>${esc(b)}</strong></div>`).join('');
  $('statusNarrative').textContent=scan.security_details_locked?`${domain} has a public AI shopping readiness report. Security details are protected.`:highImpact?`${domain} has ${highImpact} high-impact security issue${highImpact===1?'':'s'} to review.`:`${domain} has no verified critical/high failures in the public scan.`;
  $('privacyNotice')?.classList.toggle('hidden',!scan.security_details_locked);

  const layers=[['AI visibility',scores.discovery,'tests'],['Catalog & product data',scores.product_quality,'catalog'],['Agent discoverability',scores.channel,'ucp'],['UCP conformance',protocolUcp.detected?(scores.protocol_conformance??scores.ucp):null,'ucp'],['Checkout readiness',scores.checkout,'attacks'],[localConnection?'Auteric local connection':'Auteric protection',autericProtectionScore(scan),'attacks']];
  $('layerList').innerHTML=layers.map(([name,val,view])=>`<button class="layer-row" type="button" data-score-view="${view}" aria-label="Open ${esc(name)} details, score ${esc(scoreText(val))}"><span>${esc(name)}</span><div class="layer-bar" role="progressbar" aria-label="${esc(name)} score" aria-valuenow="${val===null||val===undefined?0:clamp(val)}" aria-valuemin="0" aria-valuemax="100"><i style="width:${val===null||val===undefined?0:clamp(val)}%"></i></div><b>${esc(scoreText(val))}</b></button>`).join('');
  document.querySelectorAll('[data-score-view]').forEach(button=>button.onclick=()=>openReportView(button.dataset.scoreView));
  const botValues=Object.values(o.ai_bot_access||{}), allowedBots=botValues.filter(Boolean).length;
  const ucpState=o.ucp_status==='verified'?'Verified':o.ucp_status==='invalid'?'Invalid':'Not detected';
  const hasUcp=Boolean(protocolUcp.detected), conformanceScore=Number(protocolUcp.conformance_score??scores.protocol_conformance??scores.ucp??0), conformanceGrade=hasUcp?(protocolUcp.grade||grade(conformanceScore)):'—';
  const protocolCaps=Array.isArray(protocolUcp.capabilities)?protocolUcp.capabilities:(u.capability_details||[]);
  const primaryBlocker=(protocolUcp.biggest_blockers||[])[0];
  $('protocolConformance').innerHTML=`<div class="protocol-grade ${hasUcp?`grade-${esc(conformanceGrade)}`:''}"><span>${hasUcp?'UCP CONFORMANCE':protocol.acp?.detected?'ACP DETECTED':'PROTOCOL STATUS'}</span><strong>${esc(conformanceGrade)}</strong><small>${hasUcp?`${esc(scoreText(conformanceScore))} / 100`:'Not scored'}</small></div><div class="protocol-conformance-copy"><span class="eyebrow">AUTOMATED PUBLIC ASSESSMENT</span><h3>${esc(protocol.agent_interface||'No standardized commerce interface detected')}</h3><p>${hasUcp?'The grade measures the public UCP manifest, schema bindings and declared agent transport. It does not certify private checkout execution or runtime protection.':protocol.acp?.detected?'ACP evidence was detected; the scanner reports the interface without substituting a UCP conformance grade.':'No standardized public UCP or ACP interface was detected.'}</p>${hasUcp&&primaryBlocker?`<div class="protocol-blocker"><span>Biggest blocker</span><strong>${esc(primaryBlocker.title)}</strong></div>`:''}</div><dl class="protocol-facts"><div><dt>Commerce protocol</dt><dd>${esc(protocol.commerce_protocol||'None detected')}</dd></div><div><dt>ACP</dt><dd>${protocol.acp?.detected?'Detected':protocol.acp?.status==='unable_to_verify'?'Unable to verify':protocol.acp?'Not detected':'Not yet tested'}</dd></div><div><dt>Auteric runtime</dt><dd class="${runtimeProtected?'pass':'unknown'}">${esc(runtimeProtected?'Runtime protection verified':runtime.label||'Not detected')}</dd></div></dl>`;
  $('overviewCapabilityRows').innerHTML=protocolCaps.length?protocolCaps.map(cap=>`<tr><td><strong>${esc(cap.label||cap.id)}</strong><small class="capability-id">${esc(cap.id||'')}</small></td><td>${esc(cap.kind==='extension'?'Extension':'Core')}</td><td>${esc(cap.version||'N/V')}</td><td><span class="table-status ${cap.binding==='bound'?'pass':cap.binding==='failed'?'fail':'unknown'}">${esc(cap.binding==='bound'?'Bound':cap.binding==='failed'?'Failed':'Declared')}</span></td></tr>`).join(''):'<tr><td colspan="4" class="muted">No UCP or ACP capabilities were detected from the public interface.</td></tr>';
  const protocolItems=[
    ['UCP',protocolUcp.detected?`${conformanceGrade} · ${scoreText(conformanceScore)}/100`:ucpState,protocolUcp.detected?'verified':o.ucp_status==='invalid'?'attention':'unknown'],
    ['Agent interface',protocol.agent_interface||'Not detected',protocol.commerce_protocol&&protocol.commerce_protocol!=='None detected'?'verified':'unknown'],
    ['ACP',protocol.acp?.detected?'Detected':protocol.acp?.status==='unable_to_verify'?'Unable to verify':protocol.acp?'Not detected':'Not yet tested',protocol.acp?.detected?'verified':'unknown'],
    ['AI crawler access',botValues.length?`${allowedBots}/${botValues.length} allowed`:'Not verified',botValues.length&&allowedBots===botValues.length?'verified':botValues.length?'attention':'unknown'],
    ['Checkout signals',scores.checkout===null||scores.checkout===undefined?'Not verified':`${scoreText(scores.checkout)}/100`,Number(scores.checkout||0)>=75?'verified':Number(scores.checkout||0)>=55?'attention':'unknown'],
    ['Auteric runtime',runtimeProtected?'Protected':runtime.enforcement_verified?'Runtime evidence observed':'Not verified',runtimeProtected?'verified':'unknown']
  ];
  $('protocolStrip').innerHTML=protocolItems.map(([name,value,status])=>`<div class="protocol-item ${status}"><span>${esc(name)}</span><strong>${esc(value)}</strong></div>`).join('');
  const publicChecks=checks.filter(c=>c.category!=='transaction');
  const counts={fail:publicChecks.filter(c=>c.status==='fail').length,warning:publicChecks.filter(c=>c.status==='warning').length,unknown:publicChecks.filter(c=>c.status==='unknown').length,pass:publicChecks.filter(c=>c.status==='pass').length};
  const runtimeUnknown=checks.filter(c=>c.category==='transaction'&&c.status==='unknown').length;
  const actionCount=counts.fail+counts.warning;
  const resultTotal=counts.fail+counts.warning+counts.unknown+counts.pass;
  const known=counts.fail+counts.warning+counts.pass;
  $('openFindingCount').textContent=actionCount;
  const summaryRows=[];
  summaryRows.push(`<div class="proof-line"><span>Public controls with a result</span><strong>${known}/${publicChecks.length}</strong></div>`);
  if(counts.fail)summaryRows.push(`<div class="proof-line danger"><span>Confirmed control gaps</span><strong>${counts.fail}</strong></div>`);
  if(counts.warning)summaryRows.push(`<div class="proof-line warning"><span>Hardening / readiness actions</span><strong>${counts.warning}</strong></div>`);
  if(counts.unknown)summaryRows.push(`<div class="proof-line neutral"><span>Public controls not verified</span><strong>${counts.unknown}</strong></div>`);
  if(counts.pass)summaryRows.push(`<div class="proof-line pass"><span>Verified controls</span><strong>${counts.pass}</strong></div>`);
  if(runtimeUnknown)summaryRows.push(`<div class="proof-line gateway"><span>Runtime transaction controls requiring Gateway</span><strong>${runtimeUnknown}</strong></div>`);
  $('exposureSummary').innerHTML=summaryRows.join('');
  const blockers=findings.filter(f=>f.status!=='pass').slice(0,4); $('blockerChips').innerHTML=blockers.length?blockers.map((f,i)=>`<div class="mini-action"><span>${String(i+1).padStart(2,'0')}</span><strong>${esc(f.title)}</strong></div>`).join(''):'<div class="mini-action clean"><span>✓</span><strong>No public control gap needs immediate action</strong></div>';
  const needsProtection=Boolean(business.gateway_recommended) || highImpact>0 || runtimeUnknown>0;
  $('protectionCallout').classList.toggle('hidden',!needsProtection);
  if(needsProtection){
    const platform=String(scan.platform||'').toLowerCase();
    const exposureReady=runtime.exposure_verified===true;
    $('protectionTitle').textContent=exposureReady&&!runtimeProtected?'One safe cart check completes protection.':scan.security_details_locked?'Checkout enforcement is not yet verified.':business.headline||'Protect AI checkout before an unsafe action becomes an order';
    $('protectionText').textContent=exposureReady&&!runtimeProtected
      ? 'Your signed Auteric exposure is verified. Run one isolated cart check to confirm allowed actions through the Gateway and policy. No payment, checkout completion, order or customer cart is used.'
      : platform==='shopify'
      ? 'Agent checkout controls were not verified for this store. Set up identity, spending limits and checkout integrity checks before allowing automated purchases.'
      : business.narrative||'Agent checkout controls were not verified. Review identity, spending limits and checkout integrity before an unsafe action can become an order.';
  }
  const priority=findings.filter(f=>['fail','warning'].includes(f.status) || (f.status==='unknown' && f.id!=='transaction.connected_suite' && ['critical','high'].includes(f.severity))).slice(0,4); $('priorityFindings').innerHTML=priority.map(priorityActionCard).join('') || '<div class="empty-state good"><strong>No public control gap needs immediate action.</strong><p>Runtime transaction protections may still require connected verification.</p></div>'; $('allFindings').innerHTML=findings.map(findingCard).join('') || '<p class="muted">No findings.</p>';
  renderBusinessRisks(business);

  renderAttacks(attacks);

  const probes=u.transport_probes||[], txVerified=runtime.enforcement_verified===true;
  const chain=[['HTTPS',o.https===true?'pass':o.https===false?'fail':'unknown',o.https===true?'Verified':o.https===false?'Failed':'Unknown'],['UCP profile',o.ucp_status==='verified'?'pass':o.ucp_status==='invalid'?'fail':'unknown',o.ucp_status||'Unknown'],['Merchant keys',o.ucp_status==='verified'?(u.signing_keys_present?'pass':'fail'):'unknown',o.ucp_status==='verified'?(u.signing_keys_present?'Published':'Not published'):'Not verified'],['Transports',probes.length?(probes.every(x=>x.reachable)?'pass':'fail'):'unknown',probes.length?`${probes.filter(x=>x.reachable).length}/${probes.length} reachable`:'Not probed'],['Auteric runtime',txVerified?'pass':'unknown',txVerified?'Connected evidence':'Not detected']];
  $('trustChain').innerHTML=chain.map(([a,c,b],idx)=>`<div class="trust-step ${c}"><span class="trust-dot">${c==='pass'?'✓':c==='fail'?'!':idx===4?'⌁':'○'}</span><div><span>${esc(a)}</span><strong>${esc(b)}</strong></div></div>`).join('<i class="trust-line" aria-hidden="true"></i>');

  renderTestSummary(checks); renderTests(); renderSubsetChecks('ucpChecks',checks.filter(c=>['ucp','agent'].includes(c.category))); renderSubsetChecks('webChecks',checks.filter(c=>['transport','web'].includes(c.category)));
  renderUcp(scan,u,o); renderWeb(scan,o,scores);
  const publicCatalogTotal=Number(catalogEvidence.total_products ?? catalogEvidence.products_discovered ?? scan.products_scanned ?? scan.catalog_summary?.products ?? 0);
  state.catalog={products:[],page:1,page_count:0,total:publicCatalogTotal};
  setCatalogExpanded(false);
  renderCatalogTeaser([],publicCatalogTotal,scan.catalog_summary?.catalog||{});
  renderCatalog(scan, [], checks); loadCatalogPage(1); bindEvidence();
}

function renderAttacks(attacks){
  const pub=attacks.public||[], locked=attacks.locked||[];
  $('publicAttacks').innerHTML=pub.length?pub.map(attackCard).join(''):'<article class="empty-attack"><strong>No public attack path was verified from failed controls.</strong><p>This does not prove transaction safety. Connected runtime boundaries remain unverified until the Enforcement Gateway tests them.</p></article>';
  $('attackPreview').innerHTML=(pub.slice(0,3).map(attackCard).join('') || '<div class="attack-preview-safe"><strong>No public high-confidence attack path verified</strong><span>Runtime attack coverage still requires the gateway.</span></div>') + `<div class="attack-preview-lock"><span>+${esc(attacks.locked_count||locked.length)} connected runtime tests</span><button data-go="attacks">View locked coverage</button></div>`;
  $('lockedAttackCount').textContent=`${locked.length} connected tests`; $('lockedAttacks').innerHTML=locked.slice(0,8).map(lockedAttackCard).join('');
  document.querySelectorAll('[data-go="attacks"]').forEach(b=>b.onclick=()=>openReportView('attacks'));
}

function registryEntries(obj){
  if(!obj||typeof obj!=='object')return [];
  const out=[]; Object.entries(obj).forEach(([name,val])=>{ const arr=Array.isArray(val)?val:[val]; arr.forEach(x=>{if(x&&typeof x==='object')out.push([name,x])}); }); return out;
}
function renderUcp(scan,u,o){
  const profile=o.ucp_profile||{}, hasManifest=hasEvidenceData(profile), root=hasManifest?(profile.ucp||profile):{};
  const protocol=o.protocol_summary||{}, protocolUcp=protocol.ucp||{};
  $('ucpSummary').innerHTML=[metric('Conformance',protocolUcp.detected?`${protocolUcp.grade||grade(protocolUcp.conformance_score||0)} · ${scoreText(protocolUcp.conformance_score||0)}/100`:'Not scored'),metric('Profile',o.ucp_status||'unknown'),metric('Version',u.version||'not verified'),metric('Capabilities',u.capability_count||0),metric('Transports',(u.transports||[]).map(x=>x.toUpperCase()).join(', ')||'none'),metric('Payments',u.payment_handler_count??0),metric('Merchant keys',u.signing_keys_present?'Published':'Not published'),metric('ACP',protocol.acp?.detected?'Detected':protocol.acp?.status==='unable_to_verify'?'Unable to verify':protocol.acp?'Not detected':'Not yet tested')].join('');
  const detailedCaps=Array.isArray(protocolUcp.capabilities)?protocolUcp.capabilities:[];
  const rawCaps=registryEntries(root.capabilities);
  $('capabilityRows').innerHTML=detailedCaps.length?detailedCaps.map(cap=>`<tr><td><strong>${esc(cap.label||cap.id)}</strong></td><td>${esc(cap.version||'N/V')}</td><td><span class="table-status ${cap.binding==='bound'?'pass':cap.binding==='failed'?'fail':'unknown'}">${esc(cap.binding==='bound'?'Bound':cap.binding==='failed'?'Failed':'Declared')}</span></td></tr>`).join(''):rawCaps.length?rawCaps.map(([name,x])=>`<tr><td><strong>${esc(name.replace(/^dev\.[^.]+\./,''))}</strong></td><td>${esc(x.version||'N/V')}</td><td><span class="table-status unknown">Declared</span></td></tr>`).join(''):'<tr><td colspan="3" class="muted">No UCP capabilities verified.</td></tr>';
  const vitals=[['UCP status',o.ucp_status||'unknown'],['Endpoint',`${new URL(scan.target_url).origin}/.well-known/ucp`],['HTTP status',o.ucp_http_status??'N/V'],['UCP version',u.version||'N/V'],['Transports',(u.transports||[]).join(', ')||'none'],['Last checked',dateText(scan.completed_at)]]; $('technicalVitals').innerHTML=vitals.map(([a,b])=>`<div><span>${esc(a)}</span><strong>${esc(b)}</strong></div>`).join('');
  const bots=o.ai_bot_access||{}; $('botAccess').innerHTML=Object.keys(bots).length?Object.entries(bots).map(([name,allowed])=>`<div><span><strong>${esc(name)}</strong><small>AI crawler policy</small></span><span class="status-pill ${allowed?'pass':'fail'}">${allowed?'ALLOWED':'BLOCKED'}</span></div>`).join(''):'<p class="muted">No AI-specific robots rules verified.</p>';
  const pays=registryEntries(root.payment_handlers); $('paymentCards').innerHTML=pays.length?pays.map(([name,x])=>`<article class="payment-card"><div><strong>${esc(name)}</strong><span>${esc(x.id||'handler')}</span></div><p>Version ${esc(x.version||'N/V')}</p><small>${esc(x.config?.merchant_info?.merchant_name||x.config?.gateway||'Declared by UCP profile')}</small></article>`).join(''):'<p class="muted">No payment handlers declared.</p>';
  const files=o.standard_files||{}; const fileDefs=[['llms.txt',files.llms_txt],['agents.md',files.agents_md],['security.txt',files.security_txt],['sitemap.xml',files.sitemap]];
  $('agentFiles').innerHTML=fileDefs.map(([name,f])=>{f=f||{}; const has=!!f.present; const content=f.content?`<pre>${esc(f.content)}</pre>`:''; return `<details class="agent-file ${has?'present':'missing'}"><summary><span><strong>${esc(name)}</strong><small>${has?`${esc(f.size_bytes||0)} bytes · ${esc(f.sha256||'no hash')}`:'Not found'}</small></span><span class="status-pill ${has?'pass':'unknown'}">${has?'FOUND':'N/A'}</span></summary>${content||`<p>${has?'Content capture not enabled for this file.':'No public file was verified.'}</p>`}</details>`;}).join('');
  $('ucpRawPanel')?.classList.toggle('hidden',!hasManifest);
  $('ucpEvidence').textContent=hasManifest?pretty(profile):'';
}
function renderWeb(scan,o,scores){
  const tls=o.tls||{}, cookies=o.cookie_security||{}, files=o.standard_files||{}, hdr=o.security_header_analysis||{};
  $('webSummary').innerHTML=[metric('Web score',scoreText(scores.web_security)),metric('TLS',tls.version||'not verified'),metric('Certificate',tls.certificate_valid===true?'Valid':tls.certificate_valid===false?'Failed':'Unknown'),metric('Cookies',cookies.count??0),metric('CSP',hdr.csp?.present?'Present':'Missing'),metric('security.txt',files.security_txt?.present?'Present':'Not found')].join('');
}

function renderTestSummary(checks){
  const count=status=>checks.filter(x=>x.status===status).length;
  const rows=[['Verified controls',count('pass'),'Scanner produced positive evidence'],['Confirmed gaps',count('fail'),'The control was tested and failed'],['Needs attention',count('warning'),'Hardening or readiness issue observed'],['Not verified',count('unknown'),'No safe/vulnerable claim without more evidence']].filter(([,n])=>n>0);
  $('testSummary').innerHTML=rows.map(([a,b,c])=>metric(a,b,c)).join('')||metric('Tests','0','Run or rescan to build the current evidence matrix');
}
function renderSubsetChecks(containerId, checks){ const el=$(containerId); el.innerHTML=checks.slice(0,12).map(checkCard).join('') || '<div class="empty-state"><strong>No control records were available for this surface.</strong><p>Re-run the scan to rebuild evidence with the current scanner version.</p></div>'; }
function renderTests(){
  if(!state.scan)return; const q=($('testSearch')?.value||'').trim().toLowerCase();
  const baseChecks=effectiveChecks(state.scan); if($('testsNotice')){$('testsNotice').classList.toggle('hidden',!state.checksDerived);} const checks=baseChecks.filter(c=>{ const filter=state.testFilter; const statusMatch=filter==='all'||c.status===filter||c.category===filter; const searchMatch=!q||`${c.title} ${c.method} ${c.exposure}`.toLowerCase().includes(q); return statusMatch&&searchMatch; });
  $('securityTests').innerHTML=checks.map(checkCard).join('')||'<p class="muted">No tests match this filter.</p>'; bindEvidence();
}
function fieldLabel(key){ return ({description:'Description',price:'Price',image:'Images',brand:'Brand',sku:'SKU',gtin:'GTIN / barcode',availability:'Availability',category:'Category',canonical_url:'Product URL',variants:'Variants (supplementary)'}[key]||key); }
function availabilityLabel(value){ const v=String(value||'').toLowerCase(); if(v.includes('outofstock')||v.includes('out_of_stock'))return 'Out of stock'; if(v.includes('instock')||v.includes('in_stock'))return 'In stock'; return value||'Stock not verified'; }
function renderCatalogTeaser(products,total,catalog={}){
  const grid=$('catalogTeaserGrid'), count=$('catalogTeaserCount'), meta=$('catalogTeaserMeta');
  if(!grid||!count||!meta)return;
  const discovered=Number(total||products.length||0), complete=catalog.complete===true, lazy=catalog.lazy_pagination===true;
  count.textContent=discovered?`${discovered.toLocaleString()} products`:'No products';
  meta.textContent=discovered
    ? (lazy?`${Number(catalog.products_scanned||products.length||50)} analyzed now · the next 50 load on demand`:`${complete?'Complete public catalog':'Public evidence sample'} · ${Number(catalog.pages_fetched||1)} source page${Number(catalog.pages_fetched||1)===1?'':'s'}`)
    : 'No usable public product evidence was returned.';
  if(!products.length){
    grid.innerHTML='<div class="catalog-teaser-empty"><i></i><i></i><i></i><span>Catalog preview is loading</span></div>';
    return;
  }
  grid.innerHTML=products.slice(0,3).map((product,index)=>{
    const img=safeUrl((product.images||[])[0]), price=(product.price!==null&&product.price!==undefined)?formatPrice(product):'Price not verified';
    return `<button class="catalog-teaser-card" type="button" data-teaser-product="${index}" aria-label="Open ${esc(product.title)} in the full catalog"><span class="catalog-teaser-media ${img?'':'placeholder'}">${img?`<img src="${esc(img)}" alt="" loading="lazy" draggable="false" referrerpolicy="no-referrer" />`:'<b>NO IMAGE</b>'}</span><span class="catalog-teaser-copy"><strong dir="auto">${esc(product.title)}</strong><small>${esc(price)}</small></span><em aria-hidden="true">↘</em></button>`;
  }).join('');
  grid.querySelectorAll('[data-teaser-product]').forEach(button=>button.onclick=()=>openReportView('catalog'));
  if(motion.ready&&!motion.reduce)window.gsap.fromTo(grid.children,{autoAlpha:0,y:16,rotation:1.4},{autoAlpha:1,y:0,rotation:0,duration:.42,stagger:.07,ease:motion.ease,clearProps:'opacity,visibility,transform'});
}
function renderCatalog(scan, products, checks){
  const summary=scan.catalog_summary||{}; const coverage=summary.coverage||{};
  const catalog=summary.catalog||{}; const complete=catalog.complete===true, capped=catalog.capped===true;
  const sampleTotal=Number(summary.products ?? scan.products_scanned ?? products.length), total=Number(catalog.total_products ?? catalog.products_discovered ?? sampleTotal);
  if($('catalogTitleCount'))$('catalogTitleCount').textContent=total.toLocaleString();
  $('catalogHeadline').innerHTML=`<div><span>With images</span><strong>${Number(summary.with_images??0).toLocaleString()}</strong></div><div><span>With price</span><strong>${Number(summary.with_price??0).toLocaleString()}</strong></div><div><span>Availability</span><strong>${Number(summary.with_availability??0).toLocaleString()}</strong></div>`;
  $('catalogScope').textContent=catalog.lazy_pagination
    ? `${total.toLocaleString()} public product URLs counted via the store sitemap. Quality statistics above use the ${sampleTotal.toLocaleString()} products analyzed initially; each catalog page loads and inspects up to 50 more.`
    : (catalog.source?`${complete?'Complete catalog':'Catalog sample'} from ${catalog.source.replace(/_/g,' ')} · ${catalog.pages_fetched||1} source page${Number(catalog.pages_fetched||1)===1?'':'s'}${capped?` · capped at ${catalog.max_products||total}`:''}.`:`Catalog pagination is loading.`);
  const order=['image','price','availability','description','brand','sku','gtin','category','canonical_url','variants'];
  $('catalogCoverageChart').innerHTML=order.filter(k=>coverage[k]).map(k=>{const x=coverage[k]; const unknown=!x.count && x.not_checked===x.total; return `<div class="coverage-row"><div><span>${esc(fieldLabel(k))}</span><b>${unknown?'Not checked':`${esc(x.count)}/${esc(x.total)}`}</b></div><div class="coverage-track" role="progressbar" aria-label="${esc(fieldLabel(k))} captured presence" aria-valuenow="${esc(x.percent)}" aria-valuemin="0" aria-valuemax="100"><i style="width:${clamp(x.percent)}%"></i></div><em>${unknown?'—':`${esc(x.percent)}%`}</em></div>`}).join('')||'<p class="muted">No catalog field coverage available.</p>';
  const common=summary.common_gaps||[];
  const probe=summary.ucp_catalog_probe||((scan.observations?.ucp_analysis||{}).catalog_probe)||{};
  let probeText='No UCP live catalog probe was applicable.'; let probeClass='unknown';
  if(probe.attempted){ probeClass=probe.ok?'pass':'fail'; probeText=probe.ok?`Live ${String(probe.transport||'UCP').toUpperCase()} catalog search returned ${probe.product_count||0} parseable product records using ${probe.tool||'a catalog search tool'}.`:`Live catalog search was attempted but did not return a usable product sample${probe.error?`: ${probe.error}`:''}.`; }
  else if(probe.reason){ probeText=`Live UCP catalog probe not run: ${probe.reason}.`; }
  $('catalogProbe').innerHTML=`<div class="probe-state ${probeClass}"><span class="status-pill ${probeClass==='pass'?'pass':probeClass==='fail'?'fail':'unknown'}">${probeClass==='pass'?'VERIFIED':probeClass==='fail'?'ATTENTION':'NOT APPLICABLE'}</span><div><strong>${esc(probeText)}</strong><p>We also inspect public API, JSON-LD and storefront evidence for response completeness, variants, pricing and catalog consistency.</p></div></div>`;
  if(probe.ok && probe.shape){ const shape=probe.shape; $('catalogProbe').innerHTML+=`<p>Separate UCP/MCP sample — not merged into storefront coverage: ${['description','price','image','availability'].map(k=>`${esc(fieldLabel(k))}: ${esc(shape[k]||0)}/${esc(shape.sampled_products||0)}`).join(' · ')}. Search filters and pagination may differ from the storefront.</p>`; }
  renderSubsetChecks('catalogChecks',(checks||[]).filter(c=>c.category==='catalog'));
  renderProducts(products, common.map(x=>x.field));
}
function renderProducts(products, commonGapFields=[]){
  const common=new Set(commonGapFields);
  $('productGrid').innerHTML=products.map((p,index)=>{ const img=safeUrl((p.images||[])[0]); const audit=p.audit||{}, fields=audit.fields||{}; const uniqueGaps=Object.entries(fields).filter(([k,v])=>!v&&!common.has(k)).map(([k])=>fieldLabel(k)); const price=(p.price!==null&&p.price!==undefined)?formatPrice(p):''; const available=availabilityLabel(p.availability); return `<article class="product-card" style="--product-index:${Math.min(index,8)}"><div class="product-media ${img?'':'placeholder'}">${img?`<img src="${esc(img)}" alt="${esc(p.title)}" loading="lazy" draggable="false" referrerpolicy="no-referrer" />`:'<span>No image verified</span>'}</div><div class="product-body"><div class="product-head"><div><h3 dir="auto">${esc(p.title)}</h3>${price?`<strong class="product-price">${esc(price)}</strong>`:''}</div></div><div class="product-card-meta"><span class="product-stock">${esc(available)}</span><span class="product-readiness ${uniqueGaps.length?'attention':'ready'}">${uniqueGaps.length?'Needs review':'Ready'}</span></div><button class="product-detail-trigger" type="button" data-product-detail="${index}">View product</button></div></article>`; }).join('')||'<div class="empty-state"><strong>No products match these controls.</strong><p>Try clearing the search or selecting All products.</p></div>';
  document.querySelectorAll('[data-product-detail]').forEach(button=>button.onclick=()=>openProductDetail(Number(button.dataset.productDetail)));
}

function openProductDetail(index){
  const product=state.catalog.products?.[index]; if(!product)return;
  const img=safeUrl((product.images||[])[0]), url=safeUrl(product.url), audit=product.audit||{}, fields=audit.fields||{};
  const fieldRows=[['Price',product.price===null||product.price===undefined?'Unable to verify':formatPrice(product)],['Availability',availabilityLabel(product.availability)],['Brand',product.brand||'Not detected'],['SKU',product.sku||'Not detected'],['GTIN / barcode',product.gtin||'Not detected'],['Variants',(product.variants||[]).length],['Structured data',product.structured_data?'Detected':'Not detected'],['Source',product.source==='api'?'Connected data':product.source==='external_scan'?'Storefront data':product.source||'Not detected']];
  const gaps=Object.entries(fields).filter(([,value])=>!value).map(([key])=>fieldLabel(key));
  $('productDetail').innerHTML=`<div class="product-detail-layout"><div class="product-detail-media ${img?'':'placeholder'}">${img?`<img src="${esc(img)}" alt="${esc(product.title)}" />`:'<span>No product image was verified</span>'}</div><div><span class="eyebrow">CATALOG PRODUCT</span><h2 dir="auto">${esc(product.title)}</h2><p dir="auto">${esc(product.description||'No public product description was verified.')}</p><div class="product-detail-fields">${fieldRows.map(([label,value])=>`<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('')}</div><div class="product-detail-status"><strong>${gaps.length?`${gaps.length} data gap${gaps.length===1?'':'s'} detected`:'Core product fields look complete'}</strong><p>${gaps.length?esc(gaps.join(', ')):'Based on the public product evidence collected in this scan.'}</p></div>${url?`<a class="btn secondary" href="${esc(url)}" target="_blank" rel="noopener">Open storefront product</a>`:''}</div></div>`;
  const detailStatus=$('productDetail').querySelector('.product-detail-status');
  if(detailStatus){
    detailStatus.querySelector('strong').textContent=gaps.length?'Fields not verified in captured sources':'Core fields present in captured sources';
    detailStatus.querySelector('p').textContent=Object.entries(audit.field_states||{}).filter(([,value])=>value!=='present').map(([key,value])=>`${fieldLabel(key)}: ${value==='not_checked'?'not checked / applicability unknown':'not found in inspected source'}`).join(' · ') || 'Presence is not an independent accuracy or completeness certification.';
  }
  $('productModal').showModal(); track('product_opened',{product_id:product.id||null});
}

function renderCatalogPagination(data){
  const nav=$('catalogPagination'); if(!nav)return;
  if(!data.total){ nav.innerHTML=''; $('catalogCount').textContent='No catalog products were found'; return; }
  const pageStart=(data.page-1)*data.page_size+1, pageEnd=Math.min(data.catalog_total,pageStart+Math.max(0,Number(data.page_products??data.returned))-1);
  if(data.pagination_mode==='on_demand'){
    const controlsActive=Boolean(data.query)||data.filter!=='all';
    const scanMode=data.page===1?'included in the initial scan':'scanned on demand';
    $('catalogCount').textContent=controlsActive
      ? `${data.returned} match${data.returned===1?'':'es'} on this page · products ${pageStart}–${pageEnd} of ${Number(data.catalog_total).toLocaleString()}`
      : `Showing ${pageStart}–${pageEnd} of ${Number(data.catalog_total).toLocaleString()} · ${scanMode}`;
  }else{
    const start=(data.page-1)*data.page_size+1, end=start+data.returned-1;
    const scope=data.total===data.catalog_total?`${data.total} catalog products`:`${data.total} matching · ${data.catalog_total} scanned total`;
    $('catalogCount').textContent=`Showing ${start}–${end} of ${scope}`;
  }
  const pages=Array.from(new Set([1,data.page-1,data.page,data.page+1,data.page_count].filter(x=>x>=1&&x<=data.page_count))).sort((a,b)=>a-b);
  nav.innerHTML=`<button data-catalog-page="${data.page-1}" ${data.has_previous?'':'disabled'}>Previous</button>${pages.map(p=>`<button data-catalog-page="${p}" class="${p===data.page?'active':''}" ${p===data.page?'aria-current="page"':''}>${p}</button>`).join('')}<button data-catalog-page="${data.page+1}" ${data.has_next?'':'disabled'}>Next</button>`;
  nav.querySelectorAll('[data-catalog-page]').forEach(b=>b.onclick=()=>loadCatalogPage(Number(b.dataset.catalogPage),true));
}

async function loadCatalogPage(page=1,scrollToProducts=false){
  if(!state.scanId||!state.scan)return;
  const scanId=state.scanId, q=($('productSearch')?.value||'').trim(), filter=$('catalogFilter')?.value||'all', sort=$('catalogSort')?.value||'default';
  try{
    $('catalogCount').textContent=page>1?'Loading and analyzing the next 50 products…':'Loading catalog…';
    const data=await api(`/api/scans/${encodeURIComponent(scanId)}/catalog?page=${Math.max(1,page)}&limit=50&filter=${encodeURIComponent(filter)}&sort=${encodeURIComponent(sort)}${q?`&q=${encodeURIComponent(q)}`:''}`);
    if(scanId!==state.scanId)return;
    state.catalog=data;
    if($('catalogTitleCount'))$('catalogTitleCount').textContent=Number(data.catalog_total||0).toLocaleString();
    renderCatalogTeaser(data.products||[],data.catalog_total||data.total||0,data.catalog||state.scan.catalog_summary?.catalog||{});
    renderProducts(data.products||[],(state.scan.catalog_summary?.common_gaps||[]).map(x=>x.field));
    renderCatalogPagination(data);
    if(scrollToProducts)$('productGrid').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
  }catch(e){ $('catalogCount').textContent=`Catalog unavailable: ${e.message}`; }
}

function scheduleCatalogSearch(){
  clearTimeout(state.catalogSearchTimer);
  state.catalogSearchTimer=setTimeout(()=>loadCatalogPage(1),250);
}

function bindEvidence(){
  document.querySelectorAll('[data-evidence]').forEach(btn=>btn.onclick=()=>{ const f=(state.scan?.findings||[]).find(x=>x.id===btn.dataset.evidence); if(f){track('issue_expanded',{issue_id:f.id,issue_category:f.category,issue_severity:f.severity});openEvidence(f.title,f.summary,f.status==='pass'?'No exposure observed.':f.summary,f.recommendation?.how||'',f.evidence||[],!['pass','unsupported'].includes(f.status));} });
  document.querySelectorAll('[data-check-evidence]').forEach(btn=>btn.onclick=()=>{ const c=effectiveChecks(state.scan).find(x=>x.id===btn.dataset.checkEvidence); if(c){track('issue_expanded',{issue_id:c.id,issue_category:c.category,issue_status:c.status});openEvidence(c.title,c.result,c.exposure,c.recommendation||'',c.evidence,!['pass','unsupported'].includes(c.status));} });
}
function openEvidence(title,summary,exposure,fix,data,actionable=false){
  const hasFix=Boolean(String(fix||'').trim()), hasRaw=hasEvidenceData(data), showAction=Boolean(actionable||hasFix);
  $('evidenceTitle').textContent=title;
  $('evidenceSummary').textContent=summary||'';
  $('evidenceExposure').innerHTML=`<strong>Impact on AI shopping</strong><span>${esc(exposure||'No merchant impact was recorded.')}</span>`;
  $('evidenceFix').classList.toggle('hidden',!hasFix);
  $('evidenceFix').innerHTML=hasFix?`<strong>Auteric action</strong><span>${esc(fix)}</span>`:'';
  $('evidenceAction').classList.toggle('hidden',!showAction);
  $('evidenceRaw').classList.toggle('hidden',!hasRaw);
  $('evidenceRaw').open=false;
  $('evidenceData').textContent=hasRaw?pretty(data):'';
  $('evidenceModal').showModal();
}

function setCatalogExpanded(expanded,scroll=false){
  const section=$('catalogSection'), button=$('catalogExpandBtn'), grid=$('productGrid'), total=Number(state.catalog.total||state.scan?.products_scanned||state.scan?.catalog_summary?.products||0);
  const next=Boolean(expanded), cards=[...grid.querySelectorAll('.product-card')], flipTargets=[button,...cards.slice(0,6)];
  const flipState=motion.ready&&!motion.reduce&&window.Flip&&flipTargets.length?window.Flip.getState(flipTargets,{props:'borderRadius'}):null;
  state.catalogExpanded=next;
  section.classList.toggle('catalog-expanded',state.catalogExpanded);
  button.setAttribute('aria-expanded',String(state.catalogExpanded));
  button.innerHTML=state.catalogExpanded?'<span>Back to preview</span><small>Return to the compact report</small>':`<span>Browse ${total.toLocaleString()} products</span><small>Load and inspect 50 products per page</small>`;
  grid.setAttribute('aria-label',state.catalogExpanded?'Full product catalog':'Product catalog preview. Drag horizontally to browse products.');
  if(!state.catalogExpanded)grid.scrollLeft=0;
  syncCatalogDrag();
  if(flipState){
    window.Flip.from(flipState,{duration:.62,scale:true,ease:motion.ease,absolute:false,stagger:.025,onComplete:()=>window.ScrollTrigger?.refresh?.()});
    if(next&&cards.length>6)window.gsap.fromTo(cards.slice(6),{autoAlpha:0,y:18},{autoAlpha:1,y:0,duration:.36,stagger:.025,ease:motion.ease,clearProps:'opacity,visibility,transform'});
  }
  if(scroll)setTimeout(()=>section.scrollIntoView({behavior:motion.reduce?'auto':'smooth',block:'start'}),flipState?180:0);
}

function syncCatalogDrag(){
  if(!state.catalogDraggable)return;
  if(state.catalogExpanded)state.catalogDraggable.disable(); else state.catalogDraggable.enable();
}

function setupNativeCatalogDrag(){
  const rail=$('productGrid'); let active=false, moved=false, suppressClick=false, pointerId=null, startX=0, startScroll=0;
  rail.addEventListener('pointerdown',event=>{
    if(state.catalogExpanded||event.pointerType!=='mouse'||event.button!==0||event.target.closest('button,a'))return;
    active=true; moved=false; pointerId=event.pointerId; startX=event.clientX; startScroll=rail.scrollLeft; rail.setPointerCapture(pointerId); rail.classList.add('is-grabbing');
  });
  rail.addEventListener('pointermove',event=>{
    if(!active||event.pointerId!==pointerId)return; const delta=event.clientX-startX; if(Math.abs(delta)>5)moved=true; if(moved){event.preventDefault();rail.scrollLeft=startScroll-delta;}
  });
  const finish=event=>{ if(!active||event.pointerId!==pointerId)return; active=false; rail.classList.remove('is-grabbing'); if(rail.hasPointerCapture(pointerId))rail.releasePointerCapture(pointerId); pointerId=null; if(moved){suppressClick=true;setTimeout(()=>{suppressClick=false;},0);} };
  rail.addEventListener('pointerup',finish); rail.addEventListener('pointercancel',finish);
  rail.addEventListener('click',event=>{if(suppressClick){event.preventDefault();event.stopPropagation();}},true);
}

function setupCatalogDrag(){
  const rail=$('productGrid');
  if(!motion.ready||motion.reduce||!window.Draggable){ setupNativeCatalogDrag(); return; }
  try{
    const proxy=document.createElement('div');
    proxy.className='catalog-drag-proxy'; document.body.appendChild(proxy);
    window.gsap.set(proxy,{x:-rail.scrollLeft});
    const syncScroll=function(){ rail.scrollLeft=Math.max(0,-this.x); };
    const instances=window.Draggable.create(proxy,{
      trigger:rail, type:'x', inertia:Boolean(window.InertiaPlugin), edgeResistance:.88,
      allowNativeTouchScrolling:true, activeCursor:'grabbing', minimumMovement:5, dragResistance:.04,
      onPress(){
        window.gsap.killTweensOf(proxy); window.gsap.set(proxy,{x:-rail.scrollLeft});
        this.update(); this.applyBounds({minX:Math.min(0,rail.clientWidth-rail.scrollWidth),maxX:0});
        rail.classList.add('is-grabbing'); window.gsap.to(rail.querySelectorAll('.product-card'),{scale:.988,duration:.16,overwrite:true});
      },
      onDrag:syncScroll, onThrowUpdate:syncScroll,
      onRelease(){ rail.classList.remove('is-grabbing'); window.gsap.to(rail.querySelectorAll('.product-card'),{scale:1,duration:.3,ease:motion.ease,overwrite:true,clearProps:'transform'}); }
    });
    state.catalogDraggable=instances[0]||null; rail.classList.add('gsap-drag-ready'); syncCatalogDrag();
  }catch(error){ console.warn('Auteric catalog momentum unavailable; using native drag.',error); setupNativeCatalogDrag(); }
}

function openReportView(view){
  if(view==='catalog'){
    setView('catalog');
    return;
  }
  const inTechnical=document.getElementById('technical-report')?.contains($('dashboardView'));
  if(state.scan&&!inTechnical){
    const domain=host(state.scan.target_url);
    window.open(`/store/${encodeURIComponent(domain)}#${encodeURIComponent(view)}`,'_blank','noopener');
    return;
  }
  setView(view);
}
function scrollToReportSection(view,behavior='smooth'){
  if(view==='overview'){
    window.scrollTo({top:0,behavior});
    return;
  }
  const target=$(view==='catalog'?'catalogSection':view==='overview'?'overviewSection':'detailView');
  if(!target)return;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    target.scrollIntoView({behavior,block:'start'});
    if(view==='catalog')target.focus({preventScroll:true});
    else if(view!=='overview')$('detailBack').focus({preventScroll:true});
  }));
}
function setView(view,updateUrl=true,behavior='smooth'){
  const allowed=new Set(['overview','tests','attacks','findings','ucp','web','catalog','history']); if(!allowed.has(view))view='overview';
  const isDetail=!['overview','catalog'].includes(view);
  $('overviewSection').classList.toggle('hidden',isDetail);
  $('detailView').classList.toggle('hidden',!isDetail);
  document.querySelectorAll('[data-panel]').forEach(p=>p.classList.toggle('active',isDetail&&p.dataset.panel===view));
  if(isDetail){
    const labels={tests:'AI Shopping Readiness',attacks:'Checkout & Runtime',findings:'Security',ucp:'Protocols & Agents',web:'Web & Transport',history:'Scan History'};
    $('detailContext').textContent=labels[view]||'Detailed evidence';
  }
  if(updateUrl&&location.pathname.startsWith('/store/')){ const next=new URL(location.href); next.hash=view==='overview'?'':view; history.replaceState({},'',next); }
  if(view==='history') loadHistory();
  if(view==='catalog'){
    setCatalogExpanded(true);
  }
  scrollToReportSection(view,behavior);
}
function applyFindingFilter(filter){ state.findingFilter=filter; document.querySelectorAll('#findingFilters button').forEach(b=>b.classList.toggle('active',b.dataset.filter===filter)); document.querySelectorAll('#allFindings .finding-card').forEach(c=>{ const ok=filter==='all'||c.dataset.severity===filter||c.dataset.status===filter||c.dataset.category===filter; c.style.display=ok?'':'none'; }); }
function applyTestFilter(filter){ state.testFilter=filter; document.querySelectorAll('#testFilters button').forEach(b=>b.classList.toggle('active',b.dataset.testFilter===filter)); renderTests(); }

async function loadHistory(){
  try{ const data=await api('/api/scans?limit=20'); $('historyList').innerHTML=(data.scans||[]).map(s=>{const sc=s.scores?.overall??s.scores?.security; return `<div class="history-item"><div><strong>${esc(host(s.target_url))}</strong><span>${esc(s.platform||s.adapter||'scan')} · ${esc(dateText(s.completed_at))}</span></div><div class="history-score">${sc!==undefined?`<b>${esc(sc)}</b><small>${esc(grade(sc))}</small>`:''}<button data-open-scan="${esc(s.scan_id)}">Open report</button></div></div>`}).join('')||'<p class="muted">No scans yet.</p>'; document.querySelectorAll('[data-open-scan]').forEach(b=>b.onclick=()=>openScan(b.dataset.openScan)); }catch(e){ $('historyList').innerHTML=`<p class="muted">${esc(e.message)}</p>`; }
}
async function openScan(id){ try{ const scan=await api(`/api/scans/${id}?include_products=false`); if(scan.status!=='completed')return showToast('That scan is not complete'); state.scanId=id; state.scan=scan; history.replaceState({},'',`/store/${encodeURIComponent(host(scan.target_url))}?id=${encodeURIComponent(id)}`); render(scan); showDashboard(); setView('overview'); track('report_viewed',{target_domain:host(scan.target_url),public_report:!!scan.security_details_locked}); }catch(e){showToast(e.message)} }

async function loadDirectory(){
  clearProgressMotion(); $('landingView').classList.add('hidden'); $('dashboardView').classList.add('hidden'); $('progressView').classList.add('hidden'); $('siteHeader').classList.remove('hidden'); $('directoryView').classList.remove('hidden');
  try{
    const data=await api('/api/directory?limit=25'), sites=data.sites||[];
    const avg=sites.length?Math.round(sites.reduce((a,x)=>a+(x.score||0),0)/sites.length):0, verified=sites.filter(x=>x.ucp_status==='verified').length, issues=sites.reduce((a,x)=>a+(x.critical_high||0),0);
    $('directoryStats').innerHTML=[metric('Sites shown',sites.length),metric('Average AI score',avg+'/100'),metric('UCP detected',verified),metric('Reports with protected details',issues)].join('');
    $('directoryRows').innerHTML=sites.map(x=>`<tr data-directory-scan="${esc(x.scan_id)}"><td>${esc(x.rank)}</td><td><div class="directory-store"><img src="${favicon(x.domain)}" alt="" width="30" height="30"><span><strong>${esc(x.domain)}</strong><small>${esc(x.ucp_status||'unknown')}</small></span></div></td><td><span class="dir-score grade-${esc(x.grade)}"><b>${esc(x.grade)}</b>${esc(x.score)}</span></td><td>${esc(scoreText(x.ucp_score))}</td><td>${esc(scoreText(x.web_security))}</td><td>${esc(scoreText(x.coverage))}%</td><td><span class="exposure-count ${x.critical_high?'risk':'clean'}">${x.critical_high?'Owner verification required':'Public summary'}</span></td><td>${esc(x.platform)}</td><td>${esc(dateText(x.completed_at))}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">No public scans yet.</td></tr>';
    document.querySelectorAll('[data-directory-scan]').forEach(r=>r.onclick=()=>openScan(r.dataset.directoryScan));
  }catch(e){ $('directoryRows').innerHTML=`<tr><td colspan="9" class="muted">${esc(e.message)}</td></tr>`; }
}

function openGateway(source='report'){
  const requestedSource=typeof source==='string'?source:'report';
  const platform=String(state.scan?.platform||(requestedSource==='shopify'?'shopify':'storefront')).toLowerCase();
  const isShopify=platform==='shopify';
  const domain=state.scan?.target_url?host(state.scan.target_url):'';
  if(domain&&isLoopbackHost(domain)){showToast('Auteric connection setup requires a public domain.');return;}
  if(domain){window.open(`/onboarding/${encodeURIComponent(domain)}`,'_blank','noopener');return;}
  const shopify=state.platforms.find(item=>item.id==='shopify')||{};
  const installUrl=shopify.status==='install_available'?safeUrl(shopify.install_url):'';
  $('gatewayPlatform').textContent=isShopify?'Shopify detected':'Merchant connection';
  $('gatewayEvidence').textContent='Enforcement not verified';
  $('gatewayTitle').textContent=isShopify?`Protect ${domain||'your Shopify store'} at checkout.`:'Connect your store to Auteric protection.';
  $('gatewayIntro').textContent=isShopify
    ? 'Install the Auteric Shopify App to move from public findings to merchant-authorized verification and checkout enforcement.'
    : 'Auteric connects to the merchant-controlled purchase path to verify private controls and apply the store policy before checkout completes.';
  $('gatewayBoundary').textContent='The public scan did not test these private controls. Auteric only marks them protected after connected verification.';
  const primary=$('gatewayPrimaryCta');
  primary.href=installUrl||(isShopify?'/shopify-ai-shopping#protection':'/directory');
  primary.textContent=installUrl?'Install Auteric Shopify App':isShopify?'View Shopify protection':'View protection options';
  if(installUrl){ primary.target='_blank'; primary.rel='noopener'; }
  else{ primary.removeAttribute('target'); primary.removeAttribute('rel'); }
  $('gatewayCtaNote').textContent=installUrl
    ? 'Shopify will ask the merchant to approve the app before protection is activated.'
    : 'The Shopify installation URL is not connected on this deployment yet.';
  $('gatewayModal').showModal();
}

function fitReportDomain(){
  const title=$('reportTitle');
  if(!title||!title.clientWidth)return;
  title.style.fontSize='';
  let size=parseFloat(getComputedStyle(title).fontSize);
  while(title.scrollWidth>title.clientWidth && size>11){size-=.5;title.style.fontSize=`${size}px`;}
}
window.addEventListener('resize',fitReportDomain);
document.getElementById('scanForm').addEventListener('submit',e=>{ e.preventDefault(); const input=$('storeUrl'), error=$('scanError'); try{ const value=normalizeStoreUrl(input.value); input.removeAttribute('aria-invalid'); error.textContent=''; createScan(value,'auto'); }catch(err){ input.setAttribute('aria-invalid','true'); error.textContent=err.message; input.focus(); } });
$('connectAutericBtn').onclick=()=>{const input=$('storeUrl'),error=$('scanError');try{const value=normalizeStoreUrl(input.value);const domain=host(value);input.removeAttribute('aria-invalid');error.textContent='';window.open(`/onboarding/${encodeURIComponent(domain)}`,'_blank','noopener');}catch(err){input.setAttribute('aria-invalid','true');error.textContent=err.message;input.focus();}};
$('shopifyConnectBtn').onclick=()=>{
  track('shopify_interest_clicked',{source:'landing'});
  openGateway('shopify');
};
$('catalogExpandBtn').onclick=()=>setCatalogExpanded(!state.catalogExpanded,true);
initMotion();
setupGaugeMotion();
setupCatalogDrag();
$('shareBtn').onclick=async()=>{ try{await navigator.clipboard.writeText(location.origin+location.pathname);track('report_shared',{target_domain:state.scan?host(state.scan.target_url):null});showToast('Report link copied')}catch{showToast('Could not copy link')} };
$('exportBtn').onclick=()=>{ if(!state.scan)return; const blob=new Blob([pretty(state.scan)],{type:'application/json'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=`auteric-ai-shopping-${host(state.scan.target_url)}.json`; a.click(); URL.revokeObjectURL(a.href); };
$('evidenceClose').onclick=()=>$('evidenceModal').close(); $('productClose').onclick=()=>$('productModal').close(); $('productSearch').addEventListener('input',scheduleCatalogSearch); $('catalogFilter').addEventListener('change',()=>loadCatalogPage(1)); $('catalogSort').addEventListener('change',()=>loadCatalogPage(1)); $('testSearch').addEventListener('input',renderTests);
document.querySelector('[data-evidence-auteric]').onclick=()=>{ track('verify_store_clicked',{target_domain:state.scan?host(state.scan.target_url):null,source:'finding'}); $('evidenceModal').close(); openGateway(); };
$('retryScanBtn').onclick=()=>state.lastTarget&&createScan(state.lastTarget,'auto'); $('editDomainBtn').onclick=()=>{showLanding();$('storeUrl').value=state.lastTarget||'';$('storeUrl').focus();};
$('detailBack').onclick=()=>setView('overview');
document.querySelectorAll('[data-gauge-view]').forEach(b=>b.onclick=()=>openReportView(b.dataset.gaugeView)); document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>openReportView(b.dataset.go)); document.querySelectorAll('#findingFilters button').forEach(b=>b.onclick=()=>applyFindingFilter(b.dataset.filter)); document.querySelectorAll('#testFilters button').forEach(b=>b.onclick=()=>applyTestFilter(b.dataset.testFilter));
document.querySelectorAll('[data-directory-link]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();location.assign('/directory');})); document.querySelectorAll('[data-open-directory]').forEach(b=>b.onclick=()=>{location.assign('/directory');}); document.querySelectorAll('[data-gateway-cta]').forEach(b=>b.onclick=()=>{track('verify_store_clicked',{target_domain:state.scan?host(state.scan.target_url):null});openGateway();}); document.querySelectorAll('[data-close-gateway]').forEach(b=>b.onclick=()=>$('gatewayModal').close()); document.querySelectorAll('[data-focus-scanner]').forEach(b=>b.addEventListener('click',e=>{e.preventDefault();$('scanForm').scrollIntoView({behavior:motion.reduce?'auto':'smooth',block:'center'});setTimeout(()=>$('storeUrl')?.focus({preventScroll:true}),250);}));
document.querySelectorAll('[data-playground-link]').forEach(a=>a.addEventListener('click',()=>{try{a.href=`/playground?domain=${encodeURIComponent(host(normalizeStoreUrl($('storeUrl').value)))}#workbench`;}catch{a.href='/playground#workbench';}}));
document.querySelectorAll('a[href="/shopify-ai-shopping"]').forEach(a=>a.addEventListener('click',()=>track('shopify_interest_clicked')));
document.querySelectorAll('a[href="/woocommerce-ai-shopping"]').forEach(a=>a.addEventListener('click',()=>track('woo_interest_clicked')));
window.addEventListener('popstate',()=>{ if(document.getElementById('publicReport'))return; if(location.pathname==='/directory')loadDirectory(); else if(new URLSearchParams(location.search).get('id')||new URLSearchParams(location.search).get('scan'))location.reload(); else showLanding(); });
window.addEventListener('hashchange',()=>{ if(document.getElementById('publicReport'))return; if(state.scan)setView(location.hash.slice(1)||'overview',false); });

(async function init(){
  document.querySelector('[data-public-copy]')?.addEventListener('click',async()=>{await navigator.clipboard.writeText(location.origin+location.pathname);track('report_shared');showToast('Report link copied');});
  document.querySelector('[data-public-print]')?.addEventListener('click',()=>window.print());
  await loadPlatformConfig();
  if(location.pathname==='/directory'){ await loadDirectory(); return; }
  const publicReport=document.getElementById('publicReport');
  if(publicReport){
    document.querySelectorAll('a[href="#technical-report"]').forEach(a=>a.addEventListener('click',()=>{document.getElementById('technical-report').open=true;}));
    const openInteractiveReport=async(trigger, openCatalog=false, view='overview')=>{
      trigger.disabled=true;
      try {
        const scan=await api(`/api/scans/${encodeURIComponent(trigger.dataset.technicalDashboard)}?include_products=false`);
        state.scan=scan; state.scanId=scan.scan_id; state.lastTarget=scan.target_url;
        document.getElementById('technical-report').appendChild($('dashboardView'));
        render(scan); $('dashboardView').classList.remove('hidden'); $('dashboardView').classList.add('result-ready');
        document.querySelectorAll('#dashboardView .report-reveal').forEach(el=>{el.style.opacity='1';el.style.transform='none';});
        trigger.hidden=true;
        if(openCatalog||view!=='overview'){
          setView(openCatalog?'catalog':view,false,'auto');
          setTimeout(()=>scrollToReportSection(openCatalog?'catalog':view,'auto'),250);
        }
      } catch(e){document.querySelector('[data-technical-status]').textContent='Interactive evidence is not retained on this server. The public technical summary remains available; rescan for fresh checks.';trigger.disabled=false;}
    };
    const technicalButton=document.querySelector('[data-technical-dashboard]');
    technicalButton?.addEventListener('click',()=>window.open(`${location.pathname}#tests`,'_blank','noopener'));
    document.querySelector('[data-open-catalog]')?.addEventListener('click',()=>{
      document.getElementById('technical-report').open=true;
      if(technicalButton&&!technicalButton.hidden)openInteractiveReport(technicalButton,true,'catalog');
      else setView('catalog',false);
    });
    const requestedView=location.hash.slice(1);
    if(technicalButton&&['catalog','tests','attacks','findings','ucp','web','history'].includes(requestedView)){
      document.getElementById('technical-report').open=true;
      openInteractiveReport(technicalButton,requestedView==='catalog',requestedView);
    }
    return;
  }
  const params=new URLSearchParams(location.search), scanId=params.get('id')||params.get('scan')||document.body.dataset.scanId||publicReport?.dataset.publicScan;
  if(scanId){ try{ const scan=await api(`/api/scans/${scanId}?include_products=false`); state.scanId=scanId; state.lastTarget=scan.target_url; if(scan.status==='completed'){state.scan=scan;render(scan);showDashboard();const initialView=location.hash.slice(1)||'overview';setView(initialView,false,'auto');if(initialView!=='overview')setTimeout(()=>{if(location.hash.slice(1)===initialView)scrollToReportSection(initialView,'auto');},300);track('report_viewed',{target_domain:host(scan.target_url),public_report:!!scan.security_details_locked});return;} if(['queued','running'].includes(scan.status)){showProgress(scan.target_url);await pollScan();return;} if(scan.status==='failed'){showProgress(scan.target_url);showScanError(scan.error||'The scan failed before a report could be created.');return;} }catch(e){showToast(e.message||'Report unavailable');} }
  if(publicReport)return;
  const requestedDomain=params.get('domain'); if(requestedDomain)$('storeUrl').value=requestedDomain;
  showLanding(); track('homepage_view',{referrer:document.referrer||null,utm_source:params.get('utm_source'),utm_medium:params.get('utm_medium'),utm_campaign:params.get('utm_campaign')});
})();
