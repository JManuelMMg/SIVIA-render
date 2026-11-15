#!/usr/bin/env python3
"""
Servidor del tablero en vivo con base de datos y acceso restringido a personal de gobierno.

Extiende smn_server.py (clima SMN, GOES, radar, INEGI, SSN, USGS; sin cambios) con la plataforma:
  GET  /login, POST /login, GET /salir   inicio y cierre de sesión
  GET  /salud                          verificación para el hospedaje (no expone datos)
  GET  /api/sesion                     quién está conectado y con qué rol
  GET  /api/bd/estado                  conexión y conteos
  GET  /api/candidatos                 {id: estado} de los candidatos del modelo
  POST /api/candidatos/<id>            {"estado": "confirmado"|"descartado"|"pendiente"}      (operador, admin)
  GET  /api/ordenes?dependencia=...    órdenes con su estado (el rol "dependencia" ve solo las suyas)
  POST /api/ordenes/<id>               {"estado": "asignada"|"en_atencion"|"resuelta"|"descartada"|"pendiente"}
  GET  /api/evidencia/<archivo>        redirige a una liga firmada del bucket (si está configurado)

Seguridad (se activa al definir SESION_SECRETO; en Render es obligatorio):
  - Todo, incluido el tablero, exige sesión. Solo entran cuentas con correo de un dominio de gobierno
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
  DATABASE_URL, SESION_SECRETO, GOB_DOMINIOS, CORREOS_AUTORIZADOS (excepciones: correos exactos separados por coma)
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

DB = os.environ.get("DATABASE_URL")
BUCKET = os.environ.get("BUCKET", "reportbucket")
SECRETO = os.environ.get("SESION_SECRETO", "")
DOMINIOS = [d.strip().lower().lstrip("@") for d in
            os.environ.get("GOB_DOMINIOS", "guadalajara.gob.mx,jalisco.gob.mx,gob.mx").split(",") if d.strip()]
# Excepciones puntuales (p. ej. el equipo que desarrolla o un asesor externo): correos EXACTOS, nunca dominios.
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
    return any(dom == d or dom.endswith("." + d) for d in DOMINIOS)


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


PAGINA_LOGIN = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SIVIA · Acceso</title>
<style>
:root{--fondo:#F5F3EC;--tarjeta:#FBFBF8;--texto:#14302B;--suave:#5A6764;--acento:#1F5C4F;--borde:#D5DBD7;--error:#B8383F}
@media (prefers-color-scheme:dark){:root{--fondo:#101917;--tarjeta:#18241F;--texto:#E8EEEA;--suave:#A9B8B3;--acento:#5FB39C;--borde:#2D3B36;--error:#F08A8F}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--fondo);
color:var(--texto);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
main{width:100%;max-width:380px;background:var(--tarjeta);border:1px solid var(--borde);border-radius:14px;padding:32px 28px}
h1{margin:0 0 4px;font-size:28px}p{margin:0 0 20px;color:var(--suave);font-size:14px}
label{display:block;font-size:14px;font-weight:600;margin:14px 0 6px}
input{width:100%;padding:11px 12px;border:1px solid var(--borde);border-radius:8px;background:var(--fondo);color:var(--texto);font-size:16px}
button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:var(--acento);color:#fff;font-size:16px;font-weight:600;cursor:pointer}
.error{color:var(--error);font-size:14px;margin:14px 0 0}.nota{margin:20px 0 0;font-size:12px}
</style></head><body><main>
<h1>SIVIA</h1><p>Acceso exclusivo para personal del Gobierno de Guadalajara y de Jalisco.</p>
<form method="post" action="/login" autocomplete="on">
<input type="hidden" name="siguiente" value="__SIG__">
<label for="correo">Correo institucional</label>
<input id="correo" name="correo" type="email" required autocomplete="username" placeholder="nombre@guadalajara.gob.mx">
<label for="clave">Contraseña</label>
<input id="clave" name="clave" type="password" required autocomplete="current-password">
__ERROR__
<button type="submit">Entrar</button></form>
<p class="nota">Las cuentas las da de alta el administrador de la plataforma. Cada acceso queda registrado.</p>
</main></body></html>"""

