/* Shared code for the public DFW new-home deals site. Data: ../data/api/site.json (rebuilt every hour). */
const CONTACT = {
  name: "Conner Rutledge",
  phone: "(636) 698-8868",
  phoneHref: "tel:+16366988868",
  email: "conner@rutledgesellstexas.com",
  brokerage: "Regal Realtors",
};
const DATA_URL = "../data/api/site.json";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => (n || n === 0) ? "$" + Math.round(n).toLocaleString("en-US") : "";
const moneyK = (n) => {
  if (!n) return "";
  n = Math.abs(n);
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (n >= 1000) return "$" + Math.round(n / 1000) + "k";
  return "$" + n;
};
const shortDate = (iso) => iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "America/Chicago" }) : "";
const ago = (iso) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 60) return m <= 1 ? "just now" : m + " min ago";
  const h = Math.round(m / 60);
  if (h < 36) return h + (h === 1 ? " hour ago" : " hours ago");
  return Math.round(h / 24) + " days ago";
};

let _data;
async function loadData() {
  if (!_data) {
    _data = fetch(DATA_URL, { cache: "no-cache" }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then((d) => {
      d.byId = Object.fromEntries(d.homes.map((h) => [h.id, h]));
      d.homes.forEach((h, i) => (h.rank = i));   // feed is pre-sorted best deal first
      return d;
    });
  }
  return _data;
}

function shell(active) {
  const links = [["index.html", "Home", "home"], ["homes.html", "All Homes", "homes"], ["deals.html", "Deals of the Week", "deals"],
                 ["incentives.html", "Incentives", "incentives"], ["homes.html#map", "Map", "map"], ["contact.html", "Contact", "contact"]];
  const header = document.createElement("header");
  header.className = "top";
  header.innerHTML = `<div class="wrap">
      <a class="logo" href="index.html" aria-label="${esc(CONTACT.name)}, ${esc(CONTACT.brokerage)}: DFW New Home Deals">
        <span class="mono" aria-hidden="true">CR</span>
        <span><b>${esc(CONTACT.name)}</b><small>${esc(CONTACT.brokerage)}<span class="wide"> · New Home Deals</span></small></span></a>
      <button class="menu-btn" aria-expanded="false" aria-controls="nav">Menu</button>
      <nav class="main" id="nav">${links.map(([href, label, key]) =>
        `<a href="${href}"${key === active ? ' class="on" aria-current="page"' : ""}>${label}</a>`).join("")}</nav></div>`;
  document.body.prepend(header);
  const notice = document.createElement("div");
  notice.className = "notice";
  notice.innerHTML = 'DFW NEW HOME DEALS<span class="wide"> · Price cuts &amp; builder incentives, checked every hour</span>';
  document.body.prepend(notice);
  const btn = $(".menu-btn", header), nav = $("nav", header);
  btn.addEventListener("click", () => { nav.classList.toggle("open"); btn.setAttribute("aria-expanded", nav.classList.contains("open")); });
  const foot = document.createElement("footer");
  foot.innerHTML = `<div class="wrap"><div>
      <p>Prices, price cuts and incentives are read from each builder's own website and checked about every hour.
      Savings are estimates (the builder's own markdown or the drop we tracked, plus the advertised incentive's
      dollar value). Always confirm price, availability and incentive terms with the builder.</p>
      <p id="updated"></p></div>
      <div><b>${esc(CONTACT.name.toUpperCase())}</b><br><span style="letter-spacing:.2em;font-size:12.5px">${esc(CONTACT.brokerage.toUpperCase())}</span><br><a href="${CONTACT.phoneHref}">${esc(CONTACT.phone)}</a><br>
      <a href="mailto:${esc(CONTACT.email)}">${esc(CONTACT.email)}</a></div></div>`;
  document.body.append(foot);
  loadData().then((d) => { $("#updated").textContent = `Updated ${ago(d.updated_at)} · ${d.stats.homes.toLocaleString()} homes from ${d.stats.builders} builders`; }).catch(() => {});
}

/* What makes a home a deal, as short labels. */
function dealBits(h) {
  const tags = [];
  if (h.drop) tags.push(`<span class="tag drop">Price cut ${moneyK(h.drop)}${h.cuts > 1 ? ` (${h.cuts} cuts)` : ""}</span>`);
  if (h.discount && h.discount > (h.drop || 0)) tags.push(`<span class="tag mark">${moneyK(h.discount)} off builder's price</span>`);
  if (h.offer_value) tags.push(`<span class="tag">${moneyK(h.offer_value)} incentive</span>`);
  else if (h.rate) tags.push(`<span class="tag">Rate from ${h.rate}%</span>`);
  if (h.move_in) tags.push(`<span class="tag">${esc(h.move_in)}</span>`);
  return tags.join("");
}

function askLink(h) {
  const subj = encodeURIComponent(`Question about ${h.address || "a home"}${h.city ? ", " + h.city : ""}`);
  const body = encodeURIComponent(`Hi Conner,\n\nI'm interested in ${h.address || ""}${h.city ? ", " + h.city : ""} (${h.builder}, listed at ${money(h.price)}).\n${h.url || ""}\n\n`);
  return `mailto:${CONTACT.email}?subject=${subj}&body=${body}`;
}

function badge(h) {
  if (h.deal) return `<span class="badge">Save ~${moneyK(h.deal)}</span>`;
  if (h.rate) return `<span class="badge rate">${h.rate}% rate</span>`;
  return "";
}

function card(h, extra = "") {
  const specs = [h.beds && `${h.beds} bd`, h.baths && `${h.baths} ba`, h.sqft && `${h.sqft.toLocaleString()} sq ft`].filter(Boolean);
  const wasPrice = Math.max(h.was && h.was > h.price ? h.was : 0, h.drop ? h.first_price : 0) || null;
  return `<article class="card">
    <div class="band">${badge(h)}<div class="price">${money(h.price)}${wasPrice ? `<span class="was">${money(wasPrice)}</span>` : ""}</div>
      ${extra}</div>
    <div class="body">
      <div><div class="addr">${esc(h.address || "Address on request")}</div>
      <div class="sub">${esc([h.community, h.city].filter(Boolean).join(" · "))}${h.zip ? " " + esc(h.zip) : ""}</div>
      <div class="sub">${esc(h.builder)}</div></div>
      ${specs.length ? `<div class="specs">${specs.map((s) => `<span>${s}</span>`).join("")}</div>` : ""}
      <div class="tags">${dealBits(h)}</div>
      ${h.offer ? `<div class="offer" title="${esc(h.offer_full)}">${esc(h.offer)}</div>` : ""}
      <div class="actions">
        ${h.url ? `<a class="btn ghost small" href="${esc(h.url)}" target="_blank" rel="noopener">Builder's listing</a>` : ""}
        <a class="btn small" href="${askLink(h)}">Ask Conner</a>
      </div>
    </div></article>`;
}

/* Monday-to-Monday week label */
function weekLabel(w) {
  const s = new Date(w.start), e = new Date(new Date(w.end) - 864e5);
  const f = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "America/Chicago" });
  return `${f(s)} – ${f(e)}${w.current ? " (this week)" : ""}`;
}

function fail(el, err) {
  el.innerHTML = `<div class="empty">Couldn't load the latest homes right now. Please refresh in a minute.</div>`;
  console.error(err);
}
