"""
Recorrido simulado para todos los usuarios.

Reproduce, dentro de la propia plataforma, un recorrido ya grabado de la cámara (recorrido_demo/: 70 cuadros de
calles de Guadalajara con lo que detectó el modelo YOLO11s v9). Así cualquiera que entre al tablero ve un vehículo
recorriendo la ciudad, su trazo en el mapa y lo que "ve" la cámara, sin tener que correr nada en su computadora.
Se graba con:  python agente/camara_simulada.py --grabar plataforma/recorrido_demo
Se apaga con la variable de entorno DEMO_RECORRIDO=0. No crea candidatos ni escribe en la base de datos.
"""
import json
import os
import threading
import time
from pathlib import Path

import camaras

VEHICULO = "GDL-SIM"
CARPETA = Path(__file__).resolve().parent / "recorrido_demo"
SEG_POR_CUADRO = 3.0          # cada foto se queda 3 s
PASOS = 3                     # posiciones intermedias entre fotos (el vehículo se mueve suave)
PAUSA_FINAL = 20              # segundos detenido al terminar antes de volver a empezar

_hilo = None


def activo():
    return os.environ.get("DEMO_RECORRIDO", "1") != "0" and (CARPETA / "recorrido.json").exists()


def _bucle():
    datos = json.loads((CARPETA / "recorrido.json").read_text(encoding="utf-8"))
    cuadros = datos["cuadros"]
    total = len(cuadros)
    while True:
        camaras.reinicia_traza(VEHICULO)
        previo = None
        for i, c in enumerate(cuadros, 1):
            try:
                vista = (CARPETA / c["foto"]).read_bytes()
            except OSError:
                vista = None
            if previo:                                    # avanza del punto anterior al nuevo
                for k in range(1, PASOS):
                    f = k / PASOS
                    camaras.registra_posicion(VEHICULO, previo["lat"] + (c["lat"] - previo["lat"]) * f,
                                              previo["lng"] + (c["lng"] - previo["lng"]) * f,
                                              i - 1, total, previo.get("ms"), previo.get("dets", ()), None, True)
                    time.sleep(SEG_POR_CUADRO / PASOS)
            camaras.registra_posicion(VEHICULO, c["lat"], c["lng"], i, total, c.get("ms"), c.get("dets", ()),
                                      vista, True)
            time.sleep(SEG_POR_CUADRO / PASOS)
            previo = c
        time.sleep(PAUSA_FINAL)


def inicia():
    """Arranca la reproducción en segundo plano (una sola vez)."""
    global _hilo
    if _hilo or not activo():
        return False
    _hilo = threading.Thread(target=_bucle, name="recorrido-demo", daemon=True)
    _hilo.start()
    return True
