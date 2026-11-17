Aquí tienes una versión **más técnica** del README, destacando claramente el uso del modelo de IA:

# SIVIA Render

**Tablero operativo en vivo del sistema SIVIA**  
Sistema de Vigilancia e Intervención Urbana para Guadalajara.

Plataforma web que integra detecciones de inteligencia artificial, órdenes de trabajo prioritarias y capas de información geoespacial en tiempo real.



## Descripción técnica

SIVIA Render es el frontend + backend operativo del proyecto SIVIA. Su función principal es:

- Visualizar y gestionar **órdenes de trabajo** generadas por el Coordinador.
- Presentar **candidatos detectados por IA** pendientes de validación humana.
- Integrar capas en vivo (clima, satélite, radar, sismos y límites municipales).
- Permitir la validación y turnado de hallazgos mediante un sistema de roles.

El sistema está orientado a personal de gobierno y cuenta con autenticación, control de acceso y bitácora de eventos.



## Modelo de Inteligencia Artificial

El sistema utiliza un modelo de detección de objetos basado en **YOLO11s (versión v9)**.

| Aspecto                    | Detalle                                      |
|---------------------------|----------------------------------------------|
| Arquitectura              | YOLO11s                                      |
| Versión                   | v9                                           |
| Tarea                     | Detección de hallazgos urbanos               |
| Entrada                   | Imágenes (Mapillary / cámaras de vehículos)  |
| Salida                    | Candidatos con bounding box y confianza      |
| Validación                | Obligatoria (humana) antes de generar orden  |

### Flujo de detección

1. El modelo procesa imágenes y genera candidatos.
2. Se selecciona el candidato de mayor confianza por imagen.
3. El operador valida o descarta el candidato.
4. Solo los candidatos **confirmados** se convierten en órdenes de trabajo.

El modelo se expone también para uso en cámaras de vehículos y dispositivos móviles a través del endpoint:
GET /api/camara/modelo.onnx

## Arquitectura general

┌─────────────────────┐
│   Frontend (HTML)   │
│   index.html        │
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│   servidor.py       │  ← Servidor principal
│  (ThreadingHTTP)    │
└─────────┬───────────┘
          │
┌─────────┴──────────────────────────────┐
│                                        │
▼                                        ▼
smn_server.py                     PostgreSQL
(clima, GOES, radar,              (usuarios, órdenes,
 sismos, INEGI)                    candidatos, bitácora)
          │
          ▼
     Bucket S3
   (evidencias)


## Módulos principales

| Archivo            | Responsabilidad                                      |
|--------------------|------------------------------------------------------|
| `servidor.py`      | Servidor HTTP principal, autenticación y API         |
| `smn_server.py`    | Integración con SMN, GOES, radar, SSN e INEGI        |
| `camaras.py`       | Gestión de cámaras y detecciones en tiempo real      |
| `turnado.py`       | Lógica de creación y asignación de órdenes           |
| `admin.py`         | Panel de administración y bitácora                   |
| `crear_usuario.py` | Alta de usuarios con roles                           |



## API principal

| Endpoint                          | Método | Descripción                                      |
|-----------------------------------|--------|--------------------------------------------------|
| `/api/candidatos`                 | GET    | Estado de candidatos del modelo                  |
| `/api/candidatos/<id>`            | POST   | Confirmar / descartar candidato                  |
| `/api/ordenes`                    | GET    | Listado de órdenes (filtrable por dependencia)   |
| `/api/ordenes/<id>`               | POST   | Actualizar estado de una orden                   |
| `/api/ordenes/nueva`              | POST   | Crear orden a partir de un candidato validado    |
| `/api/camara/deteccion`           | POST   | Recibir detección desde cámara                   |
| `/api/camara/modelo.onnx`         | GET    | Descargar modelo YOLO11s v9                      |
| `/api/bd/estado`                  | GET    | Estado de conexión y conteos                     |


## Sistema de roles

| Rol            | Permisos                                              |
|----------------|-------------------------------------------------------|
| `consulta`     | Solo visualización                                    |
| `dependencia`  | Ve y actualiza órdenes de su dependencia              |
| `operador`     | Valida candidatos y gestiona cualquier orden          |
| `admin`        | Acceso completo + administración de usuarios          |


## Priorización de órdenes

La prioridad se calcula a partir de:

```
Prioridad = Severidad × Tipo de institución × Siniestralidad INEGI × Población
```

Adicionalmente se aplican factores meteorológicos del SMN:

- Lluvia → Alcantarillas ×1.35 | Baches ×1.2  
- Viento → Árboles y cableado ×1.5  

> Nota: Estos factores son exploratorios y no constituyen criterios oficiales.

---

## Tecnologías

| Capa              | Tecnología                          |
|-------------------|-------------------------------------|
| Backend           | Python 3.10+                        |
| Servidor          | `ThreadingHTTPServer`               |
| Base de datos     | PostgreSQL + `psycopg`              |
| Almacenamiento    | S3-compatible (`boto3`)             |
| Modelo de IA      | YOLO11s v9 (ONNX)                   |
| Frontend          | HTML + JavaScript nativo            |
| Despliegue        | Render (`render.yaml`)              |

---

## Instalación local

```bash
pip install -r requirements.txt
python servidor.py
```

Abrir:

```
http://127.0.0.1:8000/index.html
```

Con base de datos compartida:

```powershell
& .\iniciar_con_bd.ps1
```

---

## Variables de entorno (producción)

| Variable                  | Descripción                                      |
|---------------------------|--------------------------------------------------|
| `DATABASE_URL`            | Cadena de conexión PostgreSQL                    |
| `SESION_SECRETO`          | Secreto para firmar sesiones                     |
| `GOB_DOMINIOS`            | Dominios de gobierno autorizados                 |
| `AWS_ACCESS_KEY_ID`       | Credencial del bucket                            |
| `AWS_SECRET_ACCESS_KEY`   | Credencial del bucket                            |
| `BUCKET`                  | Nombre del bucket de evidencias                  |

---

## Seguridad

- Autenticación obligatoria en producción
- Contraseñas con PBKDF2-SHA256 (310,000 iteraciones)
- Cookies firmadas (HttpOnly + SameSite=Strict + Secure)
- Bloqueo temporal tras intentos fallidos
- Bitácora de accesos y cambios
- Solo se sirven archivos públicos necesarios (`index.html` e `img/`)

---

## Autor

**Juan Manuel Medina**  
GitHub: [@JManuelMMg](https://github.com/JManuelMMg)

---

## Licencia

Uso interno. Todos los derechos reservados.
```

