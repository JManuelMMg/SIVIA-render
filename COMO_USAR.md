# Tablero en vivo — SIVIA

Une el tablero del colaborador B (clima SMN, satélite GOES, radar, límites INEGI, sismos SSN, análisis)
con los datos reales del proyecto: 494 órdenes de trabajo del Coordinador y 127 candidatos del detector
YOLO11s v9 pendientes de validación humana, con su foto de evidencia de Mapillary.

## Abrirlo
Doble clic en `iniciar_tablero.bat` (o `python servidor.py` en esta carpeta) y entra a
`http://127.0.0.1:8000/index.html`. Necesita internet para el mapa y las capas en vivo.
Sin servidor, `index.html` abre como archivo, pero sin clima, radar, satélite ni sismos.

## Con base de datos (validaciones compartidas)
En PowerShell, dentro de esta carpeta: `& .\iniciar_con_bd.ps1`. Pide la cadena de conexión de
PostgreSQL (y, si quieres, las llaves del bucket) sin mostrarlas, las usa solo en esa ventana y las
borra al cerrar. Así, lo que confirma o descarta un operador queda en la base y lo ven todos; sin base,
cada navegador guarda lo suyo. Para cargar o actualizar la base: `prototipo\plataforma\iniciar_con_bd.ps1 -Accion cargar`.

## Regenerarlo
```
python prototipo/agente/tablero.py        # reduce las imágenes de evidencia
python prototipo/agente/tablero_vivo.py   # arma esta carpeta
```

## Qué es cada cosa
- **Órdenes:** la prioridad es la del Coordinador (severidad × tipo de institución × siniestralidad
  INEGI × población). El pronóstico SMN la multiplica (lluvia: alcantarillas ×1.35 y baches ×1.2;
  viento: árboles y cableado ×1.5). Son factores exploratorios, no criterios oficiales.
- **Severidad:** sale de la gravedad del tipo de hallazgo (crítica: árbol, alcantarilla abierta;
  alta: pavimento, falta de cruce; media: rejilla, cableado).
- **Validación IA:** un candidato por foto (el de mayor confianza). No se vuelve orden hasta que
  una persona lo confirma.
- **Sismos y placas:** solo monitoreo de reportes; no predicen ni cambian prioridades.
