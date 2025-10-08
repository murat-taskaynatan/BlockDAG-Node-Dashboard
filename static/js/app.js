
document.addEventListener('DOMContentLoaded', () => {
  const interval = (window.POLL_INTERVAL || 2000);
  async function fetchStatus() {
    try {
      const res = await fetch('/api/status', { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      window.onStatus && window.onStatus(data);
    } catch (err) {
      console.warn('fetchStatus failed:', err);
    }
  }

  fetchStatus();
  setInterval(fetchStatus, interval);
});

document.addEventListener('DOMContentLoaded', () => {
  async function fetchStatus() {
    try {
      const res = await fetch('/api/status', { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      window.onStatus && window.onStatus(data);
    } catch (err) {
      console.warn('fetchStatus failed:', err);
    }
  }

  fetchStatus();
  setInterval(fetchStatus, interval);
});
