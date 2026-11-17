# SIVIA Render

**Tablero en vivo de SIVIA** — Sistema de vigilancia e intervención urbana para Guadalajara.

Plataforma web que integra:

- Órdenes de trabajo prioritarias del Coordinador
- Detecciones de IA (YOLO11s v9) pendientes de validación humana
- Capas en vivo: clima SMN, satélite GOES, radar, límites INEGI, sismos SSN
- Evidencia fotográfica de Mapillary
- Sistema de roles y autenticación para personal de gobierno

---

## Características principales

| Módulo | Descripción |
|--------|-------------|
| **Órdenes** | Priorización por severidad × institución × siniestralidad INEGI × población + factores meteorológicos |
| **Validación IA** | Candidatos detectados por YOLO11s v9. Solo se convierten en orden tras confirmación humana |
| **Capas en vivo** | Pronóstico SMN, GOES, radar, límites municipales, sismos SSN/USGS |
| **Cámaras** | Soporte para detecciones desde vehículos y dispositivos móviles |
| **Roles** | consulta · dependencia · operador · admin |

---

## Requisitos

- Python 3.10+
- PostgreSQL (opcional, para validaciones compartidas)
- Cuenta en Render (o cualquier hosting) si se despliega en producción

```bash
pip install -r requirements.txt
