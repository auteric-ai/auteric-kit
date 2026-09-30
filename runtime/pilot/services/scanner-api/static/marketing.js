(function () {
  function track(name, properties = {}) {
    const detail = { name, properties: { ...properties, path: location.pathname } };
    window.dataLayer = window.dataLayer || [];
    window.dataLayer.push({ event: name, ...detail.properties });
    window.dispatchEvent(new CustomEvent('auteric:analytics', { detail }));
  }

  function normalizeUrl(value) {
    const raw = String(value || '').trim();
    if (!raw) throw new Error('Enter your store URL.');
    const localScanner = ['localhost', '127.0.0.1', '::1'].includes(location.hostname) || location.hostname.endsWith('.localhost');
    const localInput = /^(?:localhost|(?:[\w-]+\.)+localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(raw);
    const url = new URL(/^https?:\/\//i.test(raw) ? raw : `${localInput ? 'http' : 'https'}://${raw}`);
    const loopback = url.hostname === 'localhost' || url.hostname.endsWith('.localhost') || url.hostname === '::1' || /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || (!loopback && !url.hostname.includes('.')) || (loopback && !localScanner)) {
      throw new Error(localScanner ? 'Enter a valid store URL.' : 'Enter a valid public store domain, such as example.com.');
    }
    url.hash = '';
    return url.href;
  }

  document.querySelectorAll('[data-scan-form]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const input = form.querySelector('[name="storeUrl"]');
      const status = form.querySelector('[data-form-status]');
      const button = form.querySelector('button[type="submit"]');
      try {
        input.removeAttribute('aria-invalid');
        const target = normalizeUrl(input.value);
        status.textContent = 'Starting your non-destructive scan…';
        button.disabled = true;
        track('scan_started', { target_domain: new URL(target).hostname, landing_page: location.pathname });
        const response = await fetch('/api/scans', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target_url: target, adapter: 'auto' }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || `Scan could not start (${response.status}).`);
        location.href = `/scan/${encodeURIComponent(new URL(target).hostname)}?id=${encodeURIComponent(payload.scan_id)}`;
      } catch (error) {
        input.setAttribute('aria-invalid', 'true');
        status.textContent = error.message || 'The scan could not start. Please try again.';
        button.disabled = false;
        track('scan_failed', { stage: 'start', message: status.textContent });
      }
    });
  });

  document.querySelectorAll('a[href="/shopify-ai-shopping"]').forEach((link) => {
    link.addEventListener('click', () => track('shopify_interest_clicked'));
  });
  document.querySelectorAll('a[href="/woocommerce-ai-shopping"]').forEach((link) => {
    link.addEventListener('click', () => track('woo_interest_clicked'));
  });

  const params = new URLSearchParams(location.search);
  track(location.pathname === '/' ? 'homepage_view' : 'landing_page_view', {
    referrer: document.referrer || null,
    utm_source: params.get('utm_source'),
    utm_medium: params.get('utm_medium'),
    utm_campaign: params.get('utm_campaign'),
  });
})();
