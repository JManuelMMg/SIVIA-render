#!/usr/bin/env python3
"""
Servidor del tablero en vivo con base de datos y acceso restringido a personal de gobierno.

Extiende smn_server.py (clima SMN, GOES, radar, INEGI, SSN, USGS; sin cambios) con la plataforma:
  GET  /login, POST /login, GET /salir   inicio y cierre de sesión
  GET  /salud                          verificación para el hospedaje (no expone datos)
  GET  /api/sesion                     quién está conectado y con qué rol
  GET  /admin, /api/admin/...          panel del administrador: resumen, usuarios, bitácora, bloqueos (solo admin)
  GET  /cuenta, POST /api/cuenta/clave cada usuario cambia su propia contraseña
  GET  /api/bd/estado                  conexión y conteos
  GET  /api/candidatos                 {id: estado} de los candidatos del modelo
  POST /api/candidatos/<id>            {"estado": "confirmado"|"descartado"|"pendiente"}      (operador, admin)
  GET  /api/ordenes?dependencia=...    órdenes con su estado (el rol "dependencia" ve solo las suyas)
  POST /api/ordenes/<id>               {"estado": "asignada"|"en_atencion"|"resuelta"|"descartada"|"pendiente"}
  GET  /api/evidencia/<archivo>        redirige a una liga firmada del bucket (si está configurado)

Seguridad (se activa al definir SESION_SECRETO; en Render es obligatorio):
  - La pantalla de inicio (/) es pública; el tablero (/tablero) y los datos exigen sesión. Solo entran cuentas con
    correo de un dominio de gobierno o de Gmail
    (GOB_DOMINIOS, por defecto guadalajara.gob.mx,jalisco.gob.mx,gob.mx) dadas de alta por un administrador
    con plataforma/crear_usuario.py. No hay registro abierto.
  - Contraseñas con PBKDF2-SHA256 (310,000 iteraciones y sal propia); sesión firmada con HMAC en cookie
    HttpOnly, SameSite=Strict y Secure, que vence a las 8 horas.
  - Roles: consulta (solo ver), dependencia (ve y actualiza las órdenes de su dependencia), operador
    (valida candidatos y actualiza cualquier orden) y admin.
  - Tras 5 intentos fallidos el correo y la IP quedan bloqueados 15 minutos. Cada acceso y cada cambio
    queda en la bitácora (tabla eventos) con el correo de quien lo hizo.
  - Solo se sirven index.html e img/; el código y los scripts de la carpeta no son descargables.

Credenciales SOLO por variables de entorno, nunca en archivos:
  DATABASE_URL, SESION_SECRETO, GOB_DOMINIOS, DOMINIOS_PERSONALES (por defecto gmail.com), CORREOS_AUTORIZADOS (excepciones: correos exactos separados por coma)
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3, AWS_REGION, BUCKET (por defecto "reportbucket")
Sin SESION_SECRETO y en 127.0.0.1 funciona como antes, para uso local (sin login).

Uso: python servidor.py [--host 127.0.0.1] [--port 8000] [--no-browser]
     En Render: python servidor.py --host 0.0.0.0 --port $PORT --no-browser
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smn_server  # noqa: E402
import admin  # noqa: E402

DB = os.environ.get("DATABASE_URL")
BUCKET = os.environ.get("BUCKET", "reportbucket")
SECRETO = os.environ.get("SESION_SECRETO", "")
DOMINIOS = [d.strip().lower().lstrip("@") for d in
            os.environ.get("GOB_DOMINIOS", "guadalajara.gob.mx,jalisco.gob.mx,gob.mx").split(",") if d.strip()]
# Excepciones puntuales (p. ej. el equipo que desarrolla o un asesor externo): correos EXACTOS, nunca dominios.
# Dominios personales aceptados (solo para cuentas que el administrador da de alta; no hay registro abierto).
PERSONALES = [d.strip().lower().lstrip("@") for d in os.environ.get("DOMINIOS_PERSONALES", "gmail.com").split(",") if d.strip()]
EXCEPCIONES = {c.strip().lower() for c in os.environ.get("CORREOS_AUTORIZADOS", "").split(",") if c.strip()}
HTTPS = os.environ.get("RENDER") is not None or os.environ.get("FORZAR_HTTPS") == "1"
DURACION = 8 * 3600
EST_CAND = {"pendiente", "confirmado", "descartado"}
EST_ORD = {"pendiente", "asignada", "en_atencion", "resuelta", "descartada"}
ROLES = {"consulta", "dependencia", "operador", "admin"}
ID_OK = re.compile(r"^[A-Za-z0-9_:.\-]{1,120}$")
CORREO_OK = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
ITER = 310_000

_fallos = {}            # clave -> [intentos, bloqueado_hasta]
_candado = threading.Lock()


# --- memoria: el plan gratuito de Render tiene 512 MB ------------------------------------------
# smn_server consulta fuentes externas con caché, pero si llegan varias peticiones antes de que la primera
# termine, cada una descarga por su cuenta. Con los límites de INEGI (11 MB de JSON, ~70 MB en memoria por
# descarga) eso rebasa los 512 MB. Aquí cada fuente se descarga una sola vez a la vez y los límites se guardan
# ya serializados y comprimidos (~2 MB) en lugar del objeto completo.
def _una_a_la_vez(funcion):
    candado = threading.Lock()

    def envoltura(*a, **k):
        with candado:
            return funcion(*a, **k)
    return envoltura


def fetch_smn_ligero():
    """Igual que smn_server.fetch_smn, pero sin cargar el archivo nacional completo en memoria.

    El SMN (method=3) publica el pronóstico por hora de los ~2,400 municipios del país en un solo archivo
    comprimido; leerlo entero con json.loads ocupa ~600 MB. Aquí se descomprime por bloques y solo se
    decodifica cada registro {...} que menciona Guadalajara; la memoria se queda en unos pocos MB."""
    import zlib
    from datetime import datetime, timezone
    import urllib.request
    req = urllib.request.Request(smn_server.SMN_URL, headers={
        "User-Agent": "EntornoSeguroGDL/1.0 (SMN municipal forecast integration)",
        "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate"})
    filas, resto, dec = [], b"", None
    with urllib.request.urlopen(req, timeout=60) as resp:
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        while True:
            bloque = resp.read(1 << 20)
            if dec is None and bloque:
                if "gzip" in enc or bloque[:2] == b"\x1f\x8b":
                    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                elif "deflate" in enc or bloque[:1] not in (b"{", b"[", b" ", b"\n", b"\r", b"\xef"):
                    dec = zlib.decompressobj()
                else:
                    dec = False
            texto = (dec.decompress(bloque) if dec else bloque) if bloque else (dec.flush() if dec else b"")
            buf = resto + texto
            ini = 0
            while True:                         # registros planos {...}: sin llaves anidadas
                a = buf.find(b"{", ini)
                if a < 0:
                    ini = len(buf)
                    break
                b = buf.find(b"}", a)
                if b < 0:
                    ini = a
                    break
                obj = buf[a:b + 1]
                if b"uadalajara" in obj or b"UADALAJARA" in obj:
                    try:
                        filas.append(json.loads(obj.decode("utf-8-sig", "replace")))
                    except ValueError:
                        pass
                ini = b + 1
            resto = buf[ini:]
            if not bloque:
                break
    hallados = [f for f in filas
                if smn_server.normalize(f.get("nmun") or f.get("municipio")) == smn_server.MUNICIPALITY_NAME
                and smn_server.normalize(f.get("nes") or f.get("estado")) == smn_server.STATE_NAME]
    if not hallados:
        raise ValueError("El SMN respondió, pero no se encontró Guadalajara, Jalisco en method=3")
    registros = sorted((smn_server.normalize_record(f) for f in hallados), key=lambda r: r["fecha_hora"] or "")
    return {"ok": True, "fuente": "Servicio Meteorológico Nacional / CONAGUA", "url_fuente": smn_server.SMN_URL,
            "estado": "Jalisco", "municipio": "Guadalajara", "periodo": "Pronóstico municipal por hora, hasta 48 horas",
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"), "registros": registros,
            "stale": False}


smn_server.fetch_smn = fetch_smn_ligero

for _n in ("get_forecast", "get_ssn_events", "get_radar_metadata", "get_tectonic_boundaries",
           "get_goes_metadata", "get_goes_history"):
    if hasattr(smn_server, _n):
        setattr(smn_server, _n, _una_a_la_vez(getattr(smn_server, _n)))

_limites = {"gz": None, "crudo": None, "codigo": 502, "t": 0.0}
_limites_candado = threading.Lock()


def _redondea(obj, dec=5):
    if isinstance(obj, float):
        return round(obj, dec)
    if isinstance(obj, list):
        return [_redondea(x, dec) for x in obj]
    if isinstance(obj, dict):
        return {k: _redondea(v, dec) for k, v in obj.items()}
    return obj


def limites_inegi():
    """Bytes JSON (y gzip) de los límites de INEGI; una sola descarga a la vez, se renueva cada 24 h."""
    import gzip
    with _limites_candado:
        if _limites["crudo"] is not None and time.time() - _limites["t"] < 24 * 3600:
            return _limites
        try:
            datos = _redondea(smn_server.fetch_inegi_boundaries())
            codigo = 200 if datos.get("ok") else 502
        except Exception as e:
            if _limites["crudo"] is not None:
                return _limites
            datos, codigo = {"ok": False, "error": str(e)}, 502
        crudo = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        del datos
        _limites.update(crudo=crudo, gz=gzip.compress(crudo, 6), codigo=codigo,
                        t=time.time() if codigo == 200 else 0.0)
        return _limites

ESQUEMA_USUARIOS = """
CREATE TABLE IF NOT EXISTS usuarios (
    correo        text PRIMARY KEY,
    nombre        text NOT NULL,
    dependencia   text,
    rol           text NOT NULL CHECK (rol IN ('consulta', 'dependencia', 'operador', 'admin')),
    hash          text NOT NULL,
    activo        boolean NOT NULL DEFAULT true,
    creado_en     timestamptz NOT NULL DEFAULT now(),
    ultimo_acceso timestamptz
);
"""


# --- utilidades -------------------------------------------------------------------------------
def con():
    import psycopg
    return psycopg.connect(DB, connect_timeout=10)


def s3():
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        return None
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL"),
                        config=Config(s3={"addressing_style": "path"}))


def es_gobierno(correo):
    if correo in EXCEPCIONES:
        return True
    dom = correo.rsplit("@", 1)[-1]
    return dom in PERSONALES or any(dom == d or dom.endswith("." + d) for d in DOMINIOS)


def hash_clave(clave, sal=None):
    sal = sal or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, ITER)
    return f"pbkdf2_sha256${ITER}${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def verifica_clave(clave, guardado):
    try:
        _, it, sal, h = guardado.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), base64.b64decode(sal), int(it))
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def firmar(datos):
    cuerpo = _b64(json.dumps(datos, separators=(",", ":")).encode())
    return cuerpo + "." + _b64(hmac.new(SECRETO.encode(), cuerpo.encode(), hashlib.sha256).digest())


def leer_firma(token):
    try:
        cuerpo, firma = token.split(".")
        esperada = _b64(hmac.new(SECRETO.encode(), cuerpo.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(firma, esperada):
            return None
        datos = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
        return datos if datos.get("exp", 0) > time.time() else None
    except (ValueError, TypeError):
        return None


def bloqueado(clave):
    with _candado:
        f = _fallos.get(clave)
        return bool(f and f[1] > time.time())


def registra_fallo(clave):
    with _candado:
        f = _fallos.setdefault(clave, [0, 0])
        f[0] += 1
        if f[0] >= 5:
            f[0], f[1] = 0, time.time() + 900


def bitacora(tabla, registro, estado, usuario, nota=None):
    try:
        with con() as c:
            c.execute("INSERT INTO eventos (tabla, registro, estado, usuario, nota) VALUES (%s, %s, %s, %s, %s)",
                      (tabla, registro, estado, usuario, nota))
    except Exception:
        pass


WEB = Path(__file__).resolve().parent / "web"     # plantillas y recursos públicos (inicio, acceso, errores)
_plantillas = {}


def plantilla(nombre):
    if nombre not in _plantillas:
        _plantillas[nombre] = (WEB / nombre).read_text(encoding="utf-8")
    return _plantillas[nombre]


PUBLICOS = {"/publico/estilo.css": ("estilo.css", "text/css; charset=utf-8"),
            "/publico/mapa.svg": ("mapa.svg", "image/svg+xml"),
            "/publico/logo.png": ("logo.png", "image/png"),
            "/publico/logo_icono.png": ("logo_icono.png", "image/png"),
            "/favicon.ico": ("logo_icono.png", "image/png")}

ERRORES = {
    403: ("Acceso no autorizado", "Tu cuenta no tiene permiso para ver esta sección. Si lo necesitas, pide al administrador que cambie tu rol.", "/tablero", "Ir al tablero"),
    404: ("Esta página no existe", "La dirección no existe o ya no está disponible. Revisa la liga o vuelve al tablero.", "/tablero", "Ir al tablero"),
    405: ("Acción no permitida", "Esta dirección no acepta esa operación.", "/", "Inicio"),
    429: ("Acceso bloqueado temporalmente",
          "Hubo demasiados intentos fallidos. Por seguridad, espera 15 minutos e inténtalo de nuevo.", "/login", "Volver al acceso"),
    500: ("Algo salió mal", "Ocurrió un error inesperado. Intenta de nuevo en unos minutos.", "/", "Inicio"),
    503: ("Servicio no disponible", "La base de datos no responde en este momento. Intenta de nuevo en unos minutos.", "/login", "Reintentar"),
}
ETIQUETAS = {403: "Acceso denegado", 404: "No encontrado", 405: "No permitido", 429: "Bloqueo de seguridad",
             500: "Error del servidor", 503: "Sin servicio"}

BARRA = """<div id="sivia-sesion" style="position:fixed;right:14px;bottom:14px;z-index:9999;display:flex;align-items:center;
gap:10px;padding:7px 8px 7px 14px;border-radius:999px;background:rgba(5,8,15,.94);border:1px solid #2a3a5e;color:#eaf0f8;
font:500 13px/1.2 system-ui,'Segoe UI',sans-serif;box-shadow:0 12px 28px -12px rgba(0,0,0,.6)">
<img src="/publico/logo_icono.png" alt="" style="width:24px;height:auto">
<span>__USUARIO__ <span style="color:#aeb9cd">· __ROL__</span></span>
__EXTRA__<a href="/cuenta" style="padding:5px 10px;border-radius:999px;border:1px solid #2a3a5e;color:#eaf0f8;text-decoration:none">Mi cuenta</a>
<a href="/salir" style="padding:5px 12px;border-radius:999px;background:#3cc8d4;color:#031417;font-weight:700;text-decoration:none">Salir</a>
</div></body>"""


# --- servidor ---------------------------------------------------------------------------------
class Handler(smn_server.Handler):
    sesion = None

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if HTTPS:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, texto, code=200, cookie=None):
        body = texto.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redir(self, destino, cookie=None):
        self.send_response(303)
        self.send_header("Location", destino)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _cuerpo_crudo(self):
        n = min(int(self.headers.get("Content-Length") or 0), 10_000)
        return self.rfile.read(n) if n > 0 else b""

    def _cuerpo(self):
        try:
            return json.loads(self._cuerpo_crudo() or b"{}")
        except ValueError:
            return {}

    def _cookie(self, valor, edad):
        return (f"sivia_sesion={valor}; Path=/; Max-Age={edad}; HttpOnly; SameSite=Strict"
                + ("; Secure" if HTTPS else ""))

    def _sesion(self):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == "sivia_sesion":
                return leer_firma(v)
        return None

    def _login_pagina(self, siguiente="/tablero", error=""):
        sig = siguiente if siguiente.startswith("/") and not siguiente.startswith("//") else "/tablero"
        msg = f'<p class="alerta" role="alert">{html.escape(error)}</p>' if error else ""
        return self._html(plantilla("acceso.html").replace("__SIG__", html.escape(sig)).replace("__ERROR__", msg),
                          401 if error else 200)

    def _login(self):
        datos = parse_qs(self._cuerpo_crudo().decode("utf-8", "replace"))
        correo = (datos.get("correo", [""])[0]).strip().lower()
        clave = datos.get("clave", [""])[0]
        sig = datos.get("siguiente", ["/tablero"])[0]
        llave = f"{correo}|{self._ip()}"
        if bloqueado(llave) or bloqueado(correo):
            return self._pagina_error(429)
        fila = None
        if CORREO_OK.match(correo) and es_gobierno(correo) and DB:
            try:
                with con() as c:
                    fila = c.execute("SELECT nombre, dependencia, rol, hash, activo FROM usuarios WHERE correo = %s",
                                     (correo,)).fetchone()
            except Exception:
                return self._pagina_error(503)
        if not fila or not fila[4] or not verifica_clave(clave, fila[3]):
            if fila is None:
                hash_clave(clave)          # mismo tiempo de respuesta exista o no la cuenta
            registra_fallo(llave)
            registra_fallo(correo)
            bitacora("acceso", correo[:120] or "-", "rechazado", correo[:120] or None, self._ip())
            return self._login_pagina(sig, "Correo o contraseña incorrectos, o la cuenta no está autorizada.")
        with con() as c:
            c.execute("UPDATE usuarios SET ultimo_acceso = now() WHERE correo = %s", (correo,))
        bitacora("acceso", correo, "inicio", correo, self._ip())
        token = firmar({"u": correo, "n": fila[0], "d": fila[1], "r": fila[2], "exp": int(time.time()) + DURACION})
        destino = sig if sig.startswith("/") and not sig.startswith("//") and sig != "/" else "/tablero"
        return self._redir(destino, self._cookie(token, DURACION))

    def _protegido(self, ruta):
        """Con login activo: exige sesión. Devuelve True si ya respondió."""
        if not SECRETO:
            return False
        self.sesion = self._sesion()
        if self.sesion:
            return False
        if ruta.startswith("/api/"):
            self._json({"ok": False, "login": True, "error": "inicia sesión"}, 401)
        else:
            self._redir("/login?siguiente=" + quote(ruta))
        return True

    def _estatico_permitido(self, ruta):
        return ruta in ("/tablero", "/index.html") or (ruta.startswith("/img/") and ".." not in ruta)

    def _index(self):
        texto = (Path(smn_server.ROOT) / "index.html").read_text(encoding="utf-8")
        if self.sesion:
            barra = (BARRA.replace("__USUARIO__", html.escape(self.sesion.get("n") or self.sesion["u"]))
                     .replace("__ROL__", html.escape(self.sesion["r"] + (f" ({self.sesion['d']})" if self.sesion.get("d") else "")))
                     .replace("__EXTRA__", '<a href="/admin" style="padding:5px 10px;border-radius:999px;border:1px solid '
                              '#3cc8d4;color:#3cc8d4;text-decoration:none">Panel</a>' if self.sesion["r"] == "admin" else ""))
            texto = texto.replace("</body>", barra, 1)
        return self._html(texto)

    def _pagina_error(self, codigo, titulo=None, mensaje=None):
        t, m, liga, boton = ERRORES.get(codigo, ERRORES[500] if codigo >= 500 else ERRORES[404])
        txt = (plantilla("error.html").replace("__CODIGO__", str(codigo)).replace("__TITULO__", html.escape(titulo or t))
               .replace("__MENSAJE__", html.escape(mensaje or m)).replace("__LIGA__", liga).replace("__BOTON__", boton)
               .replace("__ETIQUETA__", ETIQUETAS.get(codigo, "Aviso"))
               .replace("__GRAVEDAD__", "grave" if codigo in (403, 429) or codigo >= 500 else ""))
        return self._html(txt, codigo)

    def send_error(self, code, message=None, explain=None):
        if urlsplit(getattr(self, "path", "/") or "/").path.startswith("/api/"):
            return self._json({"ok": False, "error": message or "error"}, code)
        return self._pagina_error(code)

    def _inicio(self):
        s = self._sesion() if SECRETO else {"n": "local"}
        if s:
            accion = '<a class="boton" href="/tablero">Ir al tablero</a>' + (
                '<a class="boton sec" href="/admin">Panel de administración</a>' if s.get("r") == "admin" else "") + \
                '<a class="boton sec" href="/salir">Cerrar sesión</a>'
            barra = '<a class="boton chico" href="/tablero">Tablero</a>'
            acceso = '<a class="boton sec" href="/tablero">Abrir el tablero</a>'
        else:
            accion = '<a class="boton" href="/login">Entrar al tablero</a><a class="boton sec" href="#como-funciona">Cómo funciona</a>'
            barra = '<a class="boton chico" href="/login">Entrar</a>'
            acceso = '<a class="boton sec" href="/login">Iniciar sesión</a>'
        return self._html(plantilla("inicio.html").replace("__ACCION_BARRA__", barra).replace("__ACCION_ACCESO__", acceso)
                          .replace("__ACCION__", accion))

    def _publico(self, ruta):
        nombre, tipo = PUBLICOS[ruta]
        cuerpo = (WEB / nombre).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _estado_publico(self):
        """Resumen del pronóstico SMN para la barra de la pantalla de inicio (dato público, sin información del sistema)."""
        try:
            pr = smn_server.get_forecast()
            regs = pr.get("registros") or []
            ahora = time.strftime("%Y-%m-%dT%H", time.gmtime(time.time() - 6 * 3600))   # hora de Guadalajara
            r = next((x for x in regs if (x.get("fecha_hora") or "") >= ahora), regs[0] if regs else None)
            if not pr.get("ok", True) or not r:
                return self._json({"ok": False}, 200)
            return self._json({"ok": True, "cielo": r.get("descripcion"), "temperatura": r.get("temperatura_c"),
                               "lluvia": r.get("probabilidad_lluvia"), "viento": r.get("viento_kmh"),
                               "hora": r.get("fecha_hora")})
        except Exception:
            return self._json({"ok": False}, 200)

    def _actor(self):
        return self.sesion["u"] if self.sesion else "local"

    def _es_admin(self):
        """Sesión de administrador y, en la base, cuenta admin activa (por si la cambiaron después del login)."""
        if not SECRETO:
            return True
        if not self.sesion or self.sesion.get("r") != "admin" or not DB:
            return False
        try:
            with con() as c:
                return admin.es_admin_activo(c, self.sesion["u"])
        except Exception:
            return False

    def _correo_valido(self, correo):
        return bool(CORREO_OK.match(correo) and es_gobierno(correo))

    def _bloqueos(self):
        ahora = time.time()
        with _candado:
            return sorted({k.split("|")[0] for k, f in _fallos.items() if f[1] > ahora and k.split("|")[0]})

    def _desbloquea(self, correo):
        with _candado:
            for k in [k for k in _fallos if k == correo or k.startswith(correo + "|")]:
                del _fallos[k]

    def _get_admin(self, ruta, consulta):
        if not self._es_admin():
            return self._json({"ok": False, "error": "solo para administradores"}, 403)
        if not DB:
            return self._json({"ok": False, "error": "sin base de datos (DATABASE_URL)"}, 503)
        q = parse_qs(consulta)
        filtros = {"tabla": q.get("tipo", [None])[0] or None, "usuario": q.get("usuario", [None])[0] or None,
                   "texto": (q.get("texto", [""])[0] or "").strip()[:60] or None}
        try:
            with con() as c:
                if ruta == "/api/admin/resumen":
                    return self._json({"ok": True, **admin.resumen(c), "bloqueados": self._bloqueos()})
                if ruta == "/api/admin/usuarios":
                    return self._json({"ok": True, "usuarios": admin.usuarios(c), "bloqueados": self._bloqueos(),
                                       "roles": admin.ROLES, "dependencias": admin.DEPENDENCIAS, "yo": self._actor()})
                if ruta == "/api/admin/bitacora":
                    return self._json({"ok": True, "eventos": admin.eventos(c, limite=q.get("limite", [300])[0], **filtros)})
                if ruta == "/api/admin/bitacora.csv":
                    cuerpo = admin.eventos_csv(c, **filtros).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="bitacora_sivia.csv"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(cuerpo)))
                    self.end_headers()
                    self.wfile.write(cuerpo)
                    return None
        except Exception as e:
            return self._json({"ok": False, "error": type(e).__name__}, 502)
        return self._json({"ok": False, "error": "ruta desconocida"}, 404)

    def _post_admin(self, ruta):
        datos = self._cuerpo()
        actor = self._actor()
        if ruta == "/api/cuenta/clave":
            if not self.sesion and SECRETO:
                return self._json({"ok": False, "login": True, "error": "inicia sesión"}, 401)
            try:
                with con() as c:
                    admin.cambiar_mi_clave(c, actor, str(datos.get("actual", "")), str(datos.get("nueva", "")),
                                           verifica_clave, hash_clave)
                return self._json({"ok": True})
            except admin.ErrorAdmin as e:
                return self._json({"ok": False, "error": str(e)}, e.codigo)
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__}, 502)
        if not self._es_admin():
            return self._json({"ok": False, "error": "solo para administradores"}, 403)
        try:
            if ruta == "/api/admin/desbloquear":
                correo = str(datos.get("correo", "")).strip().lower()
                self._desbloquea(correo)
                bitacora("usuarios", correo[:120], "desbloqueo", actor, "panel")
                return self._json({"ok": True})
            with con() as c:
                if ruta == "/api/admin/usuarios":
                    return self._json({"ok": True, **admin.alta(c, datos, actor, self._correo_valido, hash_clave)})
                m = re.match(r"^/api/admin/usuarios/([^/]+)$", ruta)
                if m:
                    return self._json({"ok": True, **admin.actualizar(c, unquote(m.group(1)), datos, actor, hash_clave)})
        except admin.ErrorAdmin as e:
            return self._json({"ok": False, "error": str(e)}, e.codigo)
        except Exception as e:
            return self._json({"ok": False, "error": type(e).__name__}, 502)
        return self._json({"ok": False, "error": "ruta desconocida"}, 404)

    def do_HEAD(self):
        return self._json({"ok": True}) if urlsplit(self.path).path in ("/salud", "/") else self.send_error(405)

    def do_GET(self):
        u = urlsplit(self.path)
        ruta = u.path
        if ruta == "/salud":
            return self._json({"ok": True})
        if ruta == "/":
            return self._inicio()
        if ruta in PUBLICOS:
            return self._publico(ruta)
        if ruta == "/api/publico":
            return self._estado_publico()
        if ruta == "/login":
            if not SECRETO or self._sesion():
                return self._redir("/tablero")
            return self._login_pagina(parse_qs(u.query).get("siguiente", ["/tablero"])[0])
        if ruta == "/salir":
            if self._sesion():
                bitacora("acceso", self._sesion()["u"], "salida", self._sesion()["u"], self._ip())
            return self._redir("/", self._cookie("", 0))
        if self._protegido(ruta):
            return None
        if ruta == "/admin":
            if not self._es_admin():
                return self._pagina_error(403)
            nombre = (self.sesion or {}).get("n") or self._actor()
            return self._html(plantilla("admin.html").replace("__USUARIO__", html.escape(nombre)))
        if ruta == "/cuenta":
            s = self.sesion or {"u": "local", "n": "Uso local", "r": "admin"}
            return self._html(plantilla("cuenta.html").replace("__USUARIO__", html.escape(s.get("n") or s["u"]))
                              .replace("__CORREO__", html.escape(s["u"])).replace("__ROL__", html.escape(s["r"])))
        if ruta.startswith("/api/admin/"):
            return self._get_admin(ruta, u.query)
        if ruta == "/api/sesion":
            s = self.sesion or {}
            return self._json({"ok": True, "login": bool(SECRETO), "correo": s.get("u"), "nombre": s.get("n"),
                               "rol": s.get("r", "admin" if not SECRETO else None), "dependencia": s.get("d")})
        if not ruta.startswith(("/api/bd/", "/api/candidatos", "/api/ordenes", "/api/evidencia/")):
            if ruta == "/api/limites":
                lim = limites_inegi()
                gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
                cuerpo = lim["gz"] if gz else lim["crudo"]
                self.send_response(lim["codigo"])
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "private, max-age=3600")
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)
                return None
            if ruta.startswith("/api/"):
                return super().do_GET()            # clima, sismos, radar, satélite
            if SECRETO and not self._estatico_permitido(ruta):
                return self.send_error(404)
            if ruta in ("/tablero", "/index.html"):
                return self._index()
            return super().do_GET()
        if not DB:
            return self._json({"ok": False, "error": "sin base de datos (DATABASE_URL)"}, 503)
        try:
            if ruta == "/api/bd/estado":
                with con() as c:
                    n = c.execute("SELECT (SELECT count(*) FROM ordenes), (SELECT count(*) FROM candidatos), "
                                  "(SELECT count(*) FROM candidatos WHERE estado <> 'pendiente')").fetchone()
                return self._json({"ok": True, "ordenes": n[0], "candidatos": n[1], "revisados": n[2],
                                   "bucket": bool(s3())})
            if ruta == "/api/candidatos":
                with con() as c:
                    filas = c.execute("SELECT id, estado FROM candidatos").fetchall()
                return self._json({"ok": True, "estados": dict(filas)})
            if ruta == "/api/ordenes":
                dep = parse_qs(u.query).get("dependencia", [None])[0]
                if self.sesion and self.sesion["r"] == "dependencia":
                    dep = self.sesion.get("d")            # una dependencia solo ve lo suyo
                q = ("SELECT o.id, o.dependencia, o.hallazgo, o.prioridad, o.estado, i.nombre, i.zona, o.actualizada_en "
                     "FROM ordenes o LEFT JOIN instituciones i ON i.id = o.institucion_id")
                with con() as c:
                    filas = c.execute(q + (" WHERE o.dependencia = %s" if dep else "") + " ORDER BY o.prioridad DESC",
                                      (dep,) if dep else ()).fetchall()
                cols = ["id", "dependencia", "hallazgo", "prioridad", "estado", "institucion", "zona", "actualizada_en"]
                return self._json({"ok": True, "ordenes": [dict(zip(cols, f)) for f in filas]})
            if ruta.startswith("/api/evidencia/"):
                archivo = unquote(ruta.rsplit("/", 1)[1])
                cli = s3()
                if not ID_OK.match(archivo) or not cli:
                    return self._json({"ok": False, "error": "sin bucket o archivo inválido"}, 404)
                with con() as c:
                    fila = c.execute("SELECT bucket_key FROM imagenes WHERE archivo = %s", (archivo,)).fetchone()
                if not fila or not fila[0]:
                    return self._json({"ok": False, "error": "imagen no subida al bucket"}, 404)
                liga = cli.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": fila[0]}, ExpiresIn=900)
                self.send_response(302)
                self.send_header("Location", liga)
                self.end_headers()
                return None
        except Exception as e:  # la base no responde: el tablero sigue con el navegador
            return self._json({"ok": False, "error": type(e).__name__}, 502)
        return self._json({"ok": False, "error": "ruta desconocida"}, 404)

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/login" and SECRETO:
            return self._login()
        if u.path.startswith(("/api/admin/", "/api/cuenta/")):
            if self._protegido(u.path):
                return None
            if SECRETO and self.headers.get("Origin") and urlsplit(self.headers["Origin"]).netloc != self.headers.get("Host"):
                return self._json({"ok": False, "error": "origen no permitido"}, 403)
            if not DB:
                return self._json({"ok": False, "error": "sin base de datos (DATABASE_URL)"}, 503)
            return self._post_admin(u.path)
        m = re.match(r"^/api/(candidatos|ordenes)/([^/]+)$", u.path)
        if not m:
            return self._json({"ok": False, "error": "ruta desconocida"}, 404)
        if self._protegido(u.path):
            return None
        if SECRETO and self.headers.get("Origin") and urlsplit(self.headers["Origin"]).netloc != self.headers.get("Host"):
            return self._json({"ok": False, "error": "origen no permitido"}, 403)
        if not DB:
            return self._json({"ok": False, "error": "sin base de datos (DATABASE_URL)"}, 503)
        tabla, rid = m.group(1), unquote(m.group(2))
        estado = str(self._cuerpo().get("estado", ""))
        if not ID_OK.match(rid) or estado not in (EST_CAND if tabla == "candidatos" else EST_ORD):
            return self._json({"ok": False, "error": "dato inválido"}, 400)
        rol = self.sesion["r"] if self.sesion else "admin"
        usuario = self.sesion["u"] if self.sesion else "local"
        if rol == "consulta" or (tabla == "candidatos" and rol not in ("operador", "admin")):
            return self._json({"ok": False, "error": "tu rol no permite este cambio"}, 403)
        extra = "revisado_en = now()" if tabla == "candidatos" else "actualizada_en = now()"
        filtro, params = "", [estado, rid]
        if rol == "dependencia":
            filtro, params = " AND dependencia = %s", [estado, rid, self.sesion.get("d")]
        try:
            with con() as c:
                if SECRETO:   # la cuenta pudo darse de baja después de iniciar sesión
                    act = c.execute("SELECT activo FROM usuarios WHERE correo = %s", (usuario,)).fetchone()
                    if not act or not act[0]:
                        return self._json({"ok": False, "error": "cuenta desactivada"}, 403)
                n = c.execute(f"UPDATE {tabla} SET estado = %s, {extra} WHERE id = %s{filtro}", params).rowcount
                if n:
                    c.execute("INSERT INTO eventos (tabla, registro, estado, usuario) VALUES (%s, %s, %s, %s)",
                              (tabla, rid, estado, usuario))
        except Exception as e:
            return self._json({"ok": False, "error": type(e).__name__}, 502)
        return self._json({"ok": bool(n), "id": rid, "estado": estado}, 200 if n else 404)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Tablero en vivo con base de datos")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost") and (not SECRETO or len(SECRETO) < 32 or not DB):
        sys.exit("Para abrirlo a la red define DATABASE_URL y SESION_SECRETO (32+ caracteres): sin ellos no hay login.")
    if SECRETO and DB:
        with con() as c:
            c.execute(ESQUEMA_USUARIOS)
    threading.Thread(target=limites_inegi, daemon=True).start()   # precarga para que el tablero abra rápido
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Tablero: http://{a.host}:{a.port}/index.html")
    print("Base de datos:", "conectada por DATABASE_URL" if DB else "no configurada (validaciones en el navegador)")
    print("Bucket:", f"'{BUCKET}' configurado" if s3() else "no configurado")
    print("Acceso:", f"solo cuentas de gobierno ({', '.join(DOMINIOS)})" + (f" + {len(EXCEPCIONES)} correo(s) autorizado(s)"
          if EXCEPCIONES else "") if SECRETO else "abierto (solo uso local)")
    print("Presiona Ctrl+C para detener el servidor.")
    try:
        if not a.no_browser:
            webbrowser.open(f"http://{a.host}:{a.port}/index.html")
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
