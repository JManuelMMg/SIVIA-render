"""
Cámaras de los vehículos -> plataforma SIVIA.

Una cámara (Raspberry Pi en un camión, o la cámara simulada) corre el detector YOLO11s v9 y manda cada detección a
POST /api/camara/deteccion con su token de dispositivo. Aquí se:
  - valida el token del vehículo (CAMARA_TOKENS="GDL-01:token1,GDL-02:token2", solo en variables de entorno),
  - asocia la detección a la escuela, guardería u hospital más cercano (hasta 250 m),
  - evita duplicados: la misma clase a 15 m o menos en las últimas 12 h suma un avistamiento,
  - guarda la foto en el bucket (evidencias/camara/...) y la deja como candidato pendiente de validación.
El tablero la ve en la pestaña Validación IA y en el mapa; una persona la confirma o la descarta.
"""
import base64
import hmac
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import timedelta, timezone
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS camara_detecciones (
    id             bigserial PRIMARY KEY,
    candidato_id   text NOT NULL,
    vehiculo       text NOT NULL,
    fecha          timestamptz NOT NULL DEFAULT now(),
    ultima         timestamptz NOT NULL DEFAULT now(),
    lat            double precision NOT NULL,
    lng            double precision NOT NULL,
    clase          text NOT NULL,
    confianza      real NOT NULL,
    caja           text,
    archivo        text,
    institucion_id text,
    distancia_m    real,
    vistas         integer NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS camara_detecciones_fecha ON camara_detecciones (fecha DESC);
"""
CLASES = {"arbol_riesgo", "dano_pavimento", "alcantarilla_tapada", "alcantarilla_riesgo"}
LIMITES = (20.55, 20.80, -103.47, -103.22)          # caja amplia de Guadalajara (lat, lat, lng, lng)
MAX_FOTO = 900_000
RADIO_INSTITUCION_M = 250
RADIO_DUPLICADO_M = 15
GDL = timezone(timedelta(hours=-6))               # Guadalajara, sin horario de verano desde 2022


class ErrorCamara(Exception):
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


def tokens():
    pares = {}
    for parte in os.environ.get("CAMARA_TOKENS", "").split(","):
        veh, _, tok = parte.strip().partition(":")
        if veh and len(tok) >= 24:
            pares[tok] = veh
    return pares


def autentica(cabecera):
    """Devuelve el vehículo dueño del token 'Bearer <token>' o None."""
    tok = (cabecera or "").removeprefix("Bearer ").strip()
    if not tok:
        return None
    for t, veh in tokens().items():
        if hmac.compare_digest(t.encode(), tok.encode()):
            return veh
    return None


_ritmo, _ritmo_lock = {}, threading.Lock()


def ritmo_ok(vehiculo, por_minuto=240):
    ahora = time.time()
    with _ritmo_lock:
        q = _ritmo.setdefault(vehiculo, deque())
        while q and ahora - q[0] > 60:
            q.popleft()
        if len(q) >= por_minuto:
            return False
        q.append(ahora)
        return True


def metros(la1, ln1, la2, ln2):
    p = math.pi / 180
    a = (math.sin((la2 - la1) * p / 2) ** 2
         + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((ln2 - ln1) * p / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(a))


_inst, _inst_t = None, 0.0


def instituciones(c):
    global _inst, _inst_t
    if _inst is None or time.time() - _inst_t > 3600:
        _inst = c.execute("SELECT id, nombre, tipo, lat, lng, coalesce(accidentes_200m, 0) FROM instituciones").fetchall()
        _inst_t = time.time()
    return _inst


def cercana(c, la, ln):
    mejor = None
    for f in instituciones(c):
        if abs(f[3] - la) > 0.004 or abs(f[4] - ln) > 0.004:
            continue
        d = metros(la, ln, f[3], f[4])
        if d <= RADIO_INSTITUCION_M and (mejor is None or d < mejor[1]):
            mejor = (f, d)
    return mejor


_posiciones, _pos_lock = {}, threading.Lock()


MAX_VISTA = 250_000
MAX_TRAZA = 600
_vistas = {}          # vehículo -> JPEG del último cuadro (solo en memoria; no se guarda en la base ni en el bucket)
_trazas = {}          # vehículo -> puntos recorridos [(lat, lng), ...]


def _entero(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def posicion(vehiculo, datos):
    """Posición del vehículo y, si viene, la vista en vivo de la cámara (cuadro reducido con los recuadros)."""
    la, ln = float(datos.get("lat")), float(datos.get("lng"))
    if not (LIMITES[0] <= la <= LIMITES[1] and LIMITES[2] <= ln <= LIMITES[3]):
        raise ErrorCamara("posición fuera de Guadalajara")
    dets = []
    for d in (datos.get("dets") or [])[:20]:
        if isinstance(d, dict) and d.get("clase") in CLASES:
            dets.append({"clase": d["clase"], "confianza": round(float(d.get("confianza") or 0), 3)})
    vista = None
    if datos.get("vista_b64"):
        try:
            vista = base64.b64decode(datos["vista_b64"], validate=True)
        except (ValueError, TypeError):
            raise ErrorCamara("vista inválida")
        if len(vista) > MAX_VISTA or vista[:3] != b"\xff\xd8\xff":
            raise ErrorCamara("la vista debe ser JPEG de 250 KB o menos")
    with _pos_lock:
        # recorrido en vivo: los últimos puntos por donde pasó (se reinicia si estuvo más de 10 min sin señal)
        previa = _posiciones.get(vehiculo)
        traza = _trazas.setdefault(vehiculo, deque(maxlen=MAX_TRAZA))
        if previa and time.time() - previa["t"] > 600:
            traza.clear()
        if not traza or metros(traza[-1][0], traza[-1][1], la, ln) >= 5:
            traza.append((round(la, 6), round(ln, 6)))
        _posiciones[vehiculo] = {"vehiculo": vehiculo, "lat": la, "lng": ln, "t": time.time(),
                                 "cuadro": _entero(datos.get("cuadro")), "total": _entero(datos.get("total")),
                                 "ms": _entero(datos.get("ms")), "dets": dets, "vista": vista is not None}
        if vista:
            _vistas[vehiculo] = vista


def vista(vehiculo, max_seg=600):
    """JPEG del último cuadro de ese vehículo, si sigue activo."""
    with _pos_lock:
        p = _posiciones.get(vehiculo)
        if not p or time.time() - p["t"] > max_seg:
            return None
        return _vistas.get(vehiculo)


def vehiculos(max_seg=600):
    ahora = time.time()
    with _pos_lock:
        return [dict(v, hace_s=round(ahora - v["t"]), ruta=list(_trazas.get(v["vehiculo"], ())))
                for v in _posiciones.values() if ahora - v["t"] <= max_seg]


def registra(c, datos, vehiculo, cliente_s3, bucket):
    clase = str(datos.get("clase", ""))
    if clase not in CLASES:
        raise ErrorCamara("clase desconocida")
    try:
        la, ln, conf = float(datos["lat"]), float(datos["lng"]), float(datos["confianza"])
    except (KeyError, TypeError, ValueError):
        raise ErrorCamara("faltan lat, lng o confianza")
    if not (LIMITES[0] <= la <= LIMITES[1] and LIMITES[2] <= ln <= LIMITES[3]):
        raise ErrorCamara("posición fuera de Guadalajara")
    if not 0 < conf <= 1:
        raise ErrorCamara("confianza fuera de rango")
    caja = datos.get("caja")
    caja = json.dumps([round(float(v), 4) for v in caja][:4]) if isinstance(caja, list) else None

    # ¿ya se vio esta falla? (misma clase, 15 m, 12 h, aún sin revisar)
    previas = c.execute(
        "SELECT d.id, d.candidato_id, d.lat, d.lng FROM camara_detecciones d JOIN candidatos k ON k.id = d.candidato_id "
        "WHERE d.clase = %s AND d.ultima > now() - interval '12 hours' AND k.estado = 'pendiente' "
        "AND abs(d.lat - %s) < 0.0005 AND abs(d.lng - %s) < 0.0005", (clase, la, ln)).fetchall()
    for pid, cand, pla, pln in previas:
        if metros(la, ln, pla, pln) <= RADIO_DUPLICADO_M:
            vistas = c.execute("UPDATE camara_detecciones SET vistas = vistas + 1, ultima = now(), "
                               "confianza = greatest(confianza, %s) WHERE id = %s RETURNING vistas", (conf, pid)).fetchone()[0]
            c.execute("UPDATE candidatos SET confianza = greatest(confianza, %s) WHERE id = %s", (conf, cand))
            return {"id": cand, "vistas": vistas, "nuevo": False}

    inst = cercana(c, la, ln)
    inst_id = inst[0][0] if inst else None
    sufijo = uuid.uuid4().hex[:12]
    cand_id, archivo = f"cam_{sufijo}", f"cam_{sufijo}.jpg"
    llave = None
    foto = datos.get("imagen_b64")
    if foto:
        try:
            crudo = base64.b64decode(foto, validate=True)
        except (ValueError, TypeError):
            raise ErrorCamara("imagen inválida")
        if len(crudo) > MAX_FOTO or crudo[:3] != b"\xff\xd8\xff":
            raise ErrorCamara("la imagen debe ser JPEG de 900 KB o menos")
        if cliente_s3:
            llave = f"evidencias/camara/{time.strftime('%Y-%m-%d')}/{archivo}"
            cliente_s3.put_object(Bucket=bucket, Key=llave, Body=crudo, ContentType="image/jpeg")
    c.execute("INSERT INTO imagenes (archivo, institucion_id, fuente, fecha_captura, lat, lng, autor, bucket_key) "
              "VALUES (%s, %s, 'camara_propia', %s, %s, %s, %s, %s)",
              (archivo, inst_id, time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() - 6 * 3600)), la, ln, f"Cámara {vehiculo}", llave))
    c.execute("INSERT INTO detecciones (archivo, modelo, clase, confianza, fuente) VALUES (%s, %s, %s, %s, 'camara')",
              (archivo, str(datos.get("modelo", "yolo11s_v9"))[:40], clase, conf))
    c.execute("INSERT INTO candidatos (id, archivo, institucion_id, clase, confianza) VALUES (%s, %s, %s, %s, %s)",
              (cand_id, archivo, inst_id, clase, round(conf, 3)))
    c.execute("INSERT INTO camara_detecciones (candidato_id, vehiculo, lat, lng, clase, confianza, caja, archivo, "
              "institucion_id, distancia_m) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
              (cand_id, vehiculo, la, ln, clase, conf, caja, archivo if llave else None, inst_id,
               round(inst[1], 1) if inst else None))
    c.execute("INSERT INTO eventos (tabla, registro, estado, usuario, nota) VALUES ('camara', %s, 'deteccion', %s, %s)",
              (cand_id, vehiculo, clase))
    return {"id": cand_id, "vistas": 1, "nuevo": True, "institucion": inst[0][1] if inst else None}


_clases_info = None


def clases_info(web):
    """Categoría, dependencia y descripción de cada clase (las escribe agente/tablero_vivo.py)."""
    global _clases_info
    if _clases_info is None:
        ruta = Path(web) / "clases_ia.json"
        _clases_info = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {"clases": {}}
    return _clases_info


def lista(c, web, dias=30):
    """Detecciones de cámara con la forma de los hallazgos del tablero (DATOS)."""
    info = clases_info(web)
    filas = c.execute(
        "SELECT d.candidato_id, d.vehiculo, d.fecha, d.lat, d.lng, d.clase, d.confianza, d.archivo, d.vistas, "
        "k.estado, i.nombre, i.tipo, coalesce(i.accidentes_200m, 0), d.distancia_m "
        "FROM camara_detecciones d JOIN candidatos k ON k.id = d.candidato_id "
        "LEFT JOIN instituciones i ON i.id = d.institucion_id "
        "WHERE d.fecha > now() - (%s || ' days')::interval ORDER BY d.fecha DESC LIMIT 500", (str(int(dias)),)).fetchall()
    salida = []
    for (cid, veh, fecha, la, ln, clase, conf, archivo, vistas, estado, nom, tipo, acc, dist) in filas:
        ci = info.get("clases", {}).get(clase, {})
        lugar = f"a {round(dist)} m de {nom.title()}" if nom and dist is not None else "Vía pública"
        salida.append({
            "id": cid, "nombre": (nom or "Punto en vía pública").title()[:60], "tipo": tipo or "Vía pública",
            "categoria": ci.get("categoria", clase), "dependencia": ci.get("dependencia", ""), "severidad": "media",
            "descripcion": f"{ci.get('descripcion', clase)}. Cámara {veh}, {lugar}. Vista {vistas} "
                           f"{'vez' if vistas == 1 else 'veces'}.",
            "lat": la, "lon": ln, "prioridad_base": info.get("prioridad_base_candidato", 60),
            "accidentes_200m": acc, "confianza": round(conf, 2), "estado_revision": estado,
            "modelo_clase": clase, "fecha_captura": fecha.astimezone(GDL).strftime("%Y-%m-%d %H:%M"),
            "imagen": f"api/evidencia/{archivo}" if archivo else "", "vehiculo": veh, "vistas": vistas,
            "fuente": "camara"})
    return salida
