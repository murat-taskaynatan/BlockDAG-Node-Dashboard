(() => {
  const now = () => Date.now();
  const clamp = (n,min,max)=>Math.max(min,Math.min(max,n));
  let INTERVAL = (typeof window.POLL_INTERVAL === 'number' && window.POLL_INTERVAL > 0) ? window.POLL_INTERVAL : 2000;

  // ring buffer
  function mkSeries(cap=360){
    const xs=[], ys=[];
    return {
      push(t,v){ xs.push(t); ys.push(v); if(xs.length>cap){ xs.shift(); ys.shift(); } },
      data(){ return { xs:[...xs], ys:[...ys] }; }
    };
  }

  const series = {
    height: mkSeries(), peers: mkSeries(), latency: mkSeries(), activity: mkSeries()
  };

  function mkChart(id, label){
    const el = document.getElementById(id); if(!el) return null;
    const ctx = el.getContext('2d');
    return new Chart(ctx, {
      type: 'line',
      data: { labels: [], datasets: [{ label, data: [], tension:.2, pointRadius:0, borderWidth:2 }]},
      options: {
        responsive:true, maintainAspectRatio:false, animation:false,
        scales:{
          x:{ type:'time', time:{ unit:'second' }, grid:{ display:false }},
          y:{ beginAtZero:true, grid:{ color:'rgba(255,255,255,.06)' }}
        },
        plugins:{ legend:{ display:true } }
      }
    });
  }

  const charts = { height:null, peers:null, latency:null, activity:null };

  function paint(chart, ser){
    const { xs, ys } = ser.data();
    chart.data.labels = xs;
    chart.data.datasets[0].data = ys.map((y,i)=>({x:xs[i], y}));
    chart.update('none');
  }

  async function preloadHistory(){
    try{
      const r = await fetch('/api/history', { cache:'no-store' });
      if(!r.ok) throw new Error('HTTP '+r.status);
      const H = await r.json();
      const fill = (k,ser)=>{ const L=(H[k]?.labels)||[], V=(H[k]?.series)||[]; for(let i=0;i<L.length;i++) ser.push(L[i], Number(V[i]||0)); };
      fill('height', series.height);
      fill('peers', series.peers);
      fill('latency', series.latency);
      fill('activity', series.activity);

      charts.height   ||= mkChart('heightChart','Height');
      charts.peers    ||= mkChart('peersChart','Peers');
      charts.latency  ||= mkChart('latencyChart','RPC Latency (ms)');
      charts.activity ||= mkChart('activityChart','Activity total');

      charts.height   && paint(charts.height,   series.height);
      charts.peers    && paint(charts.peers,    series.peers);
      charts.latency  && paint(charts.latency,  series.latency);
      charts.activity && paint(charts.activity, series.activity);

      console.log('[new2] history preloaded');
    }catch(e){
      console.warn('[new2] history preload failed:', e);
    }
  }

  async function fetchStatus(){
    try{
      const r = await fetch('/api/status', { cache:'no-store', headers:{'Cache-Control':'no-store'} });
      if(!r.ok) throw new Error('HTTP '+r.status);
      const d = await r.json();
      const t = now();

      const height  = Number(d.height ?? d.chain_height ?? d.block_height ?? 0);
      const peers   = Number(d.peers  ?? d.peer_count   ?? 0);
      const latency = Number(d.rpc_latency_ms ?? d.latency_ms ?? 0);
      const a = d.activity || {};
      const mined     = Number(a.mined?.rate_per_s ?? a.mined ?? 0);
      const processed = Number(a.processed?.rate_per_s ?? a.processed ?? 0);
      const sealed    = Number(a.sealed?.rate_per_s ?? a.sealed ?? 0);
      const total     = clamp(mined + processed + sealed, 0, 1e12);

      series.height.push(t, height);
      series.peers.push(t, peers);
      series.latency.push(t, latency);
      series.activity.push(t, total);

      charts.height   && paint(charts.height,   series.height);
      charts.peers    && paint(charts.peers,    series.peers);
      charts.latency  && paint(charts.latency,  series.latency);
      charts.activity && paint(charts.activity, series.activity);

    }catch(e){
      console.warn('[new2] live fetch failed:', e);
    }
  }

  const Guard = (window.__new2_guard = window.__new2_guard || {});
  function start(){
    if (Guard.id){ clearInterval(Guard.id); Guard.id = null; }
    if (typeof window.POLL_INTERVAL === 'number' && window.POLL_INTERVAL > 0) INTERVAL = window.POLL_INTERVAL;
    preloadHistory().finally(() => {
      fetchStatus();
      Guard.id = setInterval(fetchStatus, INTERVAL);
      console.log('[new2] polling @', INTERVAL, 'ms');
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden){ if (Guard.id){ clearInterval(Guard.id); Guard.id=null; } }
    else { start(); }
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
