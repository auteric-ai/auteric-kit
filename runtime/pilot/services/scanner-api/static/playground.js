const byId = id => document.getElementById(id);
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const steps = [5,28,52,70,90];
let activeScanId = null;

function escapeHtml(value=''){ return String(value).replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char])); }
function safeUrl(value){ try{ const url=new URL(value); return ['http:','https:'].includes(url.protocol)?url.href:''; }catch{return '';} }
function isLocalScanner(){ return ['localhost','127.0.0.1','::1'].includes(location.hostname) || location.hostname.endsWith('.localhost'); }
function isLoopbackHost(host){ return host==='localhost'||host.endsWith('.localhost')||host==='::1'||/^127(?:\.\d{1,3}){3}$/.test(host); }
function normalizeStoreUrl(value){
  const raw=String(value||'').trim();
  if(!raw)throw new Error('Enter a store domain.');
  let url;
  const localInput=/^(?:localhost|(?:[\w-]+\.)+localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(raw);
  try{ url=new URL(/^https?:\/\//i.test(raw)?raw:`${localInput?'http':'https'}://${raw}`); }catch{ throw new Error('Enter a valid store URL.'); }
  if(!['http:','https:'].includes(url.protocol)||!url.hostname||(!isLoopbackHost(url.hostname)&&!url.hostname.includes('.'))||(isLoopbackHost(url.hostname)&&!isLocalScanner()))throw new Error(isLocalScanner()?'Enter a valid store URL.':'Enter a valid public store domain.');
  url.hash=''; return url.href;
}
async function requestJson(url,options={}){
  const response=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.detail||data.error||`Request failed (${response.status})`);
  return data;
}
function setStatus(label,className=''){
  const status=byId('sessionStatus'); status.textContent=label; status.className=`session-status ${className}`.trim();
}
function updateLog(progress,message=''){
  const rows=[...document.querySelectorAll('.playground-log-row')];
  let active=0; steps.forEach((value,index)=>{ if(progress>=value)active=index; });
  rows.forEach((row,index)=>{
    const complete=progress>=100||index<active;
    row.classList.toggle('complete',complete); row.classList.toggle('active',!complete&&index===active);
    row.querySelector('em').textContent=complete?'DONE':index===active?'RUNNING':'WAITING';
  });
  const current=rows[Math.min(active,rows.length-1)]?.querySelector('small');
  if(current&&message)current.textContent=message;
}
function animateSession(){
  if(reduceMotion||!window.gsap)return;
  window.gsap.fromTo('.playground-log-row',{autoAlpha:.34,x:-10},{autoAlpha:1,x:0,duration:.38,stagger:.055,ease:'power3.out',overwrite:'auto'});
}
function productCard(product){
  const image=safeUrl((product.images||[])[0]);
  let price='Price not verified';
  if(product.price!==null&&product.price!==undefined){
    try{ price=product.currency?new Intl.NumberFormat(undefined,{style:'currency',currency:product.currency,maximumFractionDigits:2}).format(product.price):String(product.price); }catch{ price=String(product.price); }
  }
  return `<article class="playground-product"><div class="playground-product-media">${image?`<img src="${escapeHtml(image)}" alt="${escapeHtml(product.title)}" loading="lazy" referrerpolicy="no-referrer" />`:'<span>NO IMAGE VERIFIED</span>'}</div><div><strong dir="auto">${escapeHtml(product.title)}</strong><small>${escapeHtml(price)}</small></div></article>`;
}
async function finishSession(scan){
  const observation=scan.observations||{}, ucp=observation.ucp_analysis||{}, bots=observation.ai_bot_access||{};
  const catalog=scan.catalog_summary?.catalog||observation.catalog||{};
  const catalogPage=await requestJson(`/api/scans/${encodeURIComponent(activeScanId)}/catalog?page=1&limit=8`);
  if(scan.scan_id!==activeScanId)return;
  const products=catalogPage.products||[], botValues=Object.values(bots), allowed=botValues.filter(Boolean).length;
  byId('playgroundUcp').textContent=observation.ucp_status==='verified'?'Verified':observation.ucp_status==='invalid'?'Invalid':'Not detected';
  byId('playgroundUcpMeta').textContent=ucp.version?`Version ${ucp.version}`:'No public UCP version verified.';
  byId('playgroundTransports').textContent=(ucp.transports||[]).map(item=>String(item).toUpperCase()).join(' · ')||'None verified';
  const catalogProbe=ucp.catalog_probe||{};
  byId('playgroundTransportsMeta').textContent=catalogProbe.attempted
    ? (catalogProbe.ok?`Live ${String(catalogProbe.transport||'UCP').toUpperCase()} catalog probe verified.`:'A live catalog probe ran without a usable result.')
    : (catalogProbe.reason?`Live probe not run: ${catalogProbe.reason}.`:'Declared agent connection routes.');
  byId('playgroundCatalogCount').textContent=Number(catalogPage.catalog_total||0).toLocaleString();
  byId('playgroundCatalogMeta').textContent=catalog.complete===true?'Complete public catalog':'Public evidence sample';
  byId('playgroundAgentAccess').textContent=botValues.length?`${allowed}/${botValues.length} allowed`:'Not verified';
  byId('playgroundCatalogTitle').textContent=products.length?'Products visible in this session.':'No usable product records returned.';
  byId('playgroundProducts').innerHTML=products.length?products.map(productCard).join(''):'<div class="empty-state"><strong>No products verified.</strong><p>The report still contains protocol and discovery evidence.</p></div>';
  const report=byId('openReportLink'); report.href=`/store/${encodeURIComponent(new URL(scan.target_url).hostname)}?id=${encodeURIComponent(activeScanId)}#catalog`; report.classList.remove('hidden');
  setStatus('READY'); updateLog(100,'Evidence-backed agent view is ready.');
  if(!reduceMotion&&window.gsap)window.gsap.fromTo('.playground-metric,.playground-product',{autoAlpha:0,y:20},{autoAlpha:1,y:0,duration:.45,stagger:.045,ease:'power3.out',clearProps:'opacity,visibility,transform'});
}
async function pollSession(scanId){
  for(let attempt=0;attempt<240;attempt++){
    const scan=await requestJson(`/api/scans/${encodeURIComponent(scanId)}?include_products=false`);
    if(scanId!==activeScanId)return;
    updateLog(Number(scan.progress||0),scan.progress_message||'Inspecting public evidence');
    if(scan.status==='completed'){ await finishSession(scan); return; }
    if(scan.status==='failed')throw new Error(scan.error||'The scan failed.');
    await new Promise(resolve=>setTimeout(resolve,650));
  }
  throw new Error('The session timed out before the report was ready.');
}
async function connectStore(target){
  byId('sessionDomain').textContent=new URL(target).hostname;
  byId('playgroundProducts').innerHTML='<div class="empty-state"><strong>Connecting to storefront…</strong><p>Product evidence will appear when the read-only scan completes.</p></div>';
  byId('openReportLink').classList.add('hidden'); setStatus('CONNECTING','running'); updateLog(5,'Resolving and opening the public storefront.'); animateSession();
  const created=await requestJson('/api/scans',{method:'POST',body:JSON.stringify({target_url:target,adapter:'auto'})});
  activeScanId=created.scan_id; await pollSession(created.scan_id);
}

document.getElementById('playgroundForm').addEventListener('submit',async event=>{
  event.preventDefault(); const input=byId('playgroundUrl'), error=byId('playgroundError'), button=byId('playgroundSubmit');
  try{
    const target=normalizeStoreUrl(input.value); error.textContent=''; input.removeAttribute('aria-invalid'); button.disabled=true; button.textContent='Connecting…';
    await connectStore(target);
  }catch(problem){
    error.textContent=problem.message||'Could not connect to this store.'; input.setAttribute('aria-invalid','true'); setStatus('FAILED','failed');
  }finally{ button.disabled=false; button.textContent='Connect & inspect'; }
});

const prefilledDomain=new URLSearchParams(location.search).get('domain');
if(prefilledDomain){
  try{ byId('playgroundUrl').value=new URL(normalizeStoreUrl(prefilledDomain)).hostname; }
  catch{ /* Keep the form empty when the incoming domain is invalid. */ }
}

if(window.gsap&&!reduceMotion){
  if(window.CustomEase){ window.gsap.registerPlugin(window.CustomEase); window.CustomEase.create('playgroundEase','M0,0 C0.16,1 0.3,1 1,1'); }
  window.gsap.timeline({defaults:{ease:'playgroundEase'}})
    .fromTo('.playground-title .eyebrow',{autoAlpha:0,y:12},{autoAlpha:1,y:0,duration:.38},0)
    .fromTo('.playground-title h1',{autoAlpha:0,y:34,rotation:.5},{autoAlpha:1,y:0,rotation:0,duration:.75},.05)
    .fromTo('.playground-title p,.playground-note',{autoAlpha:0,y:20},{autoAlpha:1,y:0,duration:.5,stagger:.08},.25)
    .fromTo('.playground-workbench',{autoAlpha:0,y:24,scale:.99},{autoAlpha:1,y:0,scale:1,duration:.65},.42);
}
