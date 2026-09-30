/* Reusable dark dashboard gauge. Call ScoreGauge.render(element, score, label). */
(() => {
  const colors = Object.freeze({
    critical: '#ef4444', poor: '#d1665a', attention: '#4fa0c9',
    good: '#3fbfa8', excellent: '#38d9c9'
  });
  const toneFor = score => score < 25 ? 'critical' : score < 45 ? 'poor' : score < 65 ? 'attention' : score < 85 ? 'good' : 'excellent';
  const bounded = score => Math.max(0, Math.min(100, Number.isFinite(Number(score)) ? Number(score) : 0));
  const ticks = Array.from({length: 48}, (_, i) => {
    const angle = i * Math.PI / 24 - Math.PI / 2;
    const major = i % 2 === 0;
    const inner = major ? 99 : 102;
    return `<line x1="${(120 + inner * Math.cos(angle)).toFixed(2)}" y1="${(120 + inner * Math.sin(angle)).toFixed(2)}" x2="${(120 + 112 * Math.cos(angle)).toFixed(2)}" y2="${(120 + 112 * Math.sin(angle)).toFixed(2)}" stroke="rgba(255,255,255,${major ? '.16' : '.08'})" stroke-width="${major ? 2 : 1.5}" stroke-linecap="round"/>`;
  }).join('');

  function render(element, score, label = 'Score') {
    if (!element) return null;
    if (!element.querySelector('.score-gauge-svg')) {
      element.innerHTML = `<svg class="score-gauge-svg" viewBox="0 0 240 240" role="img" focusable="false">
        <circle class="score-gauge-halo" cx="120" cy="120" r="80" fill="none" stroke-width="38" opacity=".22" pathLength="100" transform="rotate(-90 120 120)"/>
        <g class="gauge-ticks" aria-hidden="true">${ticks}</g>
        <circle class="score-gauge-track" cx="120" cy="120" r="80" fill="none" stroke="rgba(255,255,255,.05)" stroke-width="24"/>
        <circle class="score-gauge-arc" cx="120" cy="120" r="80" fill="none" stroke-width="24" stroke-linecap="round" pathLength="100" transform="rotate(-90 120 120)"/>
        <circle class="score-gauge-knob" r="7" fill="#0b0e17" stroke="#fff" stroke-width="2.5"/>
        <text class="score-gauge-value" x="120" y="122" text-anchor="middle" font-size="56" font-weight="900">5</text>
        <text class="score-gauge-denominator" x="120" y="154" text-anchor="middle" font-size="16" font-weight="600">/100</text>
      </svg>`;
    }
    element.dataset.gaugeLabel = label;
    update(element, score);
    return element;
  }

  function update(element, score) {
    if (!element?.querySelector('.score-gauge-svg')) return;
    if (score === null || score === undefined) {
      const svg = element.querySelector('.score-gauge-svg');
      svg.setAttribute('aria-label', `${element.dataset.gaugeLabel || 'Score'}: not measured; safe test needed`);
      for (const name of ['halo', 'arc']) element.querySelector(`.score-gauge-${name}`).style.visibility = 'hidden';
      element.querySelector('.score-gauge-knob').style.visibility = 'hidden';
      element.querySelector('.score-gauge-value').textContent = '—';
      element.querySelector('.score-gauge-denominator').textContent = 'SAFE TEST';
      element.dataset.tone = 'unknown';
      element.style.setProperty('--gauge-color', '#9b91ad');
      return;
    }
    const value = Math.max(5, bounded(score));
    const tone = toneFor(value);
    const color = colors[tone];
    // Preserve a visible track gap at very high scores. Rounded caps otherwise make
    // 98–100 look indistinguishable from a completely filled ring.
    const displayValue = Math.min(96, value);
    const dash = `${displayValue} ${100 - displayValue}`;
    const angle = displayValue * Math.PI / 50 - Math.PI / 2;
    const svg = element.querySelector('.score-gauge-svg');
    svg.setAttribute('aria-label', `${element.dataset.gaugeLabel || 'Score'}: ${Math.round(value)} out of 100, ${tone}`);
    for (const name of ['halo', 'arc']) {
      const ring = element.querySelector(`.score-gauge-${name}`);
      ring.setAttribute('stroke', color);
      ring.setAttribute('stroke-dasharray', dash);
      ring.style.visibility = 'visible';
    }
    const knob = element.querySelector('.score-gauge-knob');
    knob.style.visibility = 'visible';
    knob.setAttribute('cx', (120 + 80 * Math.cos(angle)).toFixed(2));
    knob.setAttribute('cy', (120 + 80 * Math.sin(angle)).toFixed(2));
    element.querySelector('.score-gauge-value').textContent = String(Math.round(value));
    element.querySelector('.score-gauge-denominator').textContent = '/100';
    element.dataset.tone = tone;
    element.style.setProperty('--gauge-color', color);
  }

  window.ScoreGauge = Object.freeze({render, update, toneFor, colors});
})();
