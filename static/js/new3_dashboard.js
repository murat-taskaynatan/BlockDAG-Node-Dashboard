(() => {
  const now = () => Date.now();
  const clamp = (n,min,max)=>Math.max(min,Math.min(max,n));
  let INTERVAL = (typeof window.POLL_INTERVAL === 'number' && window.POLL_INTERVAL > 0) ? window.POLL_INTERVAL : 2000;

  function mkSeries(cap=360){
    const xs=[], ys=[];
    return {
      push(t,v){ xs.push(t); ys.push(v); if(xs.length>cap){ xs.shift(); ys.shift(); } },
      data(){ return { xs:[...xs], ys:[...ys] }; }
    };
  }

  const series = {
    mined: mkSeries(), processed: mkSeries(), sealed: mkSeries(), total: mkSeries()
  };

  function mkChart(id){
    const el = document.getElementById(id); if(!el) return null;
    const ctx = el.getContext('2d');
    return new Chart(ctx, {
      type: 'line',
      data: { labels: [], datasets: [
        { label:'Total',     data:[], tension:.2, pointRadius:0, borderWidth:2 },
        { label:'Mined/s',   data:[], tension:.2, pointRadius:0, borderWidth:1 },
        { label:'Processed/s',data:[], tension:.2, pointRadius:0, borderWidth:1 },
        { label:'Sealed/s',  data:[], tension:.2, pointRadius:0, borderWidth:1 },
      ]},
      options: {
        responsive:true, maintainAspectRatio:false, animation:false,
        scales:{
          x:{ type:'time', time:{ unit:'second' }, grid:{ display:false }},
          y:{ beginAtZero:true, grid:{ color:'rgba(255,255,255,.06)' }}
        },
        plugins:{ legend:{ display:true, position:'top' } }
      }
    });
  }

  const charts = { activity:null };

  function paint(){
    if (!charts.activity) return;
    const mined = series.mined.data(), processed = series.processed.data(), sealed = series.sealed.data(), total = series.total.data();
    // labels from total (same timestamps used for all)
    charts.activity.data.labels = total.xs;
    charts.activity.data.datasets[0].data = total.ys.map((y,i)=>({x: total.xs[i], y}));
    charts.activity.data.datasets[1].data = mined.ys.map((y,i)=>({x: mined.xs[i], y}));
    charts.activity.data.datasets[2].data = processed.ys.map((y,i)=>({x: processed.xs[i], y}));
    charts.activity.data.datasets[3].data = sealed.ys.map((y,i)=>({x: sealed.xs[i], y}));
    charts.activity.update('none');
  }

  async function preloadHistory(){
    try{
      const r = await fetch('/api/history', { cache:'no-store' });
      if(!r.ok) throw new Error('HTTP '+r.status);
      const H = await r.json();
      const fill = (k,ser)=>{ const L=(H[k]?.labels)||[], V=(H[k]?.series)||[]; for(let i=0;i<L.length;i++) ser.push(L[i], Number(V[i]||0)); };
      // components and total
      fill('mined',     series.mined);
      fill('processed', series.processed);
      fill('sealed',    series.sealed);
      fill('activity',  series.total);

      charts.activity ||= mkChart('activityChart');
      paint();
      console.log('[new3] history preloaded');
    }catch(e){
      console.warn('[new3] history preload failed:', e);
    }
  }

  async function fetchStatus(){
    try{
      const r = await fetch('/api/status', { cache:'no-store', headers:{'Cache-Control':'no-store'} });
      if(!r.ok) throw new Error('HTTP '+r.status);
      const d = await r.json();
      const t = now();

      const a = d.activity || {};
      const mined     = Number(a.mined?.rate_per_s ?? a.mined ?? 0);
      const processed = Number(a.processed?.rate_per_s ?? a.processed ?? 0);
      const sealed    = Number(a.sealed?.rate_per_s ?? a.sealed ?? 0);
      const total     = clamp(mined + processed + sealed, 0, 1e12);

      series.mined.push(t, mined);
      series.processed.push(t, processed);
      series.sealed.push(t, sealed);
      series.total.push(t, total);

      charts.activity ||= mkChart('activityChart');
      paint();
    }catch(e){
      console.warn('[new3] live fetch failed:', e);
    }
  }

  const Guard = (window.__new3_guard = window.__new3_guard || {});
  function start(){
    if (Guard.id){ clearInterval(Guard.id); Guard.id = null; }
    if (typeof window.POLL_INTERVAL === 'number' && window.POLL_INTERVAL > 0) INTERVAL = window.POLL_INTERVAL;
    preloadHistory().finally(() => {
      fetchStatus();
      Guard.id = setInterval(fetchStatus, INTERVAL);
      console.log('[new3] polling @', INTERVAL, 'ms');
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden){ if (Guard.id){ clearInterval(Guard.id); Guard.id=null; } }
    else { start(); }
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
