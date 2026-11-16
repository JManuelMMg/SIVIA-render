/* Tablero · de la validación a la orden de trabajo.
   Al confirmar un candidato del modelo se abre un formulario (ya prellenado con lo que vio la IA) para describir
   la falla, la acción y el plazo; al enviarlo, la orden se guarda y se turna a la dependencia que corresponde.
   En la pestaña Órdenes aparece la bandeja "Órdenes turnadas", donde cada dependencia avanza su estado. */
(() => {
  if (typeof DATOS === "undefined") return;
  const listaIA = document.getElementById("listaIA");
  const paneOrdenes = document.getElementById("pane-ordenes");
  if (!listaIA || !paneOrdenes) return;

  const ACCIONES = {
    arboles_cables: "Poda de las ramas que tocan el cableado y revisión del árbol",
    baches_banquetas: "Bacheo y reparación de la superficie de rodamiento o banqueta",
    alcantarilla_riesgo: "Colocar o reponer la tapa, reparar el brocal y señalizar de inmediato",
    alcantarilla_tapada: "Desazolve y limpieza de la rejilla pluvial antes de lluvias",
    cableado_postes: "Retiro o tensado del cableado colgante y revisión del poste",
    sin_cruce: "Balizamiento del cruce peatonal y señalización vertical",
  };
  const SEVERIDADES = [["urgente", "Urgente", 2], ["alta", "Alta", 5], ["media", "Media", 15], ["baja", "Baja", 30]];
  const ESTADOS = [["asignada", "Asignada"], ["en_atencion", "En atención"], ["resuelta", "Resuelta"], ["descartada", "Descartada"]];
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const categoria = h => (typeof textoCategoria !== "undefined" && textoCategoria[h.categoria]) || h.modelo_clase || h.categoria || "Falla";
  let deps = null, turnadas = [];

  async function cargaDependencias() {
    if (deps) return deps;
    try {
      const r = await fetch("/api/ordenes/dependencias", { cache: "no-store" });
      const j = r.ok ? await r.json() : null;
      if (j && j.ok && j.dependencias.length) deps = j.dependencias;
    } catch (_) { /* sin base: se usan las del tablero */ }
    if (!deps) deps = [...new Set(DATOS.map(h => h.dependencia).filter(Boolean))].sort();
    return deps;
  }

  /* ---------- formulario ---------- */
  const dlg = document.createElement("dialog");
  dlg.className = "ot-dialogo";
  dlg.innerHTML = `
    <form method="dialog" class="ot-form" novalidate>
      <header class="ot-cab">
        <div class="ot-foto"></div>
        <div><p class="ot-sobre">Confirmar hallazgo y crear orden de trabajo</p><h2 class="ot-titulo"></h2><p class="ot-lugar"></p></div>
        <button type="button" class="ot-cerrar" aria-label="Cerrar sin crear la orden">×</button>
      </header>
      <div class="ot-campos">
        <label class="ot-ancho">Tipo de falla<input name="hallazgo" required maxlength="120"></label>
        <label>Dependencia que la atiende<select name="dependencia" required></select></label>
        <label>Severidad<select name="severidad">${SEVERIDADES.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select></label>
        <label>Plazo de atención (días)<input name="plazo_dias" type="number" min="1" max="180" required></label>
        <label class="ot-ancho">Ubicación / referencia<input name="ubicacion" maxlength="200"></label>
        <label class="ot-ancho">Descripción de la falla<textarea name="descripcion" rows="3" required maxlength="600"></textarea></label>
        <label class="ot-ancho">Acción solicitada<textarea name="accion" rows="2" required maxlength="300"></textarea></label>
        <label class="ot-ancho"><span>Notas para la cuadrilla <span class="ot-opc">(opcional)</span></span><textarea name="notas" rows="2" maxlength="600"></textarea></label>
      </div>
      <p class="ot-error" role="alert" hidden></p>
      <footer class="ot-pie">
        <button type="button" class="ot-btn ot-sec" data-cancelar>Cancelar</button>
        <button type="submit" class="ot-btn ot-pri"></button>
      </footer>
    </form>`;
  document.body.append(dlg);
  const form = dlg.querySelector("form"), fError = dlg.querySelector(".ot-error"), fEnviar = dlg.querySelector(".ot-pri");
  let actual = null, plazoTocado = false;
  const cierra = () => { dlg.close(); actual = null; };
  dlg.querySelector(".ot-cerrar").addEventListener("click", cierra);
  dlg.querySelector("[data-cancelar]").addEventListener("click", cierra);
  form.plazo_dias.addEventListener("input", () => (plazoTocado = true));
  form.severidad.addEventListener("change", () => {
    if (!plazoTocado) form.plazo_dias.value = SEVERIDADES.find(s => s[0] === form.severidad.value)[2];
  });
  form.dependencia.addEventListener("change", () => (fEnviar.textContent = `Crear orden y turnar a ${form.dependencia.value}`));

  async function abre(id) {
    const h = DATOS.find(d => d.id === id);
    if (!h) return;
    actual = h; plazoTocado = false;
    const lista = await cargaDependencias();
    form.dependencia.innerHTML = lista.map(d => `<option>${esc(d)}</option>`).join("");
    if (h.dependencia && !lista.includes(h.dependencia)) form.dependencia.insertAdjacentHTML("afterbegin", `<option>${esc(h.dependencia)}</option>`);
    form.dependencia.value = h.dependencia || lista[0];
    const sev = h.modelo_clase === "alcantarilla_riesgo" ? "urgente"
      : (["urgente", "alta", "media", "baja"].includes(String(h.severidad).toLowerCase()) ? String(h.severidad).toLowerCase() : "media");
    form.severidad.value = sev;
    form.plazo_dias.value = SEVERIDADES.find(s => s[0] === sev)[2];
    form.hallazgo.value = categoria(h);
    const la = Number(h.lat), ln = Number(h.lon);
    form.ubicacion.value = `${h.nombre || "Vía pública"}${Number.isFinite(la) ? ` (${la.toFixed(5)}, ${ln.toFixed(5)})` : ""}`;
    form.descripcion.value = h.descripcion || "";
    form.accion.value = ACCIONES[h.categoria] || ACCIONES[h.modelo_clase] || "Revisión en sitio y reparación";
    form.notas.value = "";
    dlg.querySelector(".ot-titulo").textContent = `${categoria(h)} · IA ${Math.round(Number(h.confianza || 0) * 100)}%`;
    dlg.querySelector(".ot-lugar").textContent = [h.nombre, h.fecha_captura].filter(Boolean).join(" · ");
    const foto = dlg.querySelector(".ot-foto");
    foto.replaceChildren();
    const src = typeof imageSource === "function" ? imageSource(h.imagen) : "";
    if (src) { const im = new Image(); im.alt = "Evidencia"; im.src = src; im.onerror = () => im.remove(); foto.append(im); }
    fError.hidden = true;
    fEnviar.disabled = false;
    fEnviar.textContent = `Crear orden y turnar a ${form.dependencia.value}`;
    dlg.showModal();
    form.descripcion.focus();
  }

  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (!actual) return;
    for (const campo of ["hallazgo", "descripcion", "accion", "plazo_dias"]) {
      if (!form[campo].checkValidity() || !String(form[campo].value).trim()) {
        fError.textContent = "Completa el tipo de falla, la descripción, la acción y un plazo de 1 a 180 días.";
        fError.hidden = false; form[campo].focus(); return;
      }
    }
    const datos = Object.fromEntries(new FormData(form));
    Object.assign(datos, { candidato_id: actual.id, lat: actual.lat, lng: actual.lon,
      prioridad: typeof prioridad === "function" ? prioridad(actual) : Number(actual.prioridad_base || 0) });
    fEnviar.disabled = true; fEnviar.textContent = "Turnando…";
    try {
      const r = await fetch("/api/ordenes/nueva", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(datos) });
      const j = await r.json().catch(() => ({}));
      if (r.status === 401 && j.login) { location.href = "/login"; return; }
      if (!r.ok || !j.ok) throw new Error(j.error || `error ${r.status}`);
      const h = actual;
      revisiones[h.id] = "confirmado";
      try { localStorage.setItem(REVIEW_KEY, JSON.stringify(revisiones)); } catch (_) {}
      h.dependencia = j.dependencia;
      cierra();
      indicadores(); pintarIA(); pintarAnalisis(); pintarOrdenes(); if (typeof pintarMapa === "function") pintarMapa();
      avisa(`Orden ${j.folio} creada y turnada a ${j.dependencia} (plazo ${j.plazo_dias} días).`);
      bandeja();
    } catch (err) {
      fError.textContent = `No se pudo crear la orden: ${err.message}.`;
      fError.hidden = false; fEnviar.disabled = false;
      fEnviar.textContent = `Crear orden y turnar a ${form.dependencia.value}`;
    }
  });

  // "Confirmar" en Validación IA abre el formulario en lugar de confirmar directo
  listaIA.addEventListener("click", e => {
    const b = e.target.closest('[data-action="confirmar"]');
    if (!b) return;
    e.stopPropagation(); e.preventDefault();
    abre(b.closest(".ai-card").dataset.id);
  }, true);
  const cambiaTexto = () => listaIA.querySelectorAll('[data-action="confirmar"]').forEach(b => {
    if (b.textContent !== "Confirmar y crear orden") b.textContent = "Confirmar y crear orden";
  });
  new MutationObserver(cambiaTexto).observe(listaIA, { childList: true });
  cambiaTexto();

  /* ---------- aviso ---------- */
  const aviso = document.createElement("div");
  aviso.className = "cam-aviso"; aviso.setAttribute("role", "status"); aviso.hidden = true;
  document.body.append(aviso);
  let reloj;
  function avisa(t) { aviso.textContent = t; aviso.hidden = false; clearTimeout(reloj); reloj = setTimeout(() => (aviso.hidden = true), 7000); }

  /* ---------- bandeja de órdenes turnadas (pestaña Órdenes) ---------- */
  const caja = document.createElement("section");
  caja.className = "ot-bandeja";
  caja.innerHTML = `<div class="ot-bandeja-cab"><h3>Órdenes turnadas a dependencias</h3><span class="ot-bandeja-res"></span></div>
    <div class="ot-bandeja-lista"><p class="ot-vacio">Aún no hay órdenes turnadas. Confirma un candidato en Validación IA para crear la primera.</p></div>`;
  paneOrdenes.querySelector(".intro").after(caja);
  const bLista = caja.querySelector(".ot-bandeja-lista"), bRes = caja.querySelector(".ot-bandeja-res");

  function pintaBandeja() {
    const filtro = document.getElementById("filtroDependencia");
    const dep = filtro && filtro.value && filtro.value !== "Todas" ? filtro.value : null;
    const filas = turnadas.filter(o => !dep || o.dependencia === dep);
    const abiertas = filas.filter(o => o.estado === "asignada" || o.estado === "en_atencion").length;
    bRes.textContent = filas.length ? `${filas.length} órdenes · ${abiertas} abiertas · ${filas.filter(o => o.vencida).length} vencidas` : "";
    if (!filas.length) {
      bLista.innerHTML = `<p class="ot-vacio">${turnadas.length ? "No hay órdenes turnadas a esa dependencia." : "Aún no hay órdenes turnadas. Confirma un candidato en Validación IA para crear la primera."}</p>`;
      return;
    }
    bLista.innerHTML = filas.map(o => {
      const limite = new Date(new Date(o.creada_en).getTime() + (o.plazo_dias || 15) * 864e5);
      return `<article class="ot-fila ot-${esc(o.estado)}" data-cand="${esc(o.candidato_id)}" data-id="${esc(o.id)}">
        <div class="ot-fila-a"><b class="ot-folio">${esc(o.folio)}</b><span class="ot-sev ot-sev-${esc(o.severidad)}">${esc(o.severidad)}</span>
          ${o.vencida ? '<span class="ot-vencida">Vencida</span>' : ""}</div>
        <div class="ot-fila-b"><strong>${esc(o.hallazgo)}</strong> · ${esc(o.institucion || o.ubicacion || "Vía pública")}
          <div class="ot-meta">${esc(o.dependencia)} · atender antes del ${limite.toLocaleDateString("es-MX", { day: "numeric", month: "short" })}</div>
          <div class="ot-meta">${esc(o.accion)}</div></div>
        <label class="ot-estado"><span class="ot-sr">Estado de ${esc(o.folio)}</span>
          <select>${ESTADOS.map(([v, t]) => `<option value="${v}"${v === o.estado ? " selected" : ""}>${t}</option>`).join("")}</select></label>
      </article>`;
    }).join("");
    bLista.querySelectorAll(".ot-fila").forEach(fila => {
      fila.addEventListener("click", e => { if (!e.target.closest("select, label") && typeof focusMarker === "function") focusMarker(fila.dataset.cand); });
      const sel = fila.querySelector("select");
      sel.addEventListener("change", async () => {
        const previo = turnadas.find(o => o.id === fila.dataset.id).estado;
        sel.disabled = true;
        try {
          const r = await fetch(`/api/ordenes/${encodeURIComponent(fila.dataset.id)}`, { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ estado: sel.value }) });
          const j = await r.json().catch(() => ({}));
          if (!r.ok || !j.ok) throw new Error(j.error || `error ${r.status}`);
          turnadas.find(o => o.id === fila.dataset.id).estado = sel.value;
          avisa(`${fila.querySelector(".ot-folio").textContent}: ${sel.options[sel.selectedIndex].text}.`);
          pintaBandeja();
        } catch (err) {
          sel.value = previo; sel.disabled = false;
          avisa(`No se pudo cambiar el estado: ${err.message}.`);
        }
      });
    });
  }

  async function bandeja() {
    try {
      const r = await fetch("/api/ordenes/turnadas", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (!j.ok) return;
      turnadas = j.ordenes;
      pintaBandeja();
    } catch (_) { /* sin base de datos */ }
  }
  const filtro = document.getElementById("filtroDependencia");
  if (filtro) filtro.addEventListener("change", pintaBandeja);
  bandeja();
  setInterval(bandeja, 30000);
})();
