#!/usr/bin/env python3
"""
Alta, cambio de rol, baja y lista de cuentas de la plataforma SIVIA (solo personal de gobierno).

No hay registro abierto: un administrador corre este script con DATABASE_URL en el entorno. La contraseña la
escribe la propia persona en la terminal (no se muestra ni se guarda en archivos); se guarda solo su hash.

Uso:
  python crear_usuario.py alta  correo@guadalajara.gob.mx "Nombre Apellido" ROL [--dependencia "Parques y Jardines"]
  python crear_usuario.py clave correo@guadalajara.gob.mx          (cambiar contraseña)
  python crear_usuario.py rol   correo@guadalajara.gob.mx ROL [--dependencia ...]
  python crear_usuario.py baja  correo@guadalajara.gob.mx          (desactiva; la bitácora se conserva)
  python crear_usuario.py lista

ROL: consulta | dependencia | operador | admin. El rol "dependencia" exige --dependencia con el nombre exacto
que usa el Coordinador (Parques y Jardines, Dirección de Pavimentos, Dirección de Movilidad,
Alumbrado Público / CFE, Agua y Drenaje (SIAPA)).
"""
import argparse
import getpass
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from servidor import CORREO_OK, DOMINIOS, ESQUEMA_USUARIOS, ROLES, con, es_gobierno, hash_clave  # noqa: E402

DEPENDENCIAS = {"Parques y Jardines", "Dirección de Pavimentos", "Dirección de Movilidad",
                "Alumbrado Público / CFE", "Agua y Drenaje (SIAPA)"}


def pide_clave():
    for _ in range(3):
        a = getpass.getpass("Contraseña (mínimo 12 caracteres, con letras y números): ")
        if len(a) < 12 or not re.search(r"[A-Za-z]", a) or not re.search(r"\d", a):
            print("Demasiado débil.")
            continue
        if a != getpass.getpass("Repítela: "):
            print("No coinciden.")
            continue
        return a
    sys.exit("Sin contraseña válida; no se hizo ningún cambio.")


def valida(correo, rol=None, dep=None):
    if not CORREO_OK.match(correo) or not es_gobierno(correo):
        sys.exit(f"Solo se aceptan correos de gobierno ({', '.join(DOMINIOS)}).")
    if rol is not None and rol not in ROLES:
        sys.exit(f"Rol inválido. Usa: {', '.join(sorted(ROLES))}.")
    if rol == "dependencia" and dep not in DEPENDENCIAS:
        sys.exit("El rol dependencia necesita --dependencia con uno de: " + "; ".join(sorted(DEPENDENCIAS)))


def main():
    if not os.environ.get("DATABASE_URL"):
        sys.exit("Falta DATABASE_URL en el entorno (usa iniciar_con_bd.ps1 -Accion usuario).")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accion", choices=["alta", "clave", "rol", "baja", "lista"])
    ap.add_argument("correo", nargs="?")
    ap.add_argument("nombre_o_rol", nargs="?")
    ap.add_argument("rol", nargs="?")
    ap.add_argument("--dependencia")
    a = ap.parse_args()
    correo = (a.correo or "").strip().lower()

    with con() as c:
        c.execute(ESQUEMA_USUARIOS)
        if a.accion == "lista":
            for f in c.execute("SELECT correo, nombre, rol, coalesce(dependencia, '-'), activo, ultimo_acceso "
                               "FROM usuarios ORDER BY correo"):
                print(f"{f[0]:40s} {f[1]:28s} {f[2]:11s} {f[3]:26s} {'activa' if f[4] else 'BAJA':7s} {f[5] or ''}")
            return
        if a.accion == "alta":
            if not a.nombre_o_rol or not a.rol:
                sys.exit('Uso: alta correo "Nombre" ROL [--dependencia ...]')
            valida(correo, a.rol, a.dependencia)
            if c.execute("SELECT 1 FROM usuarios WHERE correo = %s", (correo,)).fetchone():
                sys.exit("La cuenta ya existe; usa 'clave' o 'rol'.")
            print(f"Alta de {correo} como {a.rol}. Que la persona escriba su contraseña:")
            c.execute("INSERT INTO usuarios (correo, nombre, dependencia, rol, hash) VALUES (%s, %s, %s, %s, %s)",
                      (correo, a.nombre_o_rol.strip(), a.dependencia if a.rol == "dependencia" else None, a.rol,
                       hash_clave(pide_clave())))
            c.execute("INSERT INTO eventos (tabla, registro, estado, usuario) VALUES ('usuarios', %s, %s, 'admin-cli')",
                      (correo, "alta:" + a.rol))
        elif a.accion == "clave":
            valida(correo)
            n = c.execute("UPDATE usuarios SET hash = %s WHERE correo = %s", (hash_clave(pide_clave()), correo)).rowcount
            if not n:
                sys.exit("No existe esa cuenta.")
            c.execute("INSERT INTO eventos (tabla, registro, estado, usuario) VALUES ('usuarios', %s, 'clave', 'admin-cli')",
                      (correo,))
        elif a.accion == "rol":
            valida(correo, a.nombre_o_rol, a.dependencia)
            n = c.execute("UPDATE usuarios SET rol = %s, dependencia = %s, activo = true WHERE correo = %s",
                          (a.nombre_o_rol, a.dependencia if a.nombre_o_rol == "dependencia" else None, correo)).rowcount
            if not n:
                sys.exit("No existe esa cuenta.")
            c.execute("INSERT INTO eventos (tabla, registro, estado, usuario) VALUES ('usuarios', %s, %s, 'admin-cli')",
                      (correo, "rol:" + a.nombre_o_rol))
        elif a.accion == "baja":
            n = c.execute("UPDATE usuarios SET activo = false WHERE correo = %s", (correo,)).rowcount
            if not n:
                sys.exit("No existe esa cuenta.")
            c.execute("INSERT INTO eventos (tabla, registro, estado, usuario) VALUES ('usuarios', %s, 'baja', 'admin-cli')",
                      (correo,))
    print("Listo.")


if __name__ == "__main__":
    main()
