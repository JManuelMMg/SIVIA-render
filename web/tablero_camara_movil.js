/* Tablero · "Mi cámara": recorrido con la cámara y el GPS de este celular o laptop.
   El detector YOLO11s v9 (ONNX) corre aquí mismo con onnxruntime-web; cada falla se manda a la plataforma con su
   foto y su posición (como la cámara de un vehículo) y llega a Validación IA como candidato pendiente.
   La plataforma acepta estos envíos solo con sesión de operador o admin; el vehículo se llama MOVIL-<usuario>. */
(() => {
  const pane = document.getElementById("pane-recorrido");
  if (!pane) return;
  const ORT = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/";
  const CLASES = ["alcantarilla_riesgo", "alcantarilla_tapada", "arbol_riesgo", "dano_pavimento"];
  const UMBRAL = { alcantarilla_riesgo: 0.5, alcantarilla_tapada: 0.7, arbol_riesgo: 0.5, dano_pavimento: 0.5 };
  const COLOR = { alcantarilla_riesgo: "#ff5d5d", alcantarilla_tapada: "#3cc8d4", arbol_riesgo: "#ff5d5d",
                  dano_pavimento: "#f2b33d" };
  const N = 640, MINIMO = 0.25, IOU = 0.5, PAUSA_MS = 1200;
  const LIMITES = [20.55, 20.80, -103.47, -103.22];        // los mismos que valida la plataforma

  const caja = document.createElement("div");
  caja.className = "cm-caja";
  caja.innerHTML = `
    <div class="cm-cab">
      <h3>Mi cámara</h3>
      <p>Haz un recorrido con la cámara y el GPS de este celular o laptop. El modelo YOLO11s v9 corre aquí mismo y
      cada falla que encuentra llega a Validación IA con su foto y su ubicación.</p>
    </div>
    <div class="cm-acciones">
      <button type="button" class="rec-btn" data-cm-iniciar>Iniciar recorrido con mi cámara</button>
      <button type="button" class="cm-sec" data-cm-detener hidden>Detener</button>
      <label class="rec-seguir"><input type="checkbox" data-cm-enviar checked> Enviar detecciones a la plataforma</label>
    </div>
    <div class="cm-vista" hidden><video playsinline muted></video><canvas aria-hidden="true"></canvas></div>
    <p class="cm-estado" role="status" aria-live="polite"></p>`;
  pane.querySelector(".intro").after(caja);
  const $ = s => caja.querySelector(s);
  const video = $("video"), capa = $(".cm-vista canvas"), estado = $(".cm-estado"), vistaEl = $(".cm-vista");
  const bIni = $("[data-cm-iniciar]"), bDet = $("[data-cm-detener]"), chkEnviar = $("[data-cm-enviar]");

  let sesion = null, corriendo = false, flujo = null, vigia = null, gps = null, cuadro = 0, enviadas = 0;
  const ultimoEnvio = {};                    // clase -> {t, lat, lng}: no repetir la misma falla cada segundo
  const di = t => (estado.textContent = t);

  function cargaScript(src) {
    return new Promise((ok, mal) => {
      if (window.ort) return ok();
      const s = document.createElement("script");
      s.src = src; s.onload = ok; s.onerror = () => mal(new Error("no se pudo cargar onnxruntime-web"));
      document.head.append(s);
    });
  }

  async function cargaModelo() {
    if (sesion) return sesion;
    di("Cargando el motor del modelo…");
    await cargaScript(ORT + "ort.min.js");
    window.ort.env.wasm.wasmPaths = ORT;
    di("Descargando el modelo YOLO11s v9 (38 MB, solo la primera vez)…");
    const r = await fetch("/api/camara/modelo.onnx");
    if (r.status === 401) { location.href = "/login"; throw new Error("inicia sesión"); }
    if (!r.ok) throw new Error("el modelo no está disponible en este despliegue");
    const bytes = new Uint8Array(await r.arrayBuffer());
    di("Preparando el modelo…");
    sesion = await window.ort.InferenceSession.create(bytes, { executionProviders: ["wasm"] });
    return sesion;
  }

  // --- detector: letterbox 640, CHW 0-1, salida [1, 8, 8400], NMS por clase (igual que agente/inspector.py) -----
  const lienzo = document.createElement("canvas");
  lienzo.width = lienzo.height = N;
  const ctxL = lienzo.getContext("2d", { willReadFrequently: true });
  async function detecta(fuente, W, H) {
    const s = N / Math.max(W, H), nw = Math.round(W * s), nh = Math.round(H * s);
    const ox = Math.floor((N - nw) / 2), oy = Math.floor((N - nh) / 2);
    ctxL.fillStyle = "rgb(114,114,114)"; ctxL.fillRect(0, 0, N, N);
    ctxL.drawImage(fuente, ox, oy, nw, nh);
    const px = ctxL.getImageData(0, 0, N, N).data, area = N * N, x = new Float32Array(3 * area);
    for (let i = 0; i < area; i++) {
      x[i] = px[i * 4] / 255; x[i + area] = px[i * 4 + 1] / 255; x[i + 2 * area] = px[i * 4 + 2] / 255;
    }
    const salida = await sesion.run({ [sesion.inputNames[0]]: new window.ort.Tensor("float32", x, [1, 3, N, N]) });
    const o = salida[sesion.outputNames[0]].data, A = 8400, cand = [];
    for (let i = 0; i < A; i++) {
      let c = 0, p = o[4 * A + i];
      for (let k = 1; k < CLASES.length; k++) if (o[(4 + k) * A + i] > p) { p = o[(4 + k) * A + i]; c = k; }
      if (p < MINIMO) continue;
      const cx = o[i], cy = o[A + i], w = o[2 * A + i], h = o[3 * A + i];
      cand.push({ c, p, x0: cx - w / 2, y0: cy - h / 2, x1: cx + w / 2, y1: cy + h / 2 });
    }
    cand.sort((a, b) => b.p - a.p);
    const res = [];
    for (const a of cand) {
      if (res.some(b => b.c === a.c && iou(a, b) >= IOU)) continue;
      res.push(a);
    }
    return res.map(a => {
      const x0 = Math.max(0, (a.x0 - ox) / s), y0 = Math.max(0, (a.y0 - oy) / s);
      const x1 = Math.min(W, (a.x1 - ox) / s), y1 = Math.min(H, (a.y1 - oy) / s);
      return { k: CLASES[a.c], s: a.p, caja: [x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H] };
    }).filter(d => d.s >= UMBRAL[d.k] && (d.k !== "dano_pavimento" || d.caja[1] + d.caja[3] / 2 > 0.4));
  }
  function iou(a, b) {
    const iw = Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)), ih = Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
    const inter = iw * ih, u = (a.x1 - a.x0) * (a.y1 - a.y0) + (b.x1 - b.x0) * (b.y1 - b.y0) - inter;
    return inter / Math.max(u, 1e-6);
  }

  // --- dibujo de recuadros y fotos JPEG ------------------------------------------------------------------------
  function dibuja(ctx, W, H, dets) {
    ctx.lineWidth = Math.max(3, W / 300);
    ctx.font = `${Math.max(12, Math.round(W / 55))}px sans-serif`;
    for (const d of dets) {
      const [x, y, w, h] = d.caja, col = COLOR[d.k] || "#3cc8d4", t = `${d.k} ${Math.round(d.s * 100)}%`;
      ctx.strokeStyle = col; ctx.strokeRect(x * W, y * H, w * W, h * H);
      const tw = ctx.measureText(t).width + 10, th = parseInt(ctx.font, 10) + 8;
      ctx.fillStyle = col; ctx.fillRect(x * W, Math.max(0, y * H - th), tw, th);
      ctx.fillStyle = "#000"; ctx.fillText(t, x * W + 5, Math.max(0, y * H - th) + th - 6);
    }
  }
  function jpeg(fuente, W, H, ancho, calidad, dets) {
    const c = document.createElement("canvas"), e = Math.min(1, ancho / W);
    c.width = Math.round(W * e); c.height = Math.round(H * e);
    const ctx = c.getContext("2d");
    ctx.drawImage(fuente, 0, 0, c.width, c.height);
    dibuja(ctx, c.width, c.height, dets);
    return c.toDataURL("image/jpeg", calidad).split(",")[1];
  }

  async function envia(ruta, datos) {
    const r = await fetch(ruta, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(datos) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || `error ${r.status}`);
    return j;
  }
  const dentro = p => p && p.lat >= LIMITES[0] && p.lat <= LIMITES[1] && p.lng >= LIMITES[2] && p.lng <= LIMITES[3];
  function metros(a, b) {
    const r = Math.PI / 180, x = (b.lng - a.lng) * r * Math.cos((a.lat + b.lat) * r / 2), y = (b.lat - a.lat) * r;
    return 6371000 * Math.hypot(x, y);
  }

  // --- ciclo: un cuadro, el modelo, los envíos, una pausa --------------------------------------------------------
  async function ciclo() {
    if (!corriendo) return;
    if (document.hidden || video.readyState < 2) { setTimeout(ciclo, 800); return; }
    const W = video.videoWidth, H = video.videoHeight;
    const t0 = performance.now();
    let dets = [];
    try { dets = await detecta(video, W, H); } catch (e) { di("Error del modelo: " + e.message); }
    const ms = Math.round(performance.now() - t0);
    cuadro++;
    // recuadros sobre el video
    capa.width = W; capa.height = H;
    const cx = capa.getContext("2d");
    cx.clearRect(0, 0, W, H);
    dibuja(cx, W, H, dets);
    const pos = gps && { lat: gps.coords.latitude, lng: gps.coords.longitude, prec: Math.round(gps.coords.accuracy) };
    const partes = [`Cuadro ${cuadro}`, `modelo ${ms} ms`, dets.length ? `${dets.length} falla(s) en este cuadro` : "sin fallas"];
    partes.push(pos ? `GPS ±${pos.prec} m` : "esperando GPS…");
    let aviso = "";
    if (chkEnviar.checked && pos && !dentro(pos)) aviso = " · Fuera de Guadalajara: el modelo corre, pero no se envía nada.";
    if (chkEnviar.checked && dentro(pos)) {
      try {
        await envia("/api/camara/posicion", { lat: pos.lat, lng: pos.lng, cuadro, total: 0, ms,
          dets: dets.map(d => ({ clase: d.k, confianza: +d.s.toFixed(3) })), vista_b64: jpeg(video, W, H, 640, 0.62, dets) });
        for (const d of dets) {
          const u = ultimoEnvio[d.k];
          if (u && Date.now() - u.t < 15000 && metros(u, pos) < 15) continue;   // misma falla, mismo lugar
          await envia("/api/camara/deteccion", { clase: d.k, confianza: +d.s.toFixed(3), caja: d.caja.map(v => +v.toFixed(4)),
            lat: pos.lat, lng: pos.lng, modelo: "yolo11s_v9_web", imagen_b64: jpeg(video, W, H, 1280, 0.76, [d]) });
          ultimoEnvio[d.k] = { t: Date.now(), lat: pos.lat, lng: pos.lng };
          enviadas++;
        }
      } catch (e) {
        aviso = ` · No se pudo enviar: ${e.message}.`;
        if (/sesión/.test(e.message)) { detener(); location.href = "/login"; return; }
      }
    }
    if (enviadas) partes.push(`${enviadas} enviada(s) a Validación IA`);
    di(partes.join(" · ") + aviso);
    setTimeout(ciclo, PAUSA_MS);
  }

  async function iniciar() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      di("Este navegador no permite usar la cámara (se necesita HTTPS y un navegador actual)."); return;
    }
    bIni.disabled = true;
    try {
      flujo = await navigator.mediaDevices.getUserMedia({ audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } } });
    } catch (e) {
      bIni.disabled = false; di("No se pudo abrir la cámara: " + (e.name === "NotAllowedError" ? "permiso negado." : e.message)); return;
    }
    video.srcObject = flujo; vistaEl.hidden = false;
    await video.play().catch(() => {});
    if (navigator.geolocation) {
      vigia = navigator.geolocation.watchPosition(p => (gps = p), e => di("GPS: " + e.message),
        { enableHighAccuracy: true, maximumAge: 2000, timeout: 15000 });
    }
    try { await cargaModelo(); } catch (e) { di("No se pudo cargar el modelo: " + e.message); detener(); return; }
    corriendo = true; cuadro = 0; enviadas = 0;
    bIni.hidden = true; bIni.disabled = false; bDet.hidden = false;
    di("Recorrido iniciado. Apunta la cámara a la calle.");
    ciclo();
  }
  function detener() {
    corriendo = false;
    if (flujo) flujo.getTracks().forEach(t => t.stop());
    if (vigia !== null && navigator.geolocation) navigator.geolocation.clearWatch(vigia);
    flujo = null; vigia = null; vistaEl.hidden = true;
    bIni.hidden = false; bIni.disabled = false; bDet.hidden = true;
    di(cuadro ? `Recorrido detenido: ${cuadro} cuadros, ${enviadas} detección(es) enviadas.` : "");
  }
  bIni.addEventListener("click", iniciar);
  bDet.addEventListener("click", detener);
  window.addEventListener("pagehide", detener);

  // Para probar sin cámara: siviaCamaraPrueba(img) corre el modelo sobre una imagen y devuelve las detecciones.
  window.siviaCamaraPrueba = async img => { await cargaModelo(); return detecta(img, img.naturalWidth, img.naturalHeight); };
})();
