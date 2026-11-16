/* Vista 3D de la pantalla de inicio: el municipio de Guadalajara (límite INEGI) en relieve, dividido en zonas
   hexagonales de ~700 m. La altura de cada zona es el número de accidentes de tránsito 2022-2024; su color, el
   riesgo más alto de las escuelas, guarderías y hospitales que hay en ella. Al pasar el cursor se ve el detalle.
   Usa three.js. Si no hay WebGL o la persona pidió menos movimiento, se queda el mapa plano (SVG). */
(() => {
  const lienzo = document.getElementById("mapa3d");
  const plano = document.getElementById("mapa-plano");
  const ayuda = document.getElementById("mapa3d-ayuda");
  const info = document.getElementById("mapa3d-info");
  if (!lienzo || !window.THREE) return;
  const quieto = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let gl;
  try { gl = lienzo.getContext("webgl2") || lienzo.getContext("webgl"); } catch (_) { gl = null; }
  if (!gl) return;

  const T = window.THREE;
  const PALETAS = {
    oscuro: { 2: 0xff5d5d, 1: 0xf2b33d, 0: 0x6f93e8, "-1": 0x2a3550, losa: 0x0f1830, borde: 0x3cc8d4, lado: 0x0a1122 },
    claro: { 2: 0xd23a46, 1: 0xe09a1f, 0: 0x3a6fd1, "-1": 0xb9c5da, losa: 0xe4ebf6, borde: 0x0b7c89, lado: 0xc6d2e5 },
  };
  const NIVEL = { 2: "alto", 1: "medio", 0: "bajo", "-1": "sin instituciones" };
  const tema = () => (window.siviaTema ? window.siviaTema() : "oscuro");

  fetch("/publico/municipio3d.json").then(r => r.json()).then(datos => {
    const renderer = new T.WebGLRenderer({ canvas: lienzo, antialias: true, alpha: true, context: gl });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const escena = new T.Scene();
    const camara = new T.PerspectiveCamera(34, 1, 0.1, 100);
    escena.add(new T.HemisphereLight(0xffffff, 0x404a60, 0.85));
    const sol = new T.DirectionalLight(0xffffff, 0.7);
    sol.position.set(-2, 4, 3);
    escena.add(sol);

    // km -> unidades de escena, centrado en el municipio
    const E = 2.2 / Math.max(datos.ancho_km, datos.alto_km);
    const cx = datos.ancho_km / 2, cy = datos.alto_km / 2;
    const px = x => (x - cx) * E, pz = y => -(y - cy) * E;

    // losa del municipio con su contorno real
    const forma = new T.Shape();
    datos.contorno.forEach(([x, y], i) => (i ? forma.lineTo((x - cx) * E, (y - cy) * E) : forma.moveTo((x - cx) * E, (y - cy) * E)));
    const GROSOR = 0.06;
    const losaGeo = new T.ExtrudeGeometry(forma, { depth: GROSOR, bevelEnabled: false });
    losaGeo.rotateX(-Math.PI / 2);
    const losaMat = [new T.MeshLambertMaterial(), new T.MeshLambertMaterial()];
    const losa = new T.Mesh(losaGeo, losaMat);
    escena.add(losa);
    const bordeMat = new T.LineBasicMaterial({ transparent: true, opacity: 0.9 });
    const puntosBorde = datos.contorno.map(([x, y]) => new T.Vector3(px(x), GROSOR + 0.002, pz(y)));
    puntosBorde.push(puntosBorde[0].clone());
    escena.add(new T.Line(new T.BufferGeometry().setFromPoints(puntosBorde), bordeMat));

    // zonas hexagonales
    const zonas = datos.zonas;
    const maxAcc = Math.max(1, ...zonas.map(z => z[2]));
    const geo = new T.CylinderGeometry(datos.radio_km * E * 0.9, datos.radio_km * E * 0.9, 1, 6);
    geo.translate(0, 0.5, 0);
    const mat = new T.MeshLambertMaterial();
    const hex = new T.InstancedMesh(geo, mat, zonas.length);
    const base = zonas.map(([x, y, acc]) => ({
      x: px(x), z: pz(y), h: 0.012 + 0.34 * Math.sqrt(acc / maxAcc), d: Math.hypot(px(x), pz(y)),
    }));
    escena.add(hex);

    function colores() {
      const p = PALETAS[tema()] || PALETAS.oscuro;
      const c = new T.Color();
      zonas.forEach((z, i) => hex.setColorAt(i, c.setHex(p[String(z[4])])));
      hex.instanceColor.needsUpdate = true;
      losaMat[0].color.setHex(p.losa);
      losaMat[1].color.setHex(p.lado);
      bordeMat.color.setHex(p.borde);
      pide();
    }

    // las zonas suben del centro hacia afuera (un solo momento de animación)
    const m = new T.Matrix4(), esc = new T.Vector3(), lugar = new T.Vector3(), rot = new T.Quaternion();
    const inicio = performance.now();
    const facil = t => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t));
    function eleva(ahora) {
      let pendiente = false;
      base.forEach((b, i) => {
        const t = quieto ? 1 : Math.max(0, (ahora - inicio - 200 - b.d * 600) / 1000);
        if (t < 1) pendiente = true;
        lugar.set(b.x, GROSOR, b.z);
        esc.set(1, Math.max(0.0001, b.h * facil(t)), 1);
        hex.setMatrixAt(i, m.compose(lugar, rot, esc));
      });
      hex.instanceMatrix.needsUpdate = true;
      return pendiente;
    }

    // cámara orbital: arrastre para girar; sin rueda para no secuestrar el desplazamiento de la página
    let angulo = 0.5, inclinacion = 0.82, arrastrando = false, movio = false, ultimoX = 0, ultimoY = 0, ultimaMano = 0;
    const DIST = 3.95;
    function coloca() {
      camara.position.set(Math.sin(angulo) * Math.cos(inclinacion) * DIST, Math.sin(inclinacion) * DIST,
        Math.cos(angulo) * Math.cos(inclinacion) * DIST);
      camara.lookAt(0, 0.22, 0);
    }
    lienzo.addEventListener("pointerdown", e => { arrastrando = true; movio = false; ultimoX = e.clientX; ultimoY = e.clientY; lienzo.setPointerCapture(e.pointerId); });
    lienzo.addEventListener("pointermove", e => {
      if (arrastrando) {
        if (Math.abs(e.clientX - ultimoX) + Math.abs(e.clientY - ultimoY) > 1) movio = true;
        angulo -= (e.clientX - ultimoX) * 0.008;
        inclinacion = Math.min(1.35, Math.max(0.4, inclinacion + (e.clientY - ultimoY) * 0.005));
        ultimoX = e.clientX; ultimoY = e.clientY; ultimaMano = performance.now();
        if (info) info.hidden = true;
        pide();
      } else {
        apunta(e);
      }
    });
    const suelta = () => { arrastrando = false; };
    lienzo.addEventListener("pointerup", suelta);
    lienzo.addEventListener("pointercancel", suelta);
    lienzo.addEventListener("pointerleave", () => { if (info) info.hidden = true; marcada = -1; pide(); });

    // detalle de la zona bajo el cursor
    const rayo = new T.Raycaster(), cursor = new T.Vector2();
    let marcada = -1, ultimaConsulta = 0;
    function apunta(e) {
      if (!info || performance.now() - ultimaConsulta < 40) return;
      ultimaConsulta = performance.now();
      const r = lienzo.getBoundingClientRect();
      cursor.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      rayo.setFromCamera(cursor, camara);
      const toque = rayo.intersectObject(hex)[0];
      if (!toque) { info.hidden = true; marcada = -1; ultimaMano = 0; return; }
      const z = zonas[toque.instanceId];
      marcada = toque.instanceId;
      ultimaMano = performance.now();
      info.replaceChildren();
      const t = document.createElement("b");
      t.textContent = z[3] ? `Riesgo más alto: ${NIVEL[String(z[4])]}` : "Zona sin escuelas, guarderías ni hospitales";
      info.append(t, document.createTextNode(
        `${z[3]} ${z[3] === 1 ? "institución" : "instituciones"} · ${z[2]} accidentes 2022-2024`));
      info.style.left = Math.min(r.width - 230, Math.max(8, e.clientX - r.left + 14)) + "px";
      info.style.top = Math.max(8, e.clientY - r.top - 54) + "px";
      info.hidden = false;
    }

    function tamano() {
      const r = lienzo.getBoundingClientRect();
      if (!r.width) return;
      renderer.setSize(r.width, r.height, false);
      camara.aspect = r.width / r.height;
      camara.updateProjectionMatrix();
      pide();
    }
    new ResizeObserver(tamano).observe(lienzo);

    let visible = true, cuadro = null, previo = performance.now();
    new IntersectionObserver(([e]) => { visible = e.isIntersecting; pide(); }).observe(lienzo);
    document.addEventListener("visibilitychange", pide);
    document.addEventListener("sivia-tema", colores);
    function pide() { if (!cuadro) cuadro = requestAnimationFrame(dibuja); }
    function dibuja(ahora) {
      cuadro = null;
      const dt = Math.min(64, ahora - previo); previo = ahora;
      const subiendo = eleva(ahora);
      const giraSolo = !quieto && !arrastrando && marcada < 0 && ahora - ultimaMano > 2500;
      if (giraSolo) angulo += dt * 0.00008;
      coloca();
      renderer.render(escena, camara);
      if (visible && !document.hidden && (subiendo || giraSolo || arrastrando)) pide();
    }

    lienzo.hidden = false;
    if (plano) plano.hidden = true;
    if (ayuda) ayuda.hidden = false;
    colores();
    tamano();
    coloca();
    pide();
  }).catch(() => { /* sin datos: se queda el mapa plano */ });
})();
