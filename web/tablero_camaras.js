/* Tablero · detecciones en vivo de las cámaras de los vehículos.
   Cada 20 s trae de la plataforma las detecciones nuevas (modelo YOLO11s v9 corriendo en la cámara) y las suma a
   la pestaña Validación IA y al mapa como candidatos pendientes. También muestra dónde va cada vehículo. */
(() => {
  if (typeof DATOS === "undefined" || typeof mapa === "undefined") return;
  const vistos = new Set(DATOS.map(h => h.id));
  let capaVeh = null, primera = true;

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
          ? `Nueva detección de la cámara ${ultimo.vehiculo}: ${ultimo.modelo_clase} cerca de ${ultimo.nombre}. Revísala en Validación IA.`
          : `${nuevos} detecciones nuevas de las cámaras. Revísalas en Validación IA.`);
      }
    } catch (_) { /* sin conexión: se intenta en la siguiente vuelta */ }
    primera = false;
  }

  /* Panel "Cámara en vivo": lo que está viendo la cámara del vehículo, con los recuadros del modelo. */
  const panel = document.createElement("section");
  panel.className = "cam-vivo";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Cámara en vivo");
  panel.innerHTML = `
    <header><span class="cam-vivo-luz" aria-hidden="true"></span><b class="cam-vivo-titulo">Cámara en vivo</b>
      <button type="button" class="cam-vivo-cerrar" aria-label="Cerrar la vista de la cámara">×</button></header>
    <div class="cam-vivo-marco"><img hidden alt="Último cuadro que vio la cámara, con lo que detectó el modelo"><p class="cam-vivo-espera">Esperando el primer cuadro…</p></div>
    <p class="cam-vivo-estado"></p>
    <ul class="cam-vivo-dets"></ul>`;
  document.body.append(panel);
  const pImg = panel.querySelector("img"), pEspera = panel.querySelector(".cam-vivo-espera");
  const pTitulo = panel.querySelector(".cam-vivo-titulo"), pEstado = panel.querySelector(".cam-vivo-estado");
  const pDets = panel.querySelector(".cam-vivo-dets");
  let elegido = null, cerradoPor = null, urlVista = null, activos = [];
  panel.querySelector(".cam-vivo-cerrar").addEventListener("click", () => { cerradoPor = elegido; panel.hidden = true; });

  function abre(veh) { elegido = veh; cerradoPor = null; panel.hidden = false; pinta(); vista(); }

  function pinta() {
    const v = activos.find(x => x.vehiculo === elegido);
    if (!v) return;
    pTitulo.textContent = `Cámara ${v.vehiculo} · en vivo`;
    panel.classList.toggle("cam-vivo-quieta", v.hace_s > 15);
    const partes = [];
    if (v.cuadro) partes.push(v.total ? `Cuadro ${v.cuadro} de ${v.total}` : `Cuadro ${v.cuadro}`);
    if (v.ms) partes.push(`modelo ${v.ms} ms`);
    partes.push(v.hace_s <= 2 ? "ahora" : `hace ${v.hace_s} s`);
    pEstado.textContent = partes.join(" · ");
    pDets.replaceChildren();
    const lista = v.dets && v.dets.length ? v.dets : null;
    if (!lista) {
      const li = document.createElement("li"); li.className = "cam-vivo-nada";
      li.textContent = "Sin fallas en este cuadro"; pDets.append(li);
    } else for (const d of lista) {
      const li = document.createElement("li");
      li.textContent = `${d.clase.replace(/_/g, " ")} · ${Math.round(d.confianza * 100)}%`;
      pDets.append(li);
    }
  }

  async function vista() {
    if (panel.hidden || !elegido || document.hidden) return;
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
      activos = j.vehiculos;
      if (!capaVeh) capaVeh = L.layerGroup().addTo(mapa);
      capaVeh.clearLayers();
      for (const v of activos) {
        const icono = L.divIcon({ className: "cam-veh", html: `<span class="cam-veh-punto"></span><span class="cam-veh-nombre"></span>`, iconSize: [0, 0] });
        const m = L.marker([v.lat, v.lng], { icon: icono, zIndexOffset: 1000, keyboard: false }).addTo(capaVeh);
        const el = m.getElement();
        if (el) el.querySelector(".cam-veh-nombre").textContent = v.vehiculo;
        m.bindTooltip(`Cámara ${v.vehiculo} · clic para ver lo que está viendo`, { direction: "top" });
        m.on("click", () => abre(v.vehiculo));
      }
      // se abre sola con la primera cámara activa (salvo que la persona la haya cerrado)
      if (!activos.some(v => v.vehiculo === elegido)) elegido = null;
      if (!elegido && activos.length) {
        const v = activos.find(x => x.vista && x.hace_s < 60 && x.vehiculo !== cerradoPor);
        if (v) abre(v.vehiculo);
      }
      if (!activos.length) panel.hidden = true;
      pinta();
    } catch (_) { /* sin conexión */ }
  }

  detecciones(); vehiculos();
  setInterval(detecciones, 20000);
  setInterval(vehiculos, 3000);
  setInterval(vista, 2000);
})();
