(() => {
  const root = document.getElementById('claimFlow');
  if (!root) return;
  let claim;
  const message = document.getElementById('claimMessage');
  async function send(path, headers = {}) {
    const response = await fetch(path, {method: 'POST', headers});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not complete verification');
    return data;
  }
  document.getElementById('beginClaim').onclick = async function () {
    this.disabled = true;
    try {
      claim = await send('/api/claims/' + encodeURIComponent(root.dataset.domain));
      document.getElementById('claimUrl').textContent = claim.verification_url;
      document.getElementById('claimText').textContent = claim.challenge;
      document.getElementById('claimInstructions').hidden = false;
      message.textContent = 'Challenge created. Publish only the text shown above.';
    } catch (e) { message.textContent = e.message; this.disabled = false; }
  };
  document.getElementById('verifyClaim').onclick = async function () {
    this.disabled = true;
    try {
      await send('/api/claims/' + claim.claim_id + '/verify', {Authorization: 'Bearer ' + claim.owner_token});
      message.textContent = 'Ownership verified. Your store is claimed.';
      document.getElementById('claimNext').hidden = false;
    } catch (e) { message.textContent = e.message; this.disabled = false; }
  };
})();
