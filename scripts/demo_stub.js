// Read-only snapshot: answers the web app's API calls from saved JSON files. Inserted by export_demo.py.
(() => {
  const cache = {}, real = window.fetch.bind(window);
  const load = name => cache[name] = cache[name] || real(`data/${name}.json`).then(r => r.json());
  const reply = (body, status = 200) => ({status, ok: status < 400, json: async () => body});
  window.fetch = async (input, options = {}) => {
    const url = new URL(String(input), location.href);
    const path = url.pathname.replace(/^.*\/api\//, '/api/');
    if (!path.startsWith('/api/')) return real(input, options);
    if ((options.method || 'GET') !== 'GET') return reply({error: 'This snapshot is read-only.'}, 403);
    const q = Object.fromEntries(url.searchParams);
    if (path.startsWith('/api/people/')) {
      const profiles = await load('profiles');
      return reply(profiles[decodeURIComponent(path.slice(12))] || {profile: null, history: [], offers: [], themes: []});
    }
    if (path === '/api/people') {
      const d = await load('people');
      const people = d.people.filter(p => (!q.status || p.status === q.status) && (!q.customer_id || p.customer_id === q.customer_id));
      return reply({...d, people});
    }
    if (path === '/api/variations') {
      const d = await load('variations');
      const matches = d.variations.filter(v => (!q.asset || v.asset_id === q.asset) && (!q.channel || v.channel === q.channel)
        && (!q.theme || v.image_id.replace(/-\d+$/, '') === q.theme) && (!q.status || v.status === q.status));
      const page = Math.max(1, Number(q.page) || 1);
      return reply({...d, matching: matches.length, page, variations: matches.slice((page - 1) * d.page_size, page * d.page_size)});
    }
    return reply(await load(path.slice(5)));
  };
  try { sessionStorage.setItem('pw', 'snapshot') } catch (e) {}
  addEventListener('DOMContentLoaded', () => {
    const note = document.createElement('div');
    note.className = 'notice';
    note.style.margin = '0';
    note.textContent = `Read-only snapshot of the deployed web app, saved on ${DEMO_DATE}. All people are simulated. Approval buttons do not save.`;
    document.body.prepend(note);
    const out = document.getElementById('signout');
    if (out) out.hidden = true;
  });
})();
