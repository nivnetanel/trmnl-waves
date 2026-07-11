const puppeteer = require('puppeteer');
const fetch = require('node-fetch');
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const GOSURF_URL = 'https://gosurf.co.il/forecast/tel-aviv';
const WEBHOOK    = process.env.TRMNL_WEBHOOK_URL;
const OUT_IMG    = path.join(__dirname, 'forecast.png');
const OUT_HTML   = path.join(__dirname, 'forecast.html');

const HE_DAYS = ['א׳', 'ב׳', 'ג׳', 'ד׳', 'ה׳', 'ו׳', 'ש׳'];

// ── 1. Scrape GoSurf ─────────────────────────────────────────────────────────

async function scrape() {
  const res = await fetch(GOSURF_URL, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Accept-Language': 'he-IL,he;q=0.9',
    },
  });
  if (!res.ok) throw new Error(`GoSurf fetch failed: ${res.status}`);
  const html = await res.text();
  const dom  = new JSDOM(html);
  const doc  = dom.window.document;

  // Try to pull structured weeklyData from the inline Chart.js script
  let weeklyData = null;
  for (const script of doc.querySelectorAll('script')) {
    const m = script.textContent.match(/var\s+weeklyData\s*=\s*(\[[\s\S]*?\]);/);
    if (m) { try { weeklyData = JSON.parse(m[1]); } catch (_) {} break; }
  }

  // Build 7-day array from weeklyData + DOM fallback
  const days = [];
  const today = new Date().getDay(); // 0=Sun … 6=Sat

  const rows = doc.querySelectorAll(
    '#website_forecast_weekly_cont .dayrow, ' +
    '#website_forecast_weekly_cont tr, ' +
    '.forecast_weekly_row, .weekly_day'
  );

  for (let i = 0; i < 7; i++) {
    const dayIndex = (today + i) % 7;
    const row = rows[i];

    // Wave height: prefer weeklyData chart data, fall back to DOM text
    let height = '—';
    if (weeklyData && weeklyData[i] != null) {
      height = String(parseFloat(weeklyData[i]).toFixed(1));
    } else if (row) {
      const h = row.querySelector('.wave_height, .waveheight, [class*="height"]');
      if (h) height = h.textContent.trim().replace(/[^\d.]/g, '') || '—';
    }

    // Rating (1-5 stars)
    let ratingNum = 0;
    if (row) {
      const r = row.querySelector('.rating, [class*="rating"], [class*="stars"]');
      if (r) {
        const n = parseFloat(r.textContent.trim());
        if (!isNaN(n)) ratingNum = Math.round(n);
      }
    }
    const stars = '★'.repeat(ratingNum) + '☆'.repeat(5 - ratingNum);

    // Wind
    let wind = '';
    if (row) {
      const w = row.querySelector('.wind, [class*="wind"]');
      if (w) wind = w.textContent.trim().replace(/\s+/g, ' ');
    }

    // Period
    let period = '';
    if (row) {
      const p = row.querySelector('.period, [class*="period"]');
      if (p) period = p.textContent.trim();
    }

    days.push({
      label: i === 0 ? `${HE_DAYS[dayIndex]}\nהיום` : HE_DAYS[dayIndex],
      height,
      stars,
      wind:   wind   || '—',
      period: period || '',
      today:  i === 0,
    });
  }

  if (days.every(d => d.height === '—')) {
    throw new Error('No wave data found — GoSurf DOM structure may have changed. Check selectors.');
  }

  return days;
}

// ── 2. Generate HTML ──────────────────────────────────────────────────────────

