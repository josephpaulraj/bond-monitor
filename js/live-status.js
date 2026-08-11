// Phase 3 helper: load automated market-data status.
// Include this after the existing app.js if you want a status indicator.
async function loadBondMonitorLiveStatus() {
  try {
    const r = await fetch('./data/live.json?ts=' + Date.now(), {cache:'no-store'});
    const data = await r.json();
    window.BOND_MONITOR_LIVE = data;
    return data;
  } catch (e) {
    console.warn('Bond Monitor live data unavailable', e);
    return null;
  }
}
