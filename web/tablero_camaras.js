/* Tablero · cámaras de los vehículos y recorrido en vivo.
   - Cada 20 s trae las detecciones nuevas de las cámaras (modelo YOLO11s v9 a bordo) y las suma a Validación IA.
   - Cada 3 s dibuja en el mapa dónde va cada vehículo y su recorrido.
   - La pestaña "Recorrido en vivo" muestra lo que ve la cámara (con los recuadros del modelo), el avance y lo
     detectado. GDL-SIM es un recorrido simulado que la propia plataforma reproduce para todos los usuarios. */
(() => {
  if (typeof DATOS === "undefined" || typeof mapa === "undefined") return;
  const vistos = new Set(DATOS.map(h => h.id));
  let capaVeh = null, primera = true;
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nombreClase = c => String(c || "").replace(/_/g, " ");

  /* ---------- aviso ---------- */
  const aviso = document.createElement("div");
  aviso.className = "cam-aviso";
  aviso.setAttribute("role", "status");
  aviso.setAttribute("aria-live", "polite");
  aviso.hidden = true;
  document.body.append(aviso);
  let reloj;
  function avisa(txt) {
    aviso.textContent = txt; aviso.hidden = false;
    clearTimeout(reloj); reloj = setTimeout(() => (aviso.hidden = true), 6000);
  }

  /* ---------- detecciones de cámara -> Validación IA ---------- */
  async function detecciones() {
    try {
      const r = await fetch("/api/camara/candidatos", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (!j.ok) return;
      let nuevos = 0, ultimo = null;
      for (const h of j.candidatos) {
        const previo = DATOS.find(d => d.id === h.id);
        if (previo) { Object.assign(previo, { vistas: h.vistas, descripcion: h.descripcion, confianza: h.confianza }); continue; }
        if (vistos.has(h.id)) continue;
        vistos.add(h.id); DATOS.push(h); nuevos++; ultimo = h;
      }
      if (nuevos) {
        indicadores(); pintarMapa(); pintarIA(); pintarAnalisis(); pintarOrdenes();
        if (!primera) avisa(nuevos === 1
          ? `Nueva detección de la cámara ${ultimo.vehiculo}: ${nombreClase(ultimo.modelo_clase)} cerca de ${ultimo.nombre}. Revísala en Validación IA.`
          : `${nuevos} detecciones nuevas de las cámaras. Revísalas en Validación IA.`);
      }
    } catch (_) { /* sin conexión: se intenta en la siguiente vuelta */ }
    primera = false;
  }

  /* ---------- pestaña "Recorrido en vivo" ---------- */
  const tabs = document.querySelector(".tab")?.parentElement;
  const paneRef = document.getElementById("pane-validacion");
  if (!tabs || !paneRef) return;
  const boton = document.createElement("button");
  boton.className = "tab"; boton.dataset.tab = "recorrido"; boton.type = "button";
  boton.innerHTML = `<span class="rec-tab-luz" hidden aria-hidden="true"></span>Recorrido en vivo`;
  document.querySelector('.tab[data-tab="validacion"]').after(boton);
  boton.addEventListener("click", () => { mostrarTab("recorrido"); vista(); });

  const pane = document.createElement("section");
  pane.className = "pane"; pane.id = "pane-recorrido";
  pane.innerHTML = `
    <div class="intro"><h2>Recorrido en vivo</h2>
      <p>Lo que ve la cámara de cada vehículo mientras recorre la ciudad, con los recuadros de lo que detecta el modelo.
      <b>GDL-SIM</b> es un recorrido simulado con fotos reales de calles de Guadalajara (Mapillary, CC BY-SA 4.0): la plataforma
      lo reproduce para todas las personas que entran al tablero.</p></div>
    <div class="rec-vehiculos" role="group" aria-label="Elegir vehículo"></div>
    <p class="rec-sin" hidden>No hay vehículos transmitiendo en este momento.</p>
    <div class="rec-cuerpo" hidden>
      <div class="rec-video">
        <div class="rec-marco"><img hidden alt="Último cuadro que vio la cámara, con lo que detectó el modelo">
          <p class="rec-espera">Esperando el primer cuadro…</p>
          <span class="rec-envivo"><span class="rec-luz" aria-hidden="true"></span><span class="rec-envivo-txt">EN VIVO</span></span>
        </div>
        <div class="rec-avance" aria-hidden="true"><span></span></div>
      </div>
      <div class="rec-lado">
        <dl class="rec-datos">
          <div><dt>Cuadro</dt><dd data-d="cuadro">–</dd></div>
          <div><dt>Recorrido</dt><dd data-d="km">–</dd></div>
          <div><dt>Modelo</dt><dd data-d="ms">–</dd></div>
          <div><dt>Última señal</dt><dd data-d="hace">–</dd></div>
        </dl>
        <div><h3 class="rec-sub">En este cuadro</h3><ul class="rec-dets"></ul></div>
        <div class="rec-hist-caja"><h3 class="rec-sub">Detectado en el recorrido <span class="rec-hist-n"></span></h3><ol class="rec-hist"></ol></div>
        <div class="rec-acciones">
          <button type="button" class="rec-btn" data-ver>Ver en el mapa</button>
          <label class="rec-seguir"><input type="checkbox"> Seguir al vehículo</label>
        </div>
      </div>
    </div>`;
  paneRef.parentElement.insertBefore(pane, paneRef.nextSibling);
  const $ = s => pane.querySelector(s);
  const pImg = $(".rec-marco img"), pEspera = $(".rec-espera"), pCuerpo = $(".rec-cuerpo"), pSin = $(".rec-sin");
  const pVehs = $(".rec-vehiculos"), pDets = $(".rec-dets"), pHist = $(".rec-hist"), pHistN = $(".rec-hist-n");
  const pSeguir = $(".rec-seguir input"), pAvance = $(".rec-avance span"), pEnvivo = $(".rec-envivo");
  const dato = k => pane.querySelector(`[data-d="${k}"]`);

  let elegido = null, urlVista = null, activos = [];
  const historial = {};                // vehículo -> [{cuadro, clase, confianza}]
  const ultimoCuadro = {};

  function centra(zoom) {
    const v = activos.find(x => x.vehiculo === elegido);
    if (v) mapa.setView([v.lat, v.lng], zoom || Math.max(mapa.getZoom(), 15), { animate: true });
  }
  $("[data-ver]").addEventListener("click", () => {
    centra(15);
    document.getElementById("mapa")?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  pSeguir.addEventListener("change", () => { if (pSeguir.checked) centra(); });

  /* chip compacto sobre el mapa: no tapa el recorrido y lleva a la pestaña */
  const Chip = L.Control.extend({
    onAdd() {
      const b = L.DomUtil.create("button", "rec-chip");
      b.type = "button";
      L.DomEvent.disableClickPropagation(b);
      b.addEventListener("click", () => {
        mostrarTab("recorrido"); vista();
        pane.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      return b;
    },
  });
  const chip = new Chip({ position: "topright" }).addTo(mapa).getContainer();
  chip.hidden = true;

  function pintaVehiculos() {
    pVehs.replaceChildren(...activos.map(v => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "rec-veh" + (v.vehiculo === elegido ? " activo" : "");
      b.setAttribute("aria-pressed", v.vehiculo === elegido ? "true" : "false");
      b.textContent = v.simulado ? `${v.vehiculo} · simulado` : v.vehiculo;
      b.addEventListener("click", () => { elegido = v.vehiculo; pinta(); vista(); });
      return b;
    }));
    pVehs.hidden = activos.length < 2;
  }

  function pinta() {
    const v = activos.find(x => x.vehiculo === elegido);
    pSin.hidden = !!v; pCuerpo.hidden = !v;
    boton.querySelector(".rec-tab-luz").hidden = !activos.length;
    chip.hidden = !v;
    pintaVehiculos();
    if (!v) return;
    const quieto = v.hace_s > 15;
    chip.innerHTML = `<span class="rec-luz${quieto ? " quieta" : ""}"></span>${esc(v.vehiculo)} ${v.simulado ? "simulado" : "en vivo"} · <u>ver cámara</u>`;
    pEnvivo.classList.toggle("quieta", quieto);
    pEnvivo.querySelector(".rec-envivo-txt").textContent = `${quieto ? "SIN SEÑAL" : v.simulado ? "SIMULADO" : "EN VIVO"} · ${v.vehiculo}`;
    dato("cuadro").textContent = v.cuadro ? (v.total ? `${v.cuadro} de ${v.total}` : String(v.cuadro)) : "–";
    dato("km").textContent = v.km ? `${v.km.toFixed(1)} km` : "–";
    dato("ms").textContent = v.ms ? `${v.ms} ms por cuadro` : "–";
    dato("hace").textContent = v.hace_s <= 2 ? "ahora" : `hace ${v.hace_s} s`;
    pAvance.style.width = v.total ? `${Math.min(100, (100 * v.cuadro) / v.total)}%` : "0";
    pDets.replaceChildren(...(v.dets && v.dets.length ? v.dets : [null]).map(d => {
      const li = document.createElement("li");
      if (!d) { li.className = "rec-nada"; li.textContent = "Sin fallas en este cuadro"; }
      else li.textContent = `${nombreClase(d.clase)} · ${Math.round(d.confianza * 100)}%`;
      return li;
    }));
    const h = historial[v.vehiculo] || [];
    pHistN.textContent = h.length ? `(${h.length})` : "";
    pHist.replaceChildren(...(h.length ? h.slice(-8).reverse() : [null]).map(x => {
      const li = document.createElement("li");
      if (!x) { li.className = "rec-nada"; li.textContent = "Todavía nada en este recorrido."; }
      else li.innerHTML = `<b>${esc(nombreClase(x.clase))}</b> ${Math.round(x.confianza * 100)}% <span>cuadro ${x.cuadro}</span>`;
      return li;
    }));
  }

  async function vista() {
    if (tabActiva !== "recorrido" || !elegido || document.hidden) return;
    try {
      const r = await fetch("/api/camara/vista?v=" + encodeURIComponent(elegido), { cache: "no-store" });
      if (!r.ok) return;
      const url = URL.createObjectURL(await r.blob());
      const nueva = new Image();                    // se precarga para no mostrar un cuadro a medias
      nueva.onload = () => {
        pImg.src = url; pImg.hidden = false; pEspera.hidden = true;
        if (urlVista) URL.revokeObjectURL(urlVista);
        urlVista = url;
      };
      nueva.onerror = () => URL.revokeObjectURL(url);
      nueva.src = url;
    } catch (_) { /* sin conexión */ }
  }

  async function vehiculos() {
    try {
      const r = await fetch("/api/camara/vehiculos", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (!j.ok) return;
      activos = j.vehiculos.sort((a, b) => (a.simulado - b.simulado) || a.vehiculo.localeCompare(b.vehiculo));
      if (!capaVeh) capaVeh = L.layerGroup().addTo(mapa);
      capaVeh.clearLayers();
      for (const v of activos) {
        // historial de lo detectado (se reinicia cuando el recorrido vuelve a empezar)
        if (ultimoCuadro[v.vehiculo] && v.cuadro && v.cuadro < ultimoCuadro[v.vehiculo]) historial[v.vehiculo] = [];
        if (v.cuadro && v.cuadro !== ultimoCuadro[v.vehiculo] && v.dets && v.dets.length) {
          (historial[v.vehiculo] = historial[v.vehiculo] || []).push(...v.dets.map(d => ({ ...d, cuadro: v.cuadro })));
        }
        if (v.cuadro) ultimoCuadro[v.vehiculo] = v.cuadro;
        // recorrido en el mapa
        if (v.ruta && v.ruta.length > 1) {
          L.polyline(v.ruta, { color: "#0b1220", weight: 8, opacity: .35, interactive: false }).addTo(capaVeh);
          L.polyline(v.ruta, { color: "#3cc8d4", weight: 4, opacity: .95, interactive: false, dashArray: v.simulado ? "1 7" : null,
            lineCap: "round" }).addTo(capaVeh);
          L.circleMarker(v.ruta[0], { radius: 5, color: "#3cc8d4", weight: 2, fillColor: "#0b1220", fillOpacity: 1, interactive: false })
            .addTo(capaVeh);
          let km = 0;
          for (let i = 1; i < v.ruta.length; i++) km += mapa.distance(v.ruta[i - 1], v.ruta[i]) / 1000;
          v.km = km;
        }
        const icono = L.divIcon({ className: "cam-veh", html: `<span class="cam-veh-punto"></span><span class="cam-veh-nombre"></span>`, iconSize: [0, 0] });
        const m = L.marker([v.lat, v.lng], { icon: icono, zIndexOffset: 1000, keyboard: false }).addTo(capaVeh);
        const el = m.getElement();
        if (el) el.querySelector(".cam-veh-nombre").textContent = v.simulado ? `${v.vehiculo} · simulado` : v.vehiculo;
        m.bindTooltip(`Cámara ${v.vehiculo} · clic para ver lo que está viendo`, { direction: "top" });
        m.on("click", () => { elegido = v.vehiculo; mostrarTab("recorrido"); pinta(); vista(); });
      }
      if (!activos.some(v => v.vehiculo === elegido)) elegido = activos[0] ? activos[0].vehiculo : null;
      pinta();
      if (pSeguir.checked && tabActiva === "recorrido") centra();
    } catch (_) { /* sin conexión */ }
  }

  detecciones(); vehiculos();
  setInterval(detecciones, 20000);
  setInterval(vehiculos, 3000);
  setInterval(vista, 2000);
})();
