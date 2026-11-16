"""
Órdenes de trabajo que nacen de la validación.

Cuando una persona confirma un candidato del modelo (fotos de calle o cámara de un vehículo), llena un formulario
con lo que hay que arreglar y la orden se turna a la dependencia que corresponde. La dependencia la ve en su
bandeja (pestaña Órdenes) y la mueve: asignada -> en atención -> resuelta (o descartada).
"""
import json
import re
from pathlib import Path

ESQUEMA = """
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS candidato_id text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS folio        text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS severidad    text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS plazo_dias   integer;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS accion       text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS ubicacion    text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS notas        text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS creada_por   text;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS lat          double precision;
ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS lng          double precision;
CREATE UNIQUE INDEX IF NOT EXISTS ordenes_candidato ON ordenes (candidato_id) WHERE candidato_id IS NOT NULL;
CREATE SEQUENCE IF NOT EXISTS ordenes_folio;
"""
SEVERIDADES = {"urgente": 2, "alta": 5, "media": 15, "baja": 30}          # plazo sugerido en días
ESTADOS = ("asignada", "en_atencion", "resuelta", "descartada")
ID_OK = re.compile(r"^[A-Za-z0-9_:.\-]{1,120}$")


class ErrorOrden(Exception):
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


def _texto(v, maximo, obligatorio=False, nombre="campo"):
    t = " ".join(str(v or "").split())[:maximo]
    if obligatorio and not t:
        raise ErrorOrden(f"falta {nombre}")
    return t


def dependencias(c, web):
    """Dependencias válidas: las que ya tienen órdenes y las que asigna el modelo a cada clase."""
    deps = {f[0] for f in c.execute("SELECT DISTINCT dependencia FROM ordenes").fetchall() if f[0]}
    ruta = Path(web) / "clases_ia.json"
    if ruta.exists():
        info = json.loads(ruta.read_text(encoding="utf-8"))
        deps |= {v.get("dependencia") for v in info.get("clases", {}).values() if v.get("dependencia")}
    return sorted(deps)


def crear(c, datos, usuario, web):
    """Confirma el candidato y crea su orden turnada a la dependencia. Devuelve folio e id."""
    cand = str(datos.get("candidato_id", ""))
    if not ID_OK.match(cand):
        raise ErrorOrden("candidato inválido")
    fila = c.execute("SELECT k.clase, k.institucion_id, k.archivo, k.confianza, im.lat, im.lng FROM candidatos k "
                     "LEFT JOIN imagenes im ON im.archivo = k.archivo WHERE k.id = %s", (cand,)).fetchone()
    if not fila:
        raise ErrorOrden("no encontré ese candidato", 404)
    clase, inst, archivo, conf, la, ln = fila
    previa = c.execute("SELECT folio, dependencia FROM ordenes WHERE candidato_id = %s", (cand,)).fetchone()
    if previa:
        raise ErrorOrden(f"ese hallazgo ya tiene la orden {previa[0]} turnada a {previa[1]}", 409)

    dep = _texto(datos.get("dependencia"), 80, True, "la dependencia")
    if dep not in dependencias(c, web):
        raise ErrorOrden("dependencia desconocida")
    sev = str(datos.get("severidad", "")).lower()
    if sev not in SEVERIDADES:
        raise ErrorOrden("severidad inválida")
    try:
        plazo = int(datos.get("plazo_dias") or SEVERIDADES[sev])
    except (TypeError, ValueError):
        raise ErrorOrden("plazo inválido")
    if not 1 <= plazo <= 180:
        raise ErrorOrden("el plazo debe ser de 1 a 180 días")
    hallazgo = _texto(datos.get("hallazgo"), 120, True, "el tipo de falla")
    descripcion = _texto(datos.get("descripcion"), 600, True, "la descripción")
    accion = _texto(datos.get("accion"), 300, True, "la acción solicitada")
    ubicacion = _texto(datos.get("ubicacion"), 200)
    notas = _texto(datos.get("notas"), 600)
    try:
        prioridad = max(0.0, min(500.0, float(datos.get("prioridad") or 0)))
    except (TypeError, ValueError):
        prioridad = 0.0
    if not la:
        try:
            la, ln = float(datos.get("lat")), float(datos.get("lng"))
        except (TypeError, ValueError):
            la = ln = None

    n = c.execute("SELECT nextval('ordenes_folio')").fetchone()[0]
    folio = f"OT-{n:05d}"
    oid = f"val:{cand}"
    c.execute("UPDATE candidatos SET estado = 'confirmado', revisado_en = now(), revisado_por = %s WHERE id = %s",
              (usuario, cand))
    c.execute("INSERT INTO ordenes (id, institucion_id, clave, hallazgo, dependencia, prioridad_base, factor_clima, "
              "prioridad, detecciones, imagen, explicacion, estado, candidato_id, folio, severidad, plazo_dias, accion, "
              "ubicacion, notas, creada_por, lat, lng) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, 1, %s, %s, 'asignada', "
              "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
              (oid, inst, clase, hallazgo, dep, prioridad, prioridad, archivo, descripcion, cand, folio, sev, plazo,
               accion, ubicacion, notas or None, usuario, la, ln))
    c.execute("INSERT INTO eventos (tabla, registro, estado, usuario, nota) VALUES ('candidatos', %s, 'confirmado', %s, %s)",
              (cand, usuario, f"orden {folio}"))
    c.execute("INSERT INTO eventos (tabla, registro, estado, usuario, nota) VALUES ('ordenes', %s, 'asignada', %s, %s)",
              (oid, usuario, f"{folio} turnada a {dep}"))
    return {"id": oid, "folio": folio, "dependencia": dep, "plazo_dias": plazo}


def lista(c, dependencia=None):
    """Órdenes creadas desde la validación (las más recientes primero)."""
    q = ("SELECT o.id, o.folio, o.candidato_id, o.hallazgo, o.dependencia, o.severidad, o.plazo_dias, o.accion, "
         "o.ubicacion, o.notas, o.explicacion, o.estado, o.prioridad, o.creada_por, o.creada_en, o.actualizada_en, "
         "i.nombre, coalesce(o.lat, i.lat), coalesce(o.lng, i.lng), "
         "(o.creada_en + make_interval(days => coalesce(o.plazo_dias, 15))) < now() AND o.estado IN ('asignada', 'en_atencion') "
         "FROM ordenes o LEFT JOIN instituciones i ON i.id = o.institucion_id WHERE o.candidato_id IS NOT NULL")
    filas = c.execute(q + (" AND o.dependencia = %s" if dependencia else "") + " ORDER BY o.creada_en DESC LIMIT 300",
                      (dependencia,) if dependencia else ()).fetchall()
    cols = ["id", "folio", "candidato_id", "hallazgo", "dependencia", "severidad", "plazo_dias", "accion", "ubicacion",
            "notas", "descripcion", "estado", "prioridad", "creada_por", "creada_en", "actualizada_en", "institucion",
            "lat", "lng", "vencida"]
    salida = []
    for f in filas:
        d = dict(zip(cols, f))
        for k in ("creada_en", "actualizada_en"):
            d[k] = d[k].isoformat() if d[k] else None
        salida.append(d)
    return salida