BARRA = """<div id="sivia-sesion" style="position:fixed;right:12px;bottom:12px;z-index:9999;background:#14302B;color:#F5F3EC;
font:13px system-ui,sans-serif;padding:6px 12px;border-radius:999px;box-shadow:0 2px 8px rgba(0,0,0,.25)">
__USUARIO__ · __ROL__ · <a href="/salir" style="color:#E2A229">Salir</a></div></body>"""


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

    def _login_pagina(self, siguiente="/", error=""):
        sig = siguiente if siguiente.startswith("/") and not siguiente.startswith("//") else "/"
        msg = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
        return self._html(PAGINA_LOGIN.replace("__SIG__", html.escape(sig)).replace("__ERROR__", msg),
                          401 if error else 200)

    def _login(self):
        datos = parse_qs(self._cuerpo_crudo().decode("utf-8", "replace"))
        correo = (datos.get("correo", [""])[0]).strip().lower()
        clave = datos.get("clave", [""])[0]
        sig = datos.get("siguiente", ["/"])[0]
        llave = f"{correo}|{self._ip()}"
        if bloqueado(llave) or bloqueado(correo):
            return self._login_pagina(sig, "Demasiados intentos. Espera 15 minutos.")
        fila = None
        if CORREO_OK.match(correo) and es_gobierno(correo) and DB:
            try:
                with con() as c:
                    fila = c.execute("SELECT nombre, dependencia, rol, hash, activo FROM usuarios WHERE correo = %s",
                                     (correo,)).fetchone()
            except Exception:
                return self._login_pagina(sig, "La base de datos no responde. Intenta de nuevo.")
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
        destino = sig if sig.startswith("/") and not sig.startswith("//") else "/"
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
        return ruta in ("/", "/index.html") or (ruta.startswith("/img/") and ".." not in ruta)

    def _index(self):
        texto = (Path(smn_server.ROOT) / "index.html").read_text(encoding="utf-8")
        if self.sesion:
            barra = (BARRA.replace("__USUARIO__", html.escape(self.sesion.get("n") or self.sesion["u"]))
                     .replace("__ROL__", html.escape(self.sesion["r"] + (f" ({self.sesion['d']})" if self.sesion.get("d") else ""))))
            texto = texto.replace("</body>", barra, 1)
        return self._html(texto)

    def do_HEAD(self):
        return self._json({"ok": True}) if urlsplit(self.path).path == "/salud" else self.send_error(405)

    def do_GET(self):
        u = urlsplit(self.path)
        ruta = u.path
        if ruta == "/salud":
            return self._json({"ok": True})
        if ruta == "/login":
            if not SECRETO:
                return self._redir("/")
            return self._login_pagina(parse_qs(u.query).get("siguiente", ["/"])[0])
        if ruta == "/salir":
            if self._sesion():
                bitacora("acceso", self._sesion()["u"], "salida", self._sesion()["u"], self._ip())
            return self._redir("/login", self._cookie("", 0))
        if self._protegido(ruta):
            return None
        if ruta == "/api/sesion":
            s = self.sesion or {}
            return self._json({"ok": True, "login": bool(SECRETO), "correo": s.get("u"), "nombre": s.get("n"),
                               "rol": s.get("r", "admin" if not SECRETO else None), "dependencia": s.get("d")})
        if not ruta.startswith(("/api/bd/", "/api/candidatos", "/api/ordenes", "/api/evidencia/")):
            if ruta.startswith("/api/"):
                return super().do_GET()            # clima, sismos, radar, satélite, límites
            if SECRETO and not self._estatico_permitido(ruta):
                return self.send_error(404)
            if ruta in ("/", "/index.html"):
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
