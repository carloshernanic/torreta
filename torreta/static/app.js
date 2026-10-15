/* Torreta — interface. Sem frameworks, sem build, sem rastreamento. */
(() => {
  "use strict";

  // ---------------------------------------------------------------- token/API
  const params = new URLSearchParams(location.search);
  let token = params.get("token");
  try {
    if (token) sessionStorage.setItem("torreta-token", token);
    else token = sessionStorage.getItem("torreta-token");
  } catch (_) { /* sessionStorage indisponível: segue só com a URL */ }
  if (params.get("token")) history.replaceState(null, "", location.pathname);

  async function api(method, path, body, isForm) {
    const headers = { "X-Torreta-Token": token || "" };
    if (body && !isForm) headers["Content-Type"] = "application/json";
    const res = await fetch("/api" + path, {
      method, headers, body: isForm ? body : body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let msg = `Erro ${res.status}`;
      try { const j = await res.json(); msg = j.error || j.detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const fmtBytes = (n) => {
    n = Number(n) || 0;
    const u = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
  };
  const fmtRate = (n) => (n > 0 ? fmtBytes(n) + "/s" : "");
  const fmtEta = (s) => {
    if (s == null) return "";
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}min`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}min`;
    return `${Math.floor(s / 86400)}d`;
  };
  const toast = (msg, kind = "info", ms = 4000) => {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), ms);
  };
  const categoryOf = (t) => {
    if (t.issue) return "problem";
    if (t.paused) return "paused";
    if (t.finished) return "done";
    return "downloading";
  };

  // -------------------------------------------------------------------- state
  let state = { torrents: [], totals: {}, settings: {}, can_pick_folder: false };
  let filter = "all";
  let selectedId = null;
  let filesCache = [];
  const rows = new Map(); // id -> <li>

  // ------------------------------------------------------------------- render
  function render() {
    $("#tot-down").textContent = fmtRate(state.totals.down_rate) || "0 B/s";
    $("#tot-up").textContent = fmtRate(state.totals.up_rate) || "0 B/s";

    const counts = { all: 0, downloading: 0, done: 0, paused: 0, problem: 0 };
    const list = $("#list");
    const seen = new Set();
    let shown = 0;

    for (const t of state.torrents) {
      const cat = categoryOf(t);
      counts.all++; counts[cat]++;
      seen.add(t.id);
      let li = rows.get(t.id);
      if (!li) {
        li = $("#row-tpl").content.firstElementChild.cloneNode(true);
        li.dataset.id = t.id;
        rows.set(t.id, li);
        list.appendChild(li);
      }
      const visible = filter === "all" || filter === cat;
      li.hidden = !visible;
      if (visible) shown++;
      updateRow(li, t, cat);
    }
    for (const [id, li] of rows) if (!seen.has(id)) { li.remove(); rows.delete(id); }
    // mantém a ordem do servidor (mais recentes primeiro)
    state.torrents.forEach((t, i) => {
      const li = rows.get(t.id);
      if (list.children[i] !== li) list.insertBefore(li, list.children[i] || null);
    });

    for (const k in counts) {
      const el = $(`[data-count="${k}"]`);
      if (el) el.textContent = counts[k] ? counts[k] : "";
    }
    $("#empty").hidden = shown > 0;
    if (shown === 0 && counts.all > 0) {
      $("#empty").querySelector("p").textContent = "Nada nesta categoria.";
    } else {
      $("#empty").querySelector("p").textContent = "Nenhum torrent ainda.";
    }

    if (selectedId) {
      const t = state.torrents.find((x) => x.id === selectedId);
      if (t) renderDetails(t); else closeDetails();
    }
  }

  function updateRow(li, t, cat) {
    li.className = `row ${cat}${t.id === selectedId ? " selected" : ""}`;
    $(".name", li).textContent = t.name;
    $(".name", li).title = t.name;
    const badge = $(".badge", li);
    badge.textContent = t.state;
    badge.className = `badge ${t.state.replace(/\s+/g, "-")}`;
    $(".fill", li).style.width = `${Math.round(t.progress * 1000) / 10}%`;
    $(".s-size", li).textContent = t.has_metadata
      ? `${fmtBytes(t.done)} / ${fmtBytes(t.size)} (${(t.progress * 100).toFixed(1)}%)`
      : "aguardando metadados";
    $(".s-down", li).textContent = t.down_rate ? `↓ ${fmtRate(t.down_rate)}` : "";
    $(".s-up", li).textContent = t.up_rate ? `↑ ${fmtRate(t.up_rate)}` : "";
    $(".s-peers", li).textContent = t.peers || t.seeds ? `${t.seeds} seeds · ${t.peers} peers` : "";
    $(".s-eta", li).textContent = t.eta != null && !t.finished ? `faltam ${fmtEta(t.eta)}` : "";
    const issue = $(".issue", li);
    issue.hidden = !t.issue;
    issue.textContent = t.issue || "";

    const toggle = $(".a-toggle", li);
    toggle.textContent = t.paused ? "▶" : "❚❚";
    toggle.title = t.paused ? "Retomar" : "Pausar";
    const seq = $(".a-seq", li);
    seq.classList.toggle("on", t.sequential);
  }

  // ------------------------------------------------------------------ details
  async function openDetails(id) {
    selectedId = id;
    $("#details").hidden = false;
    render();
    await loadFiles();
  }
  function closeDetails() {
    selectedId = null;
    filesCache = [];
    $("#details").hidden = true;
    render();
  }
  function renderDetails(t) {
    $("#d-name").textContent = t.name;
    $("#d-path").textContent = t.save_path;
    $("#d-size").textContent = t.has_metadata ? fmtBytes(t.size) : "—";
    $("#d-transfer").textContent = `${fmtBytes(t.downloaded)} / ${fmtBytes(t.uploaded)}`;
    $("#d-ratio").textContent = t.downloaded ? (t.uploaded / t.downloaded).toFixed(2) : "—";
    $("#d-hash").textContent = t.id;
    $("#d-files-empty").hidden = t.has_metadata;
  }
  async function loadFiles() {
    if (!selectedId) return;
    try {
      const r = await api("GET", `/torrents/${selectedId}/files`);
      filesCache = r.files;
      renderFiles();
    } catch (e) { toast(e.message, "error"); }
  }
  function renderFiles() {
    const ul = $("#d-files");
    ul.innerHTML = "";
    for (const f of filesCache) {
      const li = document.createElement("li");
      li.className = `file${f.priority === 0 ? " skipped" : ""}`;
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = f.priority > 0;
      cb.title = "Baixar este arquivo";
      cb.addEventListener("change", () => setPriority({ [f.index]: cb.checked ? 4 : 0 }));
      const name = document.createElement("span");
      name.className = "fname"; name.textContent = f.path; name.title = f.path;
      const size = document.createElement("span");
      size.className = "fsize"; size.textContent = fmtBytes(f.size);
      const bar = document.createElement("div");
      bar.className = "fbar";
      const fill = document.createElement("div");
      fill.style.width = f.size ? `${(100 * f.done) / f.size}%` : "0%";
      bar.appendChild(fill);
      li.append(cb, name, size, bar);
      ul.appendChild(li);
    }
  }
  async function setPriority(map) {
    try {
      await api("PUT", `/torrents/${selectedId}/files`, { priorities: map });
      await loadFiles();
    } catch (e) { toast(e.message, "error"); await loadFiles(); }
  }
  $("#f-all").addEventListener("click", () =>
    setPriority(Object.fromEntries(filesCache.map((f) => [f.index, 4]))));
  $("#f-none").addEventListener("click", () =>
    setPriority(Object.fromEntries(filesCache.map((f) => [f.index, 0]))));
  $("#d-close").addEventListener("click", closeDetails);

  // ------------------------------------------------------------------ actions
  async function addMagnet() {
    const input = $("#add-input");
    const uri = input.value.trim();
    if (!uri) return;
    try {
      await api("POST", "/torrents", { uri });
      input.value = "";
      toast("Torrent adicionado.", "ok");
      await refresh();
    } catch (e) { toast(e.message, "error"); }
  }
  async function addFiles(files) {
    for (const file of files) {
      if (!/\.torrent$/i.test(file.name)) { toast(`"${file.name}" não é um .torrent.`, "error"); continue; }
      const fd = new FormData();
      fd.append("file", file, file.name);
      try {
        await api("POST", "/torrents/file", fd, true);
        toast(`"${file.name}" adicionado.`, "ok");
      } catch (e) { toast(`${file.name}: ${e.message}`, "error"); }
    }
    await refresh();
  }
  $("#btn-add").addEventListener("click", addMagnet);
  $("#add-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addMagnet(); });
  $("#btn-file").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });

  // colar magnet em qualquer lugar da página adiciona direto
  document.addEventListener("paste", (e) => {
    if (e.target.closest("input, textarea")) return;
    const text = (e.clipboardData || window.clipboardData).getData("text") || "";
    const t = text.trim().toLowerCase();
    if (t.startsWith("magnet:") || (/^https?:\/\//.test(t) && t.includes(".torrent"))) {
      $("#add-input").value = text.trim();
      addMagnet();
    }
  });

  // drag & drop
  let dragDepth = 0;
  document.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("#drop").hidden = false; });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop").hidden = true; } });
  document.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; $("#drop").hidden = true;
    const files = [...(e.dataTransfer.files || [])];
    if (files.length) return addFiles(files);
    const text = e.dataTransfer.getData("text") || "";
    if (text.trim().toLowerCase().startsWith("magnet:")) { $("#add-input").value = text.trim(); addMagnet(); }
  });

  // ações por linha
  $("#list").addEventListener("click", async (e) => {
    const li = e.target.closest(".row");
    if (!li) return;
    const id = li.dataset.id;
    const t = state.torrents.find((x) => x.id === id);
    const btn = e.target.closest("button");
    if (!btn) { if (selectedId === id) closeDetails(); else openDetails(id); return; }
    e.stopPropagation();
    try {
      if (btn.classList.contains("a-toggle")) {
        await api("POST", `/torrents/${id}/${t.paused ? "resume" : "pause"}`);
      } else if (btn.classList.contains("a-seq")) {
        await api("POST", `/torrents/${id}/sequential`, { on: !t.sequential });
        toast(t.sequential ? "Download em ordem desligado." : "Baixando em ordem: dá para abrir o vídeo enquanto baixa.", "ok");
      } else if (btn.classList.contains("a-open")) {
        await api("POST", `/torrents/${id}/open`);
      } else if (btn.classList.contains("a-remove")) {
        askRemove(t); return;
      }
      await refresh();
    } catch (err) { toast(err.message, "error", 6000); }
  });

  // remoção
  let removeTarget = null;
  function askRemove(t) {
    removeTarget = t;
    $("#remove-name").textContent = t.name;
    $("#form-remove").elements.delete_files.checked = false;
    $("#dlg-remove").showModal();
  }
  $("#remove-cancel").addEventListener("click", () => $("#dlg-remove").close());
  $("#form-remove").addEventListener("submit", async (e) => {
    e.preventDefault();
    const del = e.target.elements.delete_files.checked;
    $("#dlg-remove").close();
    if (!removeTarget) return;
    try {
      await api("DELETE", `/torrents/${removeTarget.id}?delete_files=${del}`);
      if (selectedId === removeTarget.id) closeDetails();
      toast(del ? "Torrent e arquivos removidos." : "Torrent removido (arquivos mantidos).", "ok");
      await refresh();
    } catch (err) { toast(err.message, "error"); }
  });

  // filtros
  $("#filters").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-filter]");
    if (!b) return;
    filter = b.dataset.filter;
    for (const x of $("#filters").children) x.classList.toggle("active", x === b);
    render();
  });

  // configurações
  const dlg = $("#dlg-settings");
  const form = $("#form-settings");
  $("#btn-settings").addEventListener("click", () => {
    const s = state.settings;
    form.elements.download_dir.value = s.download_dir || "";
    form.elements.listen_port.value = s.listen_port || 6881;
    form.elements.download_limit_kib.value = s.download_limit_kib || 0;
    form.elements.upload_limit_kib.value = s.upload_limit_kib || 0;
    form.elements.sequential_default.checked = !!s.sequential_default;
    $("#btn-pick").hidden = !state.can_pick_folder;
    const note = $("#port-note");
    const busy = state.listen_port_effective && state.listen_port_effective !== s.listen_port;
    note.hidden = !busy;
    if (busy) note.textContent = `A porta ${s.listen_port} está em uso por outro programa; o Torreta está usando a ${state.listen_port_effective}.`;
    dlg.showModal();
  });
  $("#settings-cancel").addEventListener("click", () => dlg.close());
  $("#btn-pick").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/pick-folder");
      if (r.path) form.elements.download_dir.value = r.path;
    } catch (e) { toast(e.message, "error"); }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      download_dir: form.elements.download_dir.value,
      listen_port: Number(form.elements.listen_port.value),
      download_limit_kib: Number(form.elements.download_limit_kib.value),
      upload_limit_kib: Number(form.elements.upload_limit_kib.value),
      sequential_default: form.elements.sequential_default.checked,
    };
    try {
      state.settings = await api("PUT", "/settings", body);
      dlg.close();
      toast("Configurações salvas.", "ok");
    } catch (err) { toast(err.message, "error", 6000); }
  });

  // ------------------------------------------------------------------ polling
  let failures = 0;
  let portWarned = false;
  async function refresh() {
    try {
      state = await api("GET", "/state");
      failures = 0;
      render();
      if (!portWarned && state.listen_port_effective && state.listen_port_effective !== state.settings.listen_port) {
        portWarned = true;
        toast(`A porta ${state.settings.listen_port} está ocupada por outro programa (outro cliente torrent aberto?). Usando a porta ${state.listen_port_effective}.`, "info", 9000);
      }
      if (selectedId && !$("#details").hidden) {
        // atualiza progresso por arquivo sem recarregar a lista inteira a cada tick
        const t = state.torrents.find((x) => x.id === selectedId);
        if (t && t.has_metadata && (filesCache.length === 0 || !t.finished)) await loadFiles();
      }
    } catch (e) {
      if (++failures === 3) toast("Perdi contato com o Torreta. Ele ainda está aberto?", "error", 8000);
    }
  }
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
