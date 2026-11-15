// Stany sklepów – drobne interakcje panelu (bez zewnętrznych bibliotek)
(function () {
  // podpowiedzi na wykresach + linia celownika
  const tip = document.createElement("div");
  tip.id = "tip";
  document.body.appendChild(tip);
  document.addEventListener("mousemove", (e) => {
    const hit = e.target.closest && e.target.closest("[data-tip]");
    if (!hit) { tip.style.opacity = 0; hideCross(); return; }
    const [title, ...rows] = hit.dataset.tip.split("|");
    tip.innerHTML = "<b>" + esc(title) + "</b>" + rows.map(esc).join("<br>");
    tip.style.opacity = 1;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + 14, y = e.clientY - h - 12;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
    if (y < 8) y = e.clientY + 16;
    tip.style.left = x + "px"; tip.style.top = y + "px";
    const svg = hit.ownerSVGElement;
    if (svg && hit.dataset.x) {
      const ch = svg.querySelector(".crosshair");
      if (ch) {
        ch.setAttribute("x1", hit.dataset.x); ch.setAttribute("x2", hit.dataset.x);
        ch.setAttribute("y1", svg.dataset.top); ch.setAttribute("y2", svg.dataset.bottom);
        ch.style.opacity = 1;
      }
    }
  });
  function hideCross() { document.querySelectorAll(".crosshair").forEach((c) => (c.style.opacity = 0)); }
  function esc(s) { return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  // menu na telefonie
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-menu]")) { document.body.classList.toggle("menu-open"); }
    else if (e.target.classList.contains("scrim")) { document.body.classList.remove("menu-open"); }
  });

  // gwiazdka „obserwuj” bez przeładowania strony
  document.addEventListener("submit", async (e) => {
    const f = e.target;
    if (!f.matches("form[data-watch]")) return;
    e.preventDefault();
    const btn = f.querySelector(".star");
    try {
      const r = await fetch(f.action, { method: "POST", headers: { Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded" }, body: "action=toggle" });
      const j = await r.json();
      btn.classList.toggle("on", j.watched);
      btn.title = j.watched ? "Usuń z obserwowanych" : "Obserwuj";
    } catch (_) { f.submit(); }
  });

  // filtrowanie tabel na bieżąco
  document.querySelectorAll("input[data-filter]").forEach((inp) => {
    const tbl = document.querySelector(inp.dataset.filter);
    if (!tbl) return;
    inp.addEventListener("input", () => {
      const q = inp.value.trim().toLowerCase();
      tbl.querySelectorAll("tbody tr").forEach((tr) => {
        tr.style.display = !q || tr.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  });

  // odświeżanie paska postępu pobierania
  const run = document.querySelector("[data-running]");
  if (run) {
    const t = setInterval(async () => {
      try {
        const s = await (await fetch("/status")).json();
        if (!s.running) { clearInterval(t); location.reload(); return; }
        const txt = run.querySelector("[data-run-text]");
        if (txt) txt.textContent = [s.source, s.page ? "strona " + s.page : "", s.found ? s.found + " produktów" : ""]
          .filter(Boolean).join(" · ");
      } catch (_) {}
    }, 4000);
  }
})();
