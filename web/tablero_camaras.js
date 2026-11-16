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

  async function vehiculos() {
    try {
      const r = await fetch("/api/camara/vehiculos", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (!j.ok) return;
      if (!capaVeh) capaVeh = L.layerGroup().addTo(mapa);
      capaVeh.clearLayers();
      for (const v of j.vehiculos) {
        const icono = L.divIcon({ className: "cam-veh", html: `<span class="cam-veh-punto"></span><span class="cam-veh-nombre"></span>`, iconSize: [0, 0] });
        const m = L.marker([v.lat, v.lng], { icon: icono, zIndexOffset: 1000, keyboard: false }).addTo(capaVeh);
        const el = m.getElement();
        if (el) el.querySelector(".cam-veh-nombre").textContent = v.vehiculo;
        m.bindTooltip(`Cámara ${v.vehiculo} · hace ${v.hace_s} s`, { direction: "top" });
      }
    } catch (_) { /* sin conexión */ }
  }

  detecciones(); vehiculos();
  setInterval(detecciones, 20000);
  setInterval(vehiculos, 5000);
})();
