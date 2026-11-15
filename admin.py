"""
Panel de administración de SIVIA: lógica de datos (la usa servidor.py).

Todo pasa por funciones que reciben una conexión abierta y el correo de quien actúa; servidor.py ya verificó que
la sesión es de un administrador activo. Cada cambio queda en la tabla eventos.

Reglas que protegen la operación:
  - Las contraseñas cumplen la misma política que crear_usuario.py (12+ caracteres, letras y números) y se
    guardan como hash PBKDF2; nunca se devuelven.
  - Un administrador no puede quitarse a sí mismo el rol ni darse de baja, y no se puede dejar la plataforma
    sin ningún administrador activo.
  - Solo se aceptan correos de los dominios permitidos (gobierno, Gmail o excepciones exactas).
"""
import csv
import io
import re

DEPENDENCIAS = ["Parques y Jardines", "Dirección de Pavimentos", "Dirección de Movilidad",
                "Alumbrado Público / CFE", "Agua y Drenaje (SIAPA)"]
ROLES = ["consulta", "dependencia", "operador", "admin"]


class ErrorAdmin(Exception):
    """Error de negocio: el mensaje se muestra tal cual al administrador."""

    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


def politica_clave(clave):
    if len(clave or "") < 12 or not re.search(r"[A-Za-z]", clave) or not re.search(r"\d", clave):
        raise ErrorAdmin("La contraseña necesita al menos 12 caracteres, con letras y números.")


def es_admin_activo(c, correo):
    f = c.execute("SELECT rol, activo FROM usuarios WHERE correo = %s", (correo,)).fetchone()
    return bool(f and f[0] == "admin" and f[1])


def _evento(c, registro, estado, actor, nota=None):
    c.execute("INSERT INTO eventos (tabla, registro, estado, usuario, nota) VALUES ('usuarios', %s, %s, %s, %s)",
              (registro, estado, actor, nota))


def _admins_activos(c):
    return c.execute("SELECT count(*) FROM usuarios WHERE rol = 'admin' AND activo").fetchone()[0]


# --- consultas ------------------------------------------------------------------------------------
def resumen(c):
    def pares(q):
        return {k: v for k, v in c.execute(q).fetchall()}
    usuarios = pares("SELECT rol, count(*) FROM usuarios WHERE activo GROUP BY rol")
    return {
        "usuarios_activos": sum(usuarios.values()),
        "usuarios_baja": c.execute("SELECT count(*) FROM usuarios WHERE NOT activo").fetchone()[0],
        "usuarios_por_rol": usuarios,
        "candidatos": pares("SELECT estado, count(*) FROM candidatos GROUP BY estado"),
        "ordenes": pares("SELECT estado, count(*) FROM ordenes GROUP BY estado"),
        "ordenes_por_dependencia": pares("SELECT dependencia, count(*) FROM ordenes GROUP BY dependencia"),
        "accesos_7d": pares("SELECT estado, count(*) FROM eventos WHERE tabla = 'acceso' "
                            "AND fecha > now() - interval '7 days' GROUP BY estado"),
        "cambios_7d": c.execute("SELECT count(*) FROM eventos WHERE tabla IN ('candidatos', 'ordenes') "
                                "AND fecha > now() - interval '7 days'").fetchone()[0],
    }


def usuarios(c):
    filas = c.execute(
        "SELECT u.correo, u.nombre, u.rol, u.dependencia, u.activo, u.creado_en, u.ultimo_acceso, "
        "(SELECT count(*) FROM eventos e WHERE e.usuario = u.correo AND e.tabla IN ('candidatos', 'ordenes')) "
        "FROM usuarios u ORDER BY u.activo DESC, u.rol, u.correo").fetchall()
    cols = ["correo", "nombre", "rol", "dependencia", "activo", "creado_en", "ultimo_acceso", "cambios"]
    return [dict(zip(cols, f)) for f in filas]


def eventos(c, tabla=None, usuario=None, texto=None, limite=200):
    cond, params = [], []
    if tabla:
        cond.append("tabla = %s")
        params.append(tabla)
    if usuario:
        cond.append("usuario = %s")
        params.append(usuario)
    if texto:
        cond.append("(registro ILIKE %s OR estado ILIKE %s OR coalesce(nota, '') ILIKE %s)")
        params += [f"%{texto}%"] * 3
    q = ("SELECT id, fecha, tabla, registro, estado, usuario, nota FROM eventos"
         + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY id DESC LIMIT %s")
    params.append(max(1, min(int(limite or 200), 2000)))
    cols = ["id", "fecha", "tabla", "registro", "estado", "usuario", "nota"]
    return [dict(zip(cols, f)) for f in c.execute(q, params).fetchall()]


def eventos_csv(c, **filtros):
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["id", "fecha", "tipo", "registro", "estado", "usuario", "nota"])
    for e in eventos(c, limite=2000, **filtros):
        w.writerow([e["id"], e["fecha"], e["tabla"], e["registro"], e["estado"], e["usuario"] or "", e["nota"] or ""])
    return chr(0xFEFF) + salida.getvalue()          # BOM para que Excel lea bien los acentos


