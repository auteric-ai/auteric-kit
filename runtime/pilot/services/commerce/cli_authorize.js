(() => {
  const root = document.body;
  const requestId = root.dataset.request;
  const registrationEnabled = root.dataset.registrationEnabled === 'true';
  const localDevelopment = root.dataset.localDevelopment === 'true';
  const form = document.getElementById('account-form');
  const message = document.getElementById('message');
  const createTab = document.getElementById('tab-create');
  const loginTab = document.getElementById('tab-login');
  const orgRow = document.getElementById('organization-row');
  const submit = document.getElementById('account-submit');
  const note = document.getElementById('account-note');
  const signedIn = document.getElementById('signed-in');
  const signedInEmail = document.getElementById('signed-in-email');
  const googleOption = document.getElementById('google-option');
  const code = document.getElementById('pair-code');
  const approve = document.getElementById('approve');
  const dashboardLink = document.getElementById('dashboard-link');
  let mode = registrationEnabled ? 'create' : 'login';
  let account = null;
  let pendingGoogleCredential = null;
  let watching = false;

  function status(value, error = false) {
    message.textContent = value;
    message.classList.toggle('error', error);
  }
  function setMode(next) {
    mode = next;
    createTab.setAttribute('aria-selected', String(next === 'create'));
    loginTab.setAttribute('aria-selected', String(next === 'login'));
    orgRow.hidden = next !== 'create';
    orgRow.querySelector('input').required = next === 'create';
    form.elements.password.autocomplete = next === 'create' ? 'new-password' : 'current-password';
    form.elements.password.placeholder = next === 'create' ? 'Create a password' : 'Your Auteric password';
    submit.firstChild.textContent = next === 'create' ? 'Create account ' : 'Sign in ';
    note.textContent = next === 'create'
      ? (localDevelopment ? 'Local test account: email is not verified.' : 'Account access is separate from proof of store ownership.')
      : 'Use your Auteric account password here. The terminal code belongs in the box below.';
    status('');
  }
  async function post(path, body) {
    const response = await fetch(path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Auteric-Console': '1' },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = typeof data.detail === 'string' ? data.detail : 'This step could not be completed.';
      if (response.status === 422 && path.endsWith('/auth/login')) detail = 'Enter your account password (at least 12 characters), not the code from your terminal.';
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function showAccount(data) {
    account = data;
    signedInEmail.textContent = data.email || 'Auteric account';
    signedIn.hidden = false;
    form.hidden = true;
    document.querySelector('.tabs').hidden = true;
    googleOption.hidden = true;
    approve.disabled = false;
    code.focus();
    status('Account ready. Enter the code from your terminal below.');
    fetch(`/api/commerce/cli/status?request=${encodeURIComponent(requestId)}`, { credentials: 'same-origin' })
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        if (result?.approved && !watching) {
          approve.disabled = true;
          code.disabled = true;
          status('Setup approved. Preparing your store and dashboard…');
          void watchCompletion();
        }
      })
      .catch(() => {});
  }
  async function watchCompletion() {
    if (watching) return;
    watching = true;
    dashboardLink.hidden = false;
    for (let attempt = 0; attempt < 120; attempt++) {
      try {
        const response = await fetch(`/api/commerce/cli/status?request=${encodeURIComponent(requestId)}`, { credentials: 'same-origin' });
        if (!response.ok) throw new Error('Could not check setup progress.');
        const result = await response.json();
        if (result.status === 'complete' && result.store_id) {
          const dashboard = `/console?store=${encodeURIComponent(result.store_id)}&onboarding=connection-test`;
          dashboardLink.href = dashboard;
          status('Store ready. Opening your dashboard…');
          window.location.replace(dashboard);
          return;
        }
      } catch (error) {
        status(`${error.message} Open the dashboard or check the terminal.`, true);
        watching = false;
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    status('Setup is taking longer than expected. Check the terminal, then open your dashboard.', true);
    watching = false;
  }
  createTab.hidden = !registrationEnabled;
  createTab.addEventListener('click', () => setMode('create'));
  loginTab.addEventListener('click', () => setMode('login'));
  document.getElementById('change-account').addEventListener('click', async () => {
    try {
      await post('/api/commerce/auth/logout', {});
      account = null;
      signedIn.hidden = true;
      form.hidden = false;
      document.querySelector('.tabs').hidden = false;
      if (googleOption.dataset.configured === 'true') googleOption.hidden = false;
      form.reset();
      status('Signed out. Choose an account to continue.');
    } catch (error) { status(error.message, true); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const password = form.elements.password.value;
    if (password.length < 12) { status('Your account password must have at least 12 characters. The terminal code is entered separately below.', true); return; }
    submit.disabled = true;
    status(mode === 'create' ? 'Creating your workspace…' : 'Signing in…');
    try {
      const fields = Object.fromEntries(new FormData(form));
      if (mode === 'login') delete fields.organization;
      const signedIn = await post(`/api/commerce/auth/${mode === 'create' ? 'register' : 'login'}`, fields);
      if (pendingGoogleCredential) {
        await post('/api/commerce/auth/google/link', { credential: pendingGoogleCredential });
        pendingGoogleCredential = null;
      }
      showAccount(signedIn);
    } catch (error) {
      if (mode === 'create' && error.status === 409) {
        setMode('login');
        form.elements.password.value = '';
        status('This email already has an account. Sign in with its existing password; the password from this registration attempt was not saved.', true);
        form.elements.password.focus();
      } else status(error.message, true);
    }
    finally { submit.disabled = false; }
  });
  code.addEventListener('input', () => {
    const digits = code.value.replace(/\D/g, '').slice(0, 8);
    code.value = digits.length > 4 ? `${digits.slice(0, 4)}-${digits.slice(4)}` : digits;
  });
  approve.addEventListener('click', async () => {
    if (!account) { status('Create an account or sign in first.', true); return; }
    if (!/^\d{4}-\d{4}$/.test(code.value)) { status('Enter the 8-digit code from your terminal.', true); code.focus(); return; }
    approve.disabled = true;
    status('Pairing your setup…');
    try {
      await post('/api/commerce/cli/approve', { request_id: requestId, user_code: code.value });
      status('Setup approved. Preparing your store and dashboard…');
      code.disabled = true;
      void watchCompletion();
    } catch (error) { status(error.message, true); approve.disabled = false; }
  });
  setMode(mode);
  fetch('/api/commerce/auth/providers', { credentials: 'same-origin' })
    .then(response => response.ok ? response.json() : null)
    .then(data => {
      if (!data?.google_client_id) return;
      googleOption.dataset.configured = 'true';
      if (!account) googleOption.hidden = false;
      const script = document.createElement('script');
      script.src = 'https://accounts.google.com/gsi/client';
      script.async = true;
      script.onload = () => {
        window.google.accounts.id.initialize({ client_id: data.google_client_id, callback: async result => {
          try { showAccount(await post('/api/commerce/auth/google', { credential: result.credential })); }
          catch (error) {
            if (error.status === 409) {
              pendingGoogleCredential = result.credential;
              setMode('login');
              status('This Google email already has an Auteric account. Sign in with its password to link Google explicitly.', true);
              form.elements.email.focus();
            } else status(error.message, true);
          }
        }});
        window.google.accounts.id.renderButton(document.getElementById('google-button'),
          { type: 'standard', theme: 'outline', size: 'large', width: 360, text: 'continue_with' });
      };
      script.onerror = () => { googleOption.hidden = true; status('Google sign-in could not load. Use your account password.', true); };
      document.head.append(script);
    })
    .catch(() => {});
  fetch('/api/commerce/auth/me', { credentials: 'same-origin' })
    .then(response => response.ok ? response.json() : null)
    .then(data => { if (data) showAccount(data); })
    .catch(() => {});
})();
