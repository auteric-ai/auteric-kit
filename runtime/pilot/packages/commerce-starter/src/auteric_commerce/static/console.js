/* No merchant/model content is inserted as HTML. Credentials never enter storage. */
'use strict';
const $ = (id) => document.getElementById(id);
let token = '', localDemo = false;
function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
async function request(path, method = 'GET', body) {
  const headers = {'X-Auteric-Console':'1'};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const result = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials:'same-origin', redirect:'error'});
  if (result.status === 204) return null;
  const data = await result.json();
  if (!result.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request refused (${result.status}). Review the input and action state.`);
  return data;
}
const money = (value, currency = 'USD') => value == null ? 'Unknown' : new Intl.NumberFormat('en-US', {style:'currency', currency, maximumFractionDigits:2}).format(Number(value));
function metric(label, value) { const box = el('div'); box.append(el('span', label, 'detail-label'), el('strong', value)); return box; }
function actionButton(label, cls, fn) {
  const button = el('button', label, cls); button.type = 'button';
  button.addEventListener('click', async () => {button.disabled = true; try {await fn(); await refresh();} catch (error) {message(error.message, true);} finally {button.disabled = false;}}); return button;
}
function card(row) {
  const box = el('article', undefined, 'card'), action = row.action, decision = row.decision, context = decision.context;
  const head = el('div', undefined, 'card-head');
  head.append(el('span', action.type, 'eyebrow'), el('span', row.state.replaceAll('_',' ').toUpperCase(), `pill ${row.state}`));
  box.append(head, el('h3', context.items?.[0]?.title || context.items?.[0]?.sku || action.items?.[0]?.resource_id || 'Commerce action'));
  box.append(el('div', `Principal: ${action.principal?.subject || 'Unknown'} · Agent: ${action.agent?.id || 'Unknown'} · ${action.items?.length || 0} resource(s)`, 'identity'));
  const first = context.items?.[0];
  if (first) {
    const impact = el('div', undefined, 'impact');
    impact.append(metric('Current → proposed', `${money(first.old_price,first.currency)} → ${money(first.new_price,first.currency)}`),
      metric('Price movement', first.percentage_change == null ? 'Unknown' : `${Number(first.percentage_change).toFixed(1)}%`),
      metric('Inventory units', first.inventory_quantity == null ? 'Unknown' : Number(first.inventory_quantity).toLocaleString()),
      metric('Estimated value impact', money(first.estimated_revenue_exposure,first.currency)));
    box.append(impact);
  }
  box.append(el('p', context.estimate_notice || 'Estimated inventory-value sensitivity, not a revenue forecast.', 'muted'));
  const reasons = el('ul', undefined, 'reasons'); for (const reason of decision.reasons) reasons.append(el('li', reason)); box.append(reasons);
  box.append(el('p', `Rules: ${decision.matched_rules.join(', ')} · Decision: ${decision.outcome}`, 'muted'));
  box.append(el('p', `Expires: ${new Date(row.expires_at * 1000).toLocaleString()}${row.approved_by ? ` · Approved by ${row.approved_by}` : ''}`, 'muted'));
  const buttons = el('div', undefined, 'buttons');
  const path = `/v1/actions/${encodeURIComponent(row.id)}`;
  if (row.state === 'waiting_for_approval') {
    buttons.append(actionButton('Reject', 'reject', async () => {
      await request(path + '/reject', 'POST'); message('Rejected. No merchant mutation was made.');
    }), actionButton('Approve exact change', '', async () => {
      await request(path + '/approve', 'POST', {digest:row.digest}); message('Exact action approved. Approval alone does not execute it.');
    }));
  }
  if (localDemo && ['allowed','approved'].includes(row.state)) buttons.append(actionButton('Execute in demo', 'secondary', async () => {
    await request(`/dev/actions/${encodeURIComponent(row.id)}/execute`, 'POST'); message('Synthetic price updated and verified. Repeating execution returns the stored receipt.');
  }));
  if (buttons.children.length) box.append(buttons);
  const details = el('details'); details.append(el('summary', 'Exact action, fingerprint and decision'), el('pre', JSON.stringify(row,null,2))); box.append(details);
  const audit = el('details'); const summary = el('summary', 'Audit events'); audit.append(summary);
  audit.addEventListener('toggle', async () => {
    if (!audit.open || audit.children.length > 1) return;
    try {audit.append(el('pre', JSON.stringify(await request(`/v1/runtime/audit?action_id=${encodeURIComponent(row.id)}`),null,2)));}
    catch(error) {audit.append(el('p', error.message));}
  }); box.append(audit); return box;
}
async function refresh() {
  const rows = await request('/v1/actions');
  const pending = rows.filter(row => row.state === 'waiting_for_approval');
  $('pending').replaceChildren(...pending.map(card));
  $('activity').replaceChildren(...rows.filter(row => row.state !== 'waiting_for_approval').map(card));
  if (!pending.length) $('pending').append(el('p','No actions waiting for approval.','empty'));
  if (!$('activity').children.length) $('activity').append(el('p','No other activity yet. Evaluate a proposal to see its decision.','empty'));
  $('pending-count').textContent = String(pending.length);
}
async function connected() {
  const policy = await request('/v1/runtime/policy'); $('merchant').textContent = policy.merchant_id;
  await refresh(); $('access').hidden = true; $('workspace').hidden = false; $('demo-panel').hidden = !localDemo;
  message(localDemo ? 'Local demonstration: simulated identities share this machine. No production authentication is implied.' : 'Connected with a separate operator credential.');
}
$('login').addEventListener('submit', async (event) => {event.preventDefault(); token = $('token').value.trim(); $('token').value = ''; try {await connected();} catch(error) {token=''; message(error.message,true);}});
$('demo-login').addEventListener('click', async () => {try {await request('/dev/session','POST'); await connected();} catch(error) {message(error.message,true);}});
$('refresh').addEventListener('click', async () => {try {await refresh(); message('Activity refreshed.');} catch(error) {message(error.message,true);}});
$('logout').addEventListener('click', async () => {token=''; try {await request('/dev/session','DELETE');} catch {} $('workspace').hidden=true; $('access').hidden=false; $('pending').replaceChildren(); $('activity').replaceChildren(); message('Disconnected.');});
$('scenario').addEventListener('submit', async (event) => {event.preventDefault(); const button=event.submitter; button.disabled=true; try {const row=await request('/dev/scenarios','POST',{new_price:$('price').value}); await refresh(); message(`${row.decision.outcome}: ${row.decision.reasons.join(' ')}`);} catch(error) {message(error.message,true);} finally {button.disabled=false;}});
request('/console/config').then(config => {localDemo=config.local_demo; $('environment').textContent=localDemo ? 'LOCAL DEMO' : config.mode.toUpperCase(); $('demo-login').hidden=!localDemo; $('login').hidden=localDemo; if (localDemo) $('access-note').textContent='No account or API key needed. This loopback-only session acts as the synthetic merchant reviewer.';}).catch(error=>message(error.message,true));