# --- cambios --------------------------------------------------------------------------------------
def _valida_rol(rol, dependencia):
    if rol not in ROLES:
        raise ErrorAdmin("Rol inválido.")
    if rol == "dependencia" and dependencia not in DEPENDENCIAS:
        raise ErrorAdmin("El rol Dependencia necesita elegir la dependencia.")


def alta(c, datos, actor, correo_valido, hash_clave):
    correo = str(datos.get("correo", "")).strip().lower()
    nombre = str(datos.get("nombre", "")).strip()[:80]
    rol = str(datos.get("rol", ""))
    dep = datos.get("dependencia") or None
    clave = str(datos.get("clave", ""))
    if not correo_valido(correo):
        raise ErrorAdmin("Correo no permitido: usa uno institucional (.gob.mx), Gmail o una excepción autorizada.")
    if len(nombre) < 3:
        raise ErrorAdmin("Escribe el nombre de la persona.")
    _valida_rol(rol, dep)
    politica_clave(clave)
    if c.execute("SELECT 1 FROM usuarios WHERE correo = %s", (correo,)).fetchone():
        raise ErrorAdmin("Ya existe una cuenta con ese correo.", 409)
    c.execute("INSERT INTO usuarios (correo, nombre, dependencia, rol, hash) VALUES (%s, %s, %s, %s, %s)",
              (correo, nombre, dep if rol == "dependencia" else None, rol, hash_clave(clave)))
    _evento(c, correo, "alta:" + rol, actor, "panel")
    return {"correo": correo}


def actualizar(c, correo, datos, actor, hash_clave):
    correo = correo.strip().lower()
    fila = c.execute("SELECT rol, activo FROM usuarios WHERE correo = %s", (correo,)).fetchone()
    if not fila:
        raise ErrorAdmin("No existe esa cuenta.", 404)
    rol_actual, activo = fila
    accion = datos.get("accion")
    propio = correo == actor
    if accion == "rol":
        rol, dep = str(datos.get("rol", "")), datos.get("dependencia") or None
        _valida_rol(rol, dep)
        if propio and rol != "admin":
            raise ErrorAdmin("No puedes quitarte a ti mismo el rol de administrador.")
        if rol_actual == "admin" and rol != "admin" and activo and _admins_activos(c) <= 1:
            raise ErrorAdmin("Debe quedar al menos un administrador activo.")
        c.execute("UPDATE usuarios SET rol = %s, dependencia = %s WHERE correo = %s",
                  (rol, dep if rol == "dependencia" else None, correo))
        _evento(c, correo, "rol:" + rol, actor, "panel")
    elif accion == "nombre":
        nombre = str(datos.get("nombre", "")).strip()[:80]
        if len(nombre) < 3:
            raise ErrorAdmin("Escribe el nombre de la persona.")
        c.execute("UPDATE usuarios SET nombre = %s WHERE correo = %s", (nombre, correo))
        _evento(c, correo, "nombre", actor, "panel")
    elif accion in ("baja", "reactivar"):
        nuevo = accion == "reactivar"
        if not nuevo and propio:
            raise ErrorAdmin("No puedes darte de baja a ti mismo.")
        if not nuevo and rol_actual == "admin" and activo and _admins_activos(c) <= 1:
            raise ErrorAdmin("Debe quedar al menos un administrador activo.")
        c.execute("UPDATE usuarios SET activo = %s WHERE correo = %s", (nuevo, correo))
        _evento(c, correo, accion, actor, "panel")
    elif accion == "clave":
        clave = str(datos.get("clave", ""))
        politica_clave(clave)
        c.execute("UPDATE usuarios SET hash = %s WHERE correo = %s", (hash_clave(clave), correo))
        _evento(c, correo, "clave_restablecida", actor, "panel")
    else:
        raise ErrorAdmin("Acción desconocida.")
    return {"correo": correo, "accion": accion}


def cambiar_mi_clave(c, correo, actual, nueva, verifica_clave, hash_clave):
    fila = c.execute("SELECT hash, activo FROM usuarios WHERE correo = %s", (correo,)).fetchone()
    if not fila or not fila[1]:
        raise ErrorAdmin("Tu cuenta no está activa.", 403)
    if not verifica_clave(actual, fila[0]):
        raise ErrorAdmin("La contraseña actual no es correcta.", 403)
    if actual == nueva:
        raise ErrorAdmin("La contraseña nueva debe ser distinta de la actual.")
    politica_clave(nueva)
    c.execute("UPDATE usuarios SET hash = %s WHERE correo = %s", (hash_clave(nueva), correo))
    _evento(c, correo, "clave_cambiada", correo, "mi cuenta")
