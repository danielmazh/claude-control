/* Claude Control — Chrome extension popup (quick actions over the local server) */
const API = "http://127.0.0.1:7799";
const $ = id => document.getElementById(id);

const I18N = {
  he: {
    q5h: "מכסת 5 שעות", qwk: "מכסה שבועית", target: "יעד פתיחה",
    presets: "פרופילים", sounds: "צלילי התראה", open: "פתח",
    expand: "⤢ פתח את הדשבורד המלא", opened: "נפתח ✓", saved: "נשמר ✓",
    off1: "השרת המקומי כבוי", off2: "הרץ בטרמינל:",
    term: "חלון Terminal", tmux_new: "tmux · סשן חדש (claude)", err: "שגיאה: ",
  },
  en: {
    q5h: "5-hour quota", qwk: "Weekly quota", target: "Launch target",
    presets: "Profiles", sounds: "Notification sounds", open: "Open",
    expand: "⤢ Open the full dashboard", opened: "Opened ✓", saved: "Saved ✓",
    off1: "Local server is off", off2: "Run in a terminal:",
    term: "Terminal window", tmux_new: "tmux · new session (claude)", err: "Error: ",
  },
};
let L = localStorage.getItem("cc-ext-lang") || "he";
const t = k => I18N[L][k] || k;
let S = null;

const toast = m => { const x = $("toast"); x.textContent = m; x.classList.add("on");
  setTimeout(() => x.classList.remove("on"), 1600); };
const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cls = p => p >= 85 ? "hot" : p >= 60 ? "warn" : "";

function setLang(l) {
  L = l; localStorage.setItem("cc-ext-lang", l);
  document.documentElement.lang = l;
  document.documentElement.dir = l === "he" ? "rtl" : "ltr";
  $("fl-he").classList.toggle("on", l === "he");
  $("fl-en").classList.toggle("on", l === "en");
  render();
}

async function api(path, body) {
  const r = await fetch(API + path, body ? { method: "POST", body: JSON.stringify(body) } : {});
  const j = await r.json();
  if (j.error) { toast(t("err") + j.error); throw j.error; }
  if (j.state) S = j.state;
  return j;
}

function render() {
  if (!S) return renderOff();
  const lim = S.limits || {};
  const ses = Math.floor(lim.five_hour?.utilization ?? 0);
  const wk = Math.floor(lim.seven_day?.utilization ?? 0);
  const savedTgt = localStorage.getItem("cc-ext-target") || "";
  const tgts = [
    ...S.tmux.map(x => ({ v: "tmux:" + x.name, l: "tmux · " + x.name })),
    ...(S.tmux_available ? [{ v: "tmux:__new__", l: t("tmux_new") }] : []),
    { v: "terminal", l: t("term") }];
  $("main").innerHTML = `
    <div class="quota"><div class="l"><span>${t("q5h")}</span><b>${ses}%</b></div>
      <div class="bar"><i class="${cls(ses)}" style="width:${ses}%"></i></div></div>
    <div class="quota"><div class="l"><span>${t("qwk")}</span><b>${wk}%</b></div>
      <div class="bar"><i class="${cls(wk)}" style="width:${wk}%"></i></div></div>
    <div class="sect">${t("target")}</div>
    <select id="tgt">${tgts.map(o => `<option value="${esc(o.v)}"
      ${o.v === savedTgt ? "selected" : ""}>${esc(o.l)}</option>`).join("")}</select>
    <div class="sect">${t("presets")}</div>
    <div id="plist">${S.presets.map(p => `
      <div class="preset">
        <span class="dot" style="background:${p.custom ? "var(--good)" : "var(--blue)"}"></span>
        <span class="nm">${esc(p.name)}</span>
        <span class="est">~${p.est}k</span>
        <button data-id="${esc(p.id)}">${t("open")}</button>
      </div>`).join("")}</div>
    <div class="rowline"><span>${t("sounds")}</span>
      <label class="tg"><input type="checkbox" id="snd" ${S.sounds ? "checked" : ""}><span></span></label></div>
    <button class="expand" id="expand">${t("expand")}</button>`;

  $("tgt").onchange = e => localStorage.setItem("cc-ext-target", e.target.value);
  $("snd").onchange = e =>
    api("/api/sounds", { enabled: e.target.checked }).then(() => toast(t("saved")));
  $("expand").onclick = () => chrome.tabs.create({ url: API });
  document.querySelectorAll("#plist button").forEach(b => b.onclick = () => {
    const v = $("tgt").value;
    const target = v.startsWith("tmux:")
      ? { type: "tmux", session: v.slice(5) } : { type: "terminal" };
    api("/api/launch", { preset: b.dataset.id, target })
      .then(j => toast(j.tmux ? `${t("opened")} ${j.tmux}` : t("opened")));
  });
}

function renderOff() {
  $("main").innerHTML = `<div class="off">⏻ ${t("off1")}<br>${t("off2")}<br>
    <code>cl-dash</code></div>`;
}

$("fl-he").onclick = () => setLang("he");
$("fl-en").onclick = () => setLang("en");
$("refresh").onclick = () =>
  api("/api/state").then(j => { S = j; render(); toast("⟳"); }).catch(renderOff);

setLang(L);
api("/api/state").then(j => { S = j; render(); }).catch(renderOff);
