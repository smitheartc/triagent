"""The page jira_mock serves at GET /: a Jira-style board of the mock issues, for people watching the demo.

One self-contained HTML page (no external scripts, fonts or styles), served as is. It polls the mock's own
GET /issues every 2 s and the Triagent server's GET /jobs (the server allows any origin) for live job status; the
server's address comes from the mock's webhook URL in GET /health. Clicking a card opens the issue: the ticket as
it was raised (symptoms only), its details, and the activity feed with Triagent's comment rendered from markdown.
/#<ticket id> opens that issue directly.

Columns, derived from the issue's webhook state and its last comment:
  New                    webhook not delivered yet (settling, or the server is not answering)
  Triagent investigating webhook accepted, no comment yet (the card shows the job's live status)
  Triaged                last comment is a finished report
  Needs a human          last comment is a failed investigation, or the server rejected the webhook
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DC-OPS triage board</title>
<style>
:root {
  --bg: #ffffff; --surface: #f7f8f9; --card: #ffffff; --text: #172b4d; --muted: #626f86; --line: #dcdfe4;
  --accent: #0c66e4; --accent-soft: #e9f2ff; --ok: #1f845a; --ok-soft: #dcfff1; --bad: #c9372c; --bad-soft: #ffeceb;
  --warn: #a54800; --warn-soft: #fff3eb; --shadow: 0 1px 1px rgba(9,30,66,.25), 0 0 1px rgba(9,30,66,.31);
  --code: #f1f2f4;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1d2125; --surface: #161a1d; --card: #22272b; --text: #dee4ea; --muted: #9fadbc; --line: #38414a;
    --accent: #579dff; --accent-soft: #1c2b41; --ok: #4bce97; --ok-soft: #1c3329; --bad: #f87168; --bad-soft: #42221f;
    --warn: #fea362; --warn-soft: #3d2d1f; --shadow: 0 0 0 1px #38414a; --code: #2c333a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 20px; border-bottom: 1px solid var(--line); }
.logo { width: 28px; height: 28px; border-radius: 6px; background: var(--accent); color: #fff; display: grid;
        place-items: center; font-weight: 700; }
header h1 { font-size: 16px; margin: 0; }
header .sub { color: var(--muted); font-size: 13px; }
header .spacer { flex: 1; }
header select { background: var(--card); color: var(--text); border: 1px solid var(--line); border-radius: 4px;
                padding: 4px 8px; font: inherit; }
.live { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); }
.dot.off { background: var(--bad); }
main { display: grid; grid-template-columns: repeat(4, minmax(240px, 1fr)); gap: 12px; padding: 16px 20px;
       overflow-x: auto; }
.col { background: var(--surface); border-radius: 8px; padding: 8px; min-height: 60vh; }
.col h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 4px 6px 10px;
          display: flex; justify-content: space-between; }
.card { background: var(--card); border-radius: 6px; box-shadow: var(--shadow); padding: 10px 12px; margin-bottom: 8px;
        cursor: pointer; transition: transform .08s; }
.card:hover { transform: translateY(-1px); }
.card .title { margin: 0 0 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
.chip { font-size: 11px; padding: 1px 6px; border-radius: 3px; background: var(--accent-soft); color: var(--accent); }
.foot { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.key { font-weight: 600; color: var(--muted); }
.prio { font-size: 11px; font-weight: 700; padding: 0 5px; border-radius: 3px; }
.P1 { background: var(--bad-soft); color: var(--bad); } .P2 { background: var(--warn-soft); color: var(--warn); }
.P3, .P4 { background: var(--code); color: var(--muted); }
.status { margin-left: auto; display: flex; align-items: center; gap: 5px; }
.spin { width: 10px; height: 10px; border: 2px solid var(--accent-soft); border-top-color: var(--accent);
        border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.ok { color: var(--ok); } .bad { color: var(--bad); }
.empty { color: var(--muted); font-size: 12px; padding: 8px 6px; }
/* issue drawer */
#shade { position: fixed; inset: 0; background: rgba(9,30,66,.45); display: none; }
#drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(860px, 100vw); background: var(--bg);
          box-shadow: -4px 0 16px rgba(0,0,0,.2); display: none; overflow-y: auto; }
#drawer.open, #shade.open { display: block; }
.dhead { position: sticky; top: 0; background: var(--bg); padding: 14px 20px; border-bottom: 1px solid var(--line);
         display: flex; align-items: center; gap: 10px; }
.dhead button { margin-left: auto; background: none; border: 0; color: var(--muted); font-size: 22px; cursor: pointer; }
.dbody { display: grid; grid-template-columns: 1fr 240px; gap: 24px; padding: 16px 20px 40px; }
.dbody h3 { font-size: 13px; margin: 18px 0 6px; }
.dbody h2 { font-size: 20px; margin: 0 0 12px; }
.desc { background: var(--surface); border-radius: 6px; padding: 10px 12px; }
.note { font-size: 12px; color: var(--muted); }
dl { margin: 0; display: grid; grid-template-columns: 90px 1fr; gap: 6px 8px; font-size: 13px; }
dt { color: var(--muted); } dd { margin: 0; word-break: break-word; }
.comment { display: grid; grid-template-columns: 32px 1fr; gap: 10px; margin-top: 12px; }
.avatar { width: 32px; height: 32px; border-radius: 50%; background: #76b900; color: #fff; display: grid;
          place-items: center; font-weight: 700; }
.cmeta { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.cbody { border: 1px solid var(--line); border-radius: 6px; padding: 10px 14px; }
.cbody p { margin: 0 0 2px; } .cbody .gap { height: 8px; }
.cbody ul, .cbody ol { margin: 2px 0 6px; padding-left: 22px; }
code { background: var(--code); border-radius: 3px; padding: 0 4px; font: 12px ui-monospace, Menlo, Consolas, monospace; }
a { color: var(--accent); }
@media (max-width: 720px) {
  header { flex-wrap: wrap; padding: 10px 16px; }
  main { grid-template-columns: 1fr; padding: 12px 16px; }
  .col { min-height: auto; }
  .dbody { grid-template-columns: 1fr; padding: 12px 16px 32px; }
}
</style>
</head>
<body>
<header>
  <div class="logo">J</div>
  <div><h1>DC-OPS triage board</h1><div class="sub">Mock Jira · incident tickets raised from Elasticsearch</div></div>
  <div class="spacer"></div>
  <select id="ns" aria-label="Namespace"><option value="">All namespaces</option></select>
  <div class="live"><span class="dot" id="dot"></span><span id="livetext">live</span></div>
</header>
<main id="board"></main>
<div id="shade"></div>
<aside id="drawer" aria-label="Issue"></aside>
<script>
const COLS = [["new", "New"], ["investigating", "Triagent investigating"], ["triaged", "Triaged"],
              ["human", "Needs a human"]];
let server = null, issues = [], jobs = {}, openId = null, nsChoice = "", lastDrawer = "";
try { nsChoice = localStorage.getItem("ns") || ""; } catch (e) {}

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const when = iso => { const d = new Date(iso); return isNaN(d) ? esc(iso) : d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}); };

function column(i) {
  if (i.comment_status === "done") return "triaged";
  if (i.comment_status === "failed" || i.webhook.state === "error") return "human";
  if (i.webhook.state === "sent") return "investigating";
  return "new";
}

function statusLine(i) {
  const col = column(i), job = jobs[i.webhook.job_id];
  if (col === "new") return i.webhook.error ? `<span class="bad">server not answering (try ${i.webhook.attempts})</span>`
                                            : `<span>raising webhook…</span>`;
  if (col === "investigating") return `<span class="spin"></span><span>${esc(job ? job.status : "queued")}</span>`;
  if (col === "triaged") return job && job.entity ? `<span class="ok">✓ ${esc(job.category)} · <code>${esc(job.entity)}</code></span>`
                                                  : `<span class="ok">✓ report posted</span>`;
  return `<span class="bad">${i.webhook.state === "error" ? "webhook rejected" : "investigation failed"}</span>`;
}

function card(i) {
  const chips = [i.labels.rack && `rack ${i.labels.rack}`, i.labels.row && !i.labels.rack && `row ${i.labels.row}`,
                 i.service, i.labels.cluster].filter(Boolean);
  return `<div class="card" data-id="${esc(i.id)}" tabindex="0">
    <p class="title">${esc(i.title)}</p>
    <div class="chips">${chips.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</div>
    <div class="foot"><span class="prio ${esc(i.priority)}">${esc(i.priority)}</span><span class="key">${esc(i.id)}</span>
      <span class="status">${statusLine(i)}</span></div></div>`;
}

function render() {
  const shown = issues.filter(i => !nsChoice || i.namespace === nsChoice);
  document.getElementById("board").innerHTML = COLS.map(([key, name]) => {
    const rows = shown.filter(i => column(i) === key);
    return `<section class="col"><h2><span>${name}</span><span>${rows.length}</span></h2>
      ${rows.map(card).join("") || '<div class="empty">Nothing here</div>'}</section>`;
  }).join("");
  const sel = document.getElementById("ns"), names = [...new Set(issues.map(i => i.namespace))].sort();
  const want = ['<option value="">All namespaces</option>', ...names.map(n => `<option${n === nsChoice ? " selected" : ""}>${esc(n)}</option>`)].join("");
  if (sel.innerHTML !== want) sel.innerHTML = want;
}

function inline(s) {
  return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
               .replace(/(https?:\/\/[^\s)<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}
function md(text) {
  const out = []; let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of String(text).split("\n")) {
    let m;
    if ((m = line.match(/^\s*[-*] (.*)$/)) || (m = line.match(/^\s*\d+[.)] (.*)$/))) {
      const kind = /^\s*[-*] /.test(line) ? "ul" : "ol";
      if (list !== kind) { close(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    close();
    out.push(line.trim() ? `<p>${inline(line)}</p>` : '<div class="gap"></div>');
  }
  close();
  return out.join("");
}

async function openIssue(id) {
  openId = id;
  if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);   // shareable link to the issue
  let issue;
  try { issue = await (await fetch(`/issue/${encodeURIComponent(id)}`)).json(); } catch (e) { return; }
  if (openId !== id) return;
  const p = issue.payload || {}, wh = issue.webhook || {}, job = jobs[wh.job_id];
  const labels = Object.entries(p.labels || {}).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(", ") || "none";
  const jobLink = wh.job_id ? (server ? `<a href="${esc(server)}/jobs/${esc(wh.job_id)}" target="_blank" rel="noopener">${esc(wh.job_id)}</a>` : esc(wh.job_id)) : "not started";
  const comments = (issue.comments || []).map(c => `<div class="comment"><div class="avatar">T</div><div>
      <div class="cmeta"><strong>Triagent</strong> · ${when(c.created)} · job ${esc(c.job_id || "?")}${c.status === "failed" ? ' · <span class="bad">failed</span>' : ""}</div>
      <div class="cbody">${md(c.body)}</div></div></div>`).join("")
    || `<p class="note">${column({...issue, comment_status: null}) === "investigating" ? "Triagent is investigating; its report will appear here." : "No activity yet."}</p>`;
  const d = document.getElementById("drawer");
  const html = `<div class="dhead"><span class="prio ${esc(issue.priority)}">${esc(issue.priority)}</span><span class="key">${esc(issue.id)}</span>
      <button aria-label="Close" onclick="closeIssue()">×</button></div>
    <div class="dbody"><div>
      <h2>${esc(issue.title)}</h2>
      <h3>Description</h3><div class="desc">${esc(p.description) || "<span class='note'>No description</span>"}</div>
      <p class="note">As raised by monitoring: symptoms only, no cause.</p>
      <h3>Activity</h3>${comments}
    </div><div>
      <h3>Details</h3><dl>
        <dt>Status</dt><dd>${esc(COLS.find(c => c[0] === column({...issue, comment_status: (issue.comments || []).slice(-1)[0]?.status || null}))[1])}</dd>
        <dt>Assignee</dt><dd>Triagent</dd>
        <dt>Created</dt><dd>${when(issue.created)}</dd>
        <dt>Service</dt><dd>${esc(p.service || "none")}</dd>
        <dt>Host</dt><dd>${esc(p.host || "none")}</dd>
        <dt>Labels</dt><dd>${labels}</dd>
        <dt>Namespace</dt><dd>${esc(issue.namespace)}</dd>
        <dt>Webhook</dt><dd>${esc(wh.state)}${wh.attempts ? ` (${wh.attempts} attempt${wh.attempts > 1 ? "s" : ""})` : ""}</dd>
        <dt>Job</dt><dd>${jobLink}${job ? ` · ${esc(job.status)}` : ""}</dd>
        <dt>ES doc</dt><dd><code>${esc(issue.source_event_id)}</code></dd>
      </dl></div></div>`;
  if (html !== lastDrawer) { d.innerHTML = html; lastDrawer = html; }   // unchanged: keep scroll and selection
  d.classList.add("open"); document.getElementById("shade").classList.add("open");
}
function closeIssue() {
  openId = null; lastDrawer = "";
  history.replaceState(null, "", location.pathname);
  document.getElementById("drawer").classList.remove("open"); document.getElementById("shade").classList.remove("open");
}

async function tick() {
  try {
    issues = await (await fetch("/issues")).json();
    document.getElementById("dot").classList.remove("off"); document.getElementById("livetext").textContent = "live";
  } catch (e) {
    document.getElementById("dot").classList.add("off"); document.getElementById("livetext").textContent = "mock offline";
    return;
  }
  if (server) {
    try { for (const j of await (await fetch(`${server}/jobs`)).json()) jobs[j.job_id] = j; } catch (e) {}
  }
  render();
  if (openId) openIssue(openId);
}

document.getElementById("board").addEventListener("click", e => { const c = e.target.closest(".card"); if (c) openIssue(c.dataset.id); });
document.getElementById("board").addEventListener("keydown", e => { const c = e.target.closest(".card"); if (c && e.key === "Enter") openIssue(c.dataset.id); });
document.getElementById("shade").addEventListener("click", closeIssue);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeIssue(); });
document.getElementById("ns").addEventListener("change", e => {
  nsChoice = e.target.value; try { localStorage.setItem("ns", nsChoice); } catch (err) {} render();
});
fetch("/health").then(r => r.json()).then(h => {
  // The server is wherever the webhook goes; browsers on other machines reach it via this page's host.
  const u = new URL(h.webhook.replace(/\/webhook\/?$/, ""));
  if (["localhost", "127.0.0.1"].includes(u.hostname)) u.hostname = location.hostname;
  server = u.origin;
}).catch(() => {}).finally(() => {
  tick(); setInterval(tick, 2000);
  if (location.hash.length > 1) openIssue(decodeURIComponent(location.hash.slice(1)));
});
</script>
</body>
</html>
"""
