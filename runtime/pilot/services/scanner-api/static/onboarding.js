(() => {
  const root = document.querySelector('.merchant-onboarding');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const kit = JSON.parse($('onboardingKitConfig').textContent);
  const tabs = [...document.querySelectorAll('[data-agent]')];
  let selected = 'codex';
  function selectAgent(name) {
    const agent = kit.agents[name];
    if (!agent) return;
    selected = name;
    tabs.forEach(tab => { const active = tab.dataset.agent === name; tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; });
    $('agentPanel').setAttribute('aria-labelledby', 'agent-' + name);
    $('skillCommand').textContent = agent.command;
    $('connectCommand').textContent = agent.invocation;
    // Scanner deliberately copies only the native trigger; the installed Kit
    // remains the versioned source of detailed implementation instructions.
    $('copySkillPrompt').lastChild.textContent = 'Copy Auteric Connect command';
    $('openAgent').hidden = !agent.launch_url;
    if (agent.launch_url) $('openAgent').href = agent.launch_url;
    $('openAgent').textContent = 'Open ' + agent.label + ' with instruction';
    $('copySkillStatus').textContent = '';
  }
  tabs.forEach(tab => tab.addEventListener('click', () => selectAgent(tab.dataset.agent)));
  document.querySelector('.onboarding-agent-tabs')?.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const index = tabs.findIndex(tab => tab.dataset.agent === selected);
    const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    selectAgent(next.dataset.agent); next.focus();
  });
  async function copy(text, label) {
    try { await navigator.clipboard.writeText(text); $('copySkillStatus').textContent = label + ' copied. Open your store project before using it.'; }
    catch (_) { $('copySkillStatus').textContent = 'Clipboard access was blocked. Open your coding agent with the instruction instead.'; }
  }
  $('copySkillPrompt').addEventListener('click', () => copy(kit.agents[selected].prompt, 'Auteric Connect command'));
  $('copySkillCommand').addEventListener('click', () => copy(kit.agents[selected].command, 'Command'));
  document.querySelectorAll('[data-next-step]').forEach(button => button.addEventListener('click', () => {
    document.getElementById(button.dataset.nextStep)?.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'});
  }));
  selectAgent(selected);
  const connectionChoices = [...document.querySelectorAll('[data-connection-kind]')];
  const connectionPanels = [...document.querySelectorAll('[data-connection-panel]')];
  function selectConnection(kind) {
    connectionChoices.forEach(choice => {
      choice.setAttribute('aria-pressed', String(choice.dataset.connectionKind === kind));
    });
    connectionPanels.forEach(panel => { panel.hidden = panel.dataset.connectionPanel !== kind; });
  }
  connectionChoices.forEach(choice => choice.addEventListener('click', () => selectConnection(choice.dataset.connectionKind)));
  const builderTabs = [...document.querySelectorAll('[data-builder]')];
  function selectBuilder(name) {
    const builder = kit.builders?.[name];
    if (!builder) return;
    builderTabs.forEach(tab => tab.setAttribute('aria-selected', String(tab.dataset.builder === name)));
    $('builderTitle').textContent = builder.title;
    $('builderDescription').textContent = builder.description;
    $('builderSteps').replaceChildren(...builder.steps.map(step => {
      const item = document.createElement('li'); item.textContent = step; return item;
    }));
  }
  builderTabs.forEach(tab => tab.addEventListener('click', () => selectBuilder(tab.dataset.builder)));
  $('checkBuilderStore')?.addEventListener('click', () => {
    document.getElementById('verifyStep')?.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'});
    $('checkConnection')?.click();
  });
  const stars = $('onboardingGithubStars');
  if (stars) {
    fetch('https://api.github.com/repos/auteric-ai/auteric-kit', {headers: {Accept: 'application/vnd.github+json'}})
      .then(response => response.ok ? response.json() : null)
      .then(repo => {
        const count = Number(repo?.stargazers_count);
        if (Number.isFinite(count) && count > 20) {
          stars.textContent = `★ ${count.toLocaleString()}`;
          stars.hidden = false;
        }
      }).catch(() => {});
  }

  $('connectionHelpForm').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('[type="submit"]');
    const data = new FormData(form);
    const email = String(data.get('email') || '').trim();
    const phone = String(data.get('phone') || '').trim();
    const domain = root.dataset.domain;
    const fallback = $('connectionHelpEmail');
    fallback.href = `mailto:hello@auteric.com?subject=${encodeURIComponent('Connect my store: ' + domain)}&body=${encodeURIComponent(`Store: ${domain}\nEmail: ${email}\nPhone: ${phone}`)}`;
    fallback.hidden = true;
    button.disabled = true;
    $('connectionHelpStatus').textContent = 'Sending your request…';
    try {
      const response = await fetch('/api/onboarding/contact', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({domain, email, phone, website: String(data.get('website') || '')})
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not send your request.');
      $('connectionHelpStatus').textContent = 'Request sent to hello@auteric.com. We’ll be in touch.';
      form.reset();
    } catch (error) {
      $('connectionHelpStatus').textContent = `${error.message || 'Could not send your request.'} Open your email app and press Send.`;
      fallback.hidden = false;
    } finally { button.disabled = false; }
  });

  const rows = [...document.querySelectorAll('#playgroundLog .playground-log-row')];
  function step(index, state, detail) {
    const row = rows[index]; row.className = 'playground-log-row ' + state;
    row.querySelector('small').textContent = detail;
    row.querySelector('em').textContent = state === 'complete' ? 'VERIFIED' : state === 'next' ? 'SAFE CHECK' : state === 'failed' ? 'REVIEW' : 'CHECKING';
  }
  async function request(url, options) {
    const response = await fetch(url, options); const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || 'Connection check failed.'); return body;
  }
  $('checkConnection').addEventListener('click', async () => {
    const button = $('checkConnection'); button.disabled = true;
    $('sessionStatus').textContent = 'CHECKING'; $('connectionCheckStatus').textContent = 'Reading the published storefront…';
    rows.forEach((_, i) => step(i, 'pending', 'Waiting for a fresh public scan.'));
    try {
      const domain = root.dataset.domain;
      const created = await request('/api/scans', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({target_url:'https://' + domain, adapter:'generic'})});
      let scan;
      for (let attempt = 0; attempt < 240; attempt++) {
        scan = await request('/api/scans/' + encodeURIComponent(created.scan_id) + '?include_products=false');
        if (scan.status === 'completed' || scan.status === 'failed') break;
        $('connectionCheckStatus').textContent = scan.progress_message || 'Checking live site…';
        await new Promise(resolve => setTimeout(resolve, 650));
      }
      if (scan?.status !== 'completed') throw new Error(scan?.error || 'The check did not complete.');
      const v = await request(`/api/onboarding/${encodeURIComponent(domain)}/verification/${encodeURIComponent(created.scan_id)}`);
      const checks = [
        [v.ucp_status === 'verified', v.ucp_status === 'verified' ? 'UCP discovery verified.' : 'UCP discovery was not verified.'],
        [v.transport_verified, v.transport_verified ? 'Declared transport responded.' : 'Declared transport was not verified.'],
        [v.catalog_observed > 0, v.catalog_blocked ? 'Bot protection blocked this public scan.' : `${v.catalog_observed} product records observed.`],
        [v.attestation_status === 'verified', v.attestation_status === 'verified' ? 'Auteric signed exposure verified.' : 'Trusted Auteric exposure signature was not verified.'],
        [v.runtime_protected, v.runtime_protected ? 'Protected cart actions verified.' : v.runtime_status === 'safe_test_required' ? 'Ready for one safe cart check. No payment or order will be created.' : 'Connect the merchant runtime to verify protected actions.'],
      ];
      checks.forEach(([ok, detail], i) => step(i, ok ? 'complete' : i === 4 && v.runtime_status === 'safe_test_required' ? 'next' : 'failed', detail));
      const nextAction = $('runtimeNextAction'); const action = $('runtimeAction');
      const canRunSafeCheck = !v.runtime_protected && v.runtime_status === 'safe_test_required' && Boolean(v.runtime_action_url);
      nextAction.hidden = !canRunSafeCheck;
      if (canRunSafeCheck) {
        action.href = v.runtime_action_url;
        action.textContent = v.runtime_action_label || 'Run safe cart check';
        $('runtimeActionDetail').textContent = v.runtime_action_detail || 'No payment, order or customer cart is used.';
      }
      $('sessionStatus').textContent = v.runtime_protected ? 'PROTECTED' : v.runtime_status === 'safe_test_required' ? 'SAFE CHECK READY' : v.attestation_status === 'verified' ? 'CONNECTED' : 'SETUP NEEDED';
      $('connectionCheckStatus').textContent = v.runtime_protected ? 'Current runtime evidence confirms Auteric protection.' : canRunSafeCheck ? 'Your public connection is ready. Run the safe cart check once; protection activates automatically when it passes.' : 'The check is complete. Follow the next setup step shown above.';
    } catch (error) { $('sessionStatus').textContent = 'CHECK FAILED'; $('connectionCheckStatus').textContent = error.message || 'Could not complete the check.'; }
    finally { button.disabled = false; }
  });
})();