function buildHTML(days) {
  const updated = new Date().toLocaleTimeString('he-IL', { hour: '2-digit', minute: '2-digit' });

  const cards = days.map(d => {
    const cls  = d.today ? 'card today' : 'card';
    const label = d.label.replace('\n', '<br>');
    return `
      <div class="${cls}">
        <div class="c-day">${label}</div>
        <div class="c-sep"></div>
        <div class="c-height">${d.height}</div>
        <div class="c-unit">מטר</div>
        <div class="c-sep2"></div>
        <div class="c-stars">${d.stars}</div>
        <div class="c-wind">${d.wind}</div>
        ${d.period ? `<div class="c-period">${d.period}</div>` : ''}
      </div>`;
  }).join('');

  return `<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; width: 800px; height: 480px; overflow: hidden; }
  body {
    width: 800px; height: 480px;
    background: #fff; color: #000;
    font-family: Arial, sans-serif;
    direction: rtl;
    display: flex; flex-direction: column;
    padding: 20px 22px 16px;
  }
  .hdr {
    display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 2.5px solid #000; padding-bottom: 10px; margin-bottom: 16px;
    flex-shrink: 0;
  }
  .hdr-title { font-size: 21px; font-weight: 900; letter-spacing: -0.3px; margin: 0; }
  .hdr-updated { font-size: 11px; color: #555; }
  .grid {
    display: grid; grid-template-columns: repeat(7, 1fr);
    gap: 9px; flex: 1; min-height: 0;
  }
  .card {
    border: 2px solid #000; border-radius: 6px;
    padding: 9px 4px 7px;
    display: flex; flex-direction: column; align-items: center; gap: 0;
  }
  .card.today { background: #000; color: #fff; }
  .c-day { font-size: 11px; font-weight: 700; text-align: center; line-height: 1.2; }
  .c-sep  { width: 60%; height: 1px; background: currentColor; opacity: 0.3; margin: 6px 0 5px; }
  .c-sep2 { width: 60%; height: 1px; background: currentColor; opacity: 0.2; margin: 6px 0 5px; }
  .c-height { font-size: 26px; font-weight: 900; line-height: 1; }
  .c-unit   { font-size: 10px; opacity: 0.6; margin-top: 1px; }
  .c-stars  { font-size: 10px; letter-spacing: 0.5px; }
  .c-wind   { font-size: 10px; margin-top: 5px; text-align: center; line-height: 1.3; opacity: 0.8; }
  .c-period { font-size: 10px; opacity: 0.5; margin-top: 3px; }
  .ftr {
    flex-shrink: 0; margin-top: 14px;
    border-top: 1px solid #ccc; padding-top: 7px;
    display: flex; justify-content: space-between; align-items: center;
    font-size: 10px; color: #777;
  }
  .ftr-brand { font-size: 9px; letter-spacing: 3px; text-transform: uppercase; color: #bbb; }
</style>
</head>
<body>
  <div class="hdr">
    <span class="hdr-title">תחזית גלים שבועית — תל אביב</span>
    <span class="hdr-updated">עודכן ${updated}</span>
  </div>
  <div class="grid">${cards}</div>
  <div class="ftr">
    <span>מקור: gosurf.co.il</span>
    <span class="ftr-brand">TRMNL</span>
  </div>
</body>
</html>`;
}

// ── 3. Screenshot via Puppeteer ───────────────────────────────────────────────

async function screenshot(html) {
  fs.writeFileSync(OUT_HTML, html, 'utf8');

  const browser = await puppeteer.launch({
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 800, height: 480, deviceScaleFactor: 1 });
    await page.goto('file://' + OUT_HTML, { waitUntil: 'networkidle0' });
    await page.screenshot({ path: OUT_IMG, type: 'png',
      clip: { x: 0, y: 0, width: 800, height: 480 } });
  } finally {
    await browser.close();
  }
}

// ── 4. POST image to TRMNL webhook ────────────────────────────────────────────

async function pushToTRMNL() {
  if (!WEBHOOK) throw new Error('TRMNL_WEBHOOK_URL env var is not set');

  const img = fs.readFileSync(OUT_IMG);
  const res = await fetch(WEBHOOK, {
    method: 'POST',
    headers: { 'Content-Type': 'image/png' },
    body: img,
  });

  if (res.status === 200) {
    console.log('✓ Image pushed to TRMNL successfully');
  } else if (res.status === 429) {
    throw new Error('TRMNL rate limit hit (12/hour)');
  } else {
    const body = await res.text();
    throw new Error(`TRMNL webhook error ${res.status}: ${body}`);
  }
}

// ── main ──────────────────────────────────────────────────────────────────────

(async () => {
  try {
    console.log('Scraping GoSurf...');
    const days = await scrape();
    console.log(`Got ${days.length} days, today wave: ${days[0].height}m`);

    console.log('Generating HTML...');
    const html = buildHTML(days);

    console.log('Screenshotting...');
    await screenshot(html);

    console.log('Pushing to TRMNL...');
    await pushToTRMNL();

  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
})();
