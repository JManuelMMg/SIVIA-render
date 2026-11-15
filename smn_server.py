#!/usr/bin/env python3
"""Serve the dashboard and cached SMN, SSN, GOES satellite, radar, and tectonic data."""

import argparse
import gzip
import html
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit


ROOT = Path(__file__).resolve().parent
SMN_URL = "https://smn.conagua.gob.mx/tools/GUI/webservices/index.php?method=3"
SSN_RSS_URL = "http://www.ssn.unam.mx/rss/ultimos-sismos.xml"
SSN_CATALOG_URL = "http://www2.ssn.unam.mx:8080/catalogo/"
RAINVIEWER_URL = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_TILE_HOST = "https://tilecache.rainviewer.com"
USGS_PLATE_QUERY_URL = "https://earthquake.usgs.gov/arcgis/rest/services/eq/map_plateboundaries/MapServer/1/query"
INEGI_GEO_URL = "https://gaia.inegi.org.mx/wscatgeo/v2/geo"
GOES_IMAGE_SERVICE_URL = "https://satellitemaps.nesdis.noaa.gov/arcgis/rest/services/Most_Recent_MERGEDGC/ImageServer"
GOES_ARCHIVE_SERVICE_URL = "https://satellitemaps.nesdis.noaa.gov/arcgis/rest/services/MERGEDGC_Last_24hr/ImageServer"
STATE_NAME = "jalisco"
MUNICIPALITY_NAME = "guadalajara"
CACHE_SECONDS = 15 * 60
STALE_MAX_SECONDS = 3 * 60 * 60
cache = {"payload": None, "fetched_at": 0.0, "last_error": None}
SSN_CACHE_SECONDS = 5 * 60
SSN_STALE_MAX_SECONDS = 6 * 60 * 60
ssn_cache = {"payload": None, "fetched_at": 0.0, "last_error": None}
RADAR_CACHE_SECONDS = 5 * 60
RADAR_STALE_MAX_SECONDS = 30 * 60
radar_cache = {"payload": None, "fetched_at": 0.0, "last_error": None}
PLATE_CACHE_SECONDS = 24 * 60 * 60
plates_cache = {"payload": None, "fetched_at": 0.0, "last_error": None}
ADMIN_CACHE_SECONDS = 24 * 60 * 60
admin_cache = {"payload": None, "fetched_at": 0.0, "last_error": None}
GOES_CACHE_SECONDS = 2 * 60
GOES_STALE_MAX_SECONDS = 20 * 60
goes_cache = {"payload": None, "fetched_at": 0.0, "last_error": None}
GOES_HISTORY_CACHE_SECONDS = 5 * 60
GOES_HISTORY_STALE_MAX_SECONDS = 30 * 60
goes_history_cache = {"payload": None, "fetched_at": 0.0, "last_error": None}


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip().casefold()


def maybe_decompress(raw, content_encoding=""):
    if not raw:
        return raw
    if "gzip" in content_encoding.lower() or raw.startswith(b"\x1f\x8b"):
        return gzip.decompress(raw)
    if "deflate" in content_encoding.lower():
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    # The SMN endpoint may return a compressed body as application/octet-stream.
    if raw[:1] in (b"{", b"["):
        return raw
    for window_bits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(raw, window_bits)
        except zlib.error:
            pass
    return raw


def collect_records(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    if "nmun" in value or "municipio" in value:
        return [value]
    for key in ("data", "datos", "pronostico", "pronóstico", "resultados"):
        if key in value:
            found = collect_records(value[key])
            if found:
                return found
    for nested in value.values():
        found = collect_records(nested)
        if found:
            return found
    return []


def number(value):
    try:
        text = str(value).strip().replace(",", ".")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def display_time(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})?", text)
    if match:
        year, month, day, hour, minute = match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute or '00'}:00"
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})", text)
    if match:
        year, month, day, hour = match.groups()
        return f"{year}-{month}-{day}T{hour}:00:00"
    return text


def rss_local_name(tag):
    return str(tag).rsplit("}", 1)[-1].casefold()


def rss_child_text(element, name):
    for child in element:
        if rss_local_name(child.tag) == name.casefold():
            return "".join(child.itertext()).strip()
    return ""


def rss_descendant_text(element, names):
    names = {name.casefold() for name in names}
    for child in element.iter():
        if child is not element and rss_local_name(child.tag) in names:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def parse_coordinate(value, negative_directions=()):
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?\s*°?\s*([NSEWO])?", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    coordinate = number(re.match(r"[-+]?\d+(?:[.,]\d+)?", match.group(0)).group(0))
    direction = (match.group(1) or "").upper()
    if coordinate is not None and direction:
        coordinate = -abs(coordinate) if direction in negative_directions else abs(coordinate)
    return coordinate


def labeled_coordinate(text, labels, negative_directions=()):
    match = re.search(
        rf"\b(?:{labels})\b\s*[:=]?\s*([-+]?\d+(?:[.,]\d+)?)\s*(?:°|º)?\s*([NSEWO])?",
        str(text or ""), re.IGNORECASE,
    )
    if not match:
        return None
    coordinate = number(match.group(1))
    direction = (match.group(2) or "").upper()
    if coordinate is not None and direction:
        coordinate = -abs(coordinate) if direction in negative_directions else abs(coordinate)
    return coordinate


def clean_rss_description(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"(?is)<br\s*/?>", " ", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:500]


def normalize_ssn_event(item):
    title = rss_child_text(item, "title")
    if not title:
        return None
    pub_date = rss_child_text(item, "pubDate")
    try:
        event_time = parsedate_to_datetime(pub_date).isoformat(timespec="seconds") if pub_date else ""
    except (TypeError, ValueError, OverflowError):
        event_time = pub_date
    link = rss_child_text(item, "link") or rss_child_text(item, "guid")
    parsed_link = urlsplit(link)
    allowed_hosts = {"www.ssn.unam.mx", "ssn.unam.mx", "www2.ssn.unam.mx"}
    if parsed_link.scheme not in {"http", "https"} or parsed_link.hostname not in allowed_hosts:
        link = SSN_CATALOG_URL
    match = re.match(r"\s*(?:M\s*)?(\d+(?:[.,]\d+)?)\s*,", title, re.IGNORECASE)
    if not match:
        match = re.search(r"\bM(?:agnitud)?\s*[:=]?\s*(\d+(?:[.,]\d+)?)", title, re.IGNORECASE)
    magnitude = number(match.group(1)) if match else None
    raw_description = rss_child_text(item, "description")
    description = clean_rss_description(raw_description)
    lat = parse_coordinate(rss_descendant_text(item, ("lat", "latitude", "latitud")), ("S",))
    lon = parse_coordinate(rss_descendant_text(item, ("lon", "long", "longitude", "longitud")), ("W", "O"))
    point = rss_descendant_text(item, ("point",))
    point_match = re.match(r"\s*([-+]?\d+(?:[.,]\d+)?)\s*[,;\s]+\s*([-+]?\d+(?:[.,]\d+)?)\s*", point)
    if point_match:
        lat = lat if lat is not None else number(point_match.group(1))
        lon = lon if lon is not None else number(point_match.group(2))
    coordinate_text = f"{description} {title}"
    if lat is None:
        lat = labeled_coordinate(coordinate_text, "latitud|latitude|lat", ("S",))
    if lon is None:
        lon = labeled_coordinate(coordinate_text, "longitud|longitude|lon|long", ("W", "O"))
    pair = re.search(
        r"\b(?:latitud\s+y\s+longitud|coordenadas?|coordinates?)\b\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)\s*[,;/]\s*([-+]?\d+(?:[.,]\d+)?)",
        coordinate_text, re.IGNORECASE,
    )
    if pair:
        lat = lat if lat is not None else number(pair.group(1))
        lon = lon if lon is not None else number(pair.group(2))
    if lat is not None and not -90 <= lat <= 90:
        lat = None
    if lon is not None and not -180 <= lon <= 180:
        lon = None
    depth = parse_coordinate(rss_descendant_text(item, ("depth", "profundidad")))
    if depth is None:
        depth_match = re.search(r"\b(?:profundidad|depth)\b\s*[:=]?\s*([\d.,]+)\s*(?:km)?", description, re.IGNORECASE)
        depth = number(depth_match.group(1)) if depth_match else None
    return {
        "titulo": title[:240],
        "magnitud": magnitude,
        "fecha_hora": event_time,
        "descripcion": description,
        "lat": lat,
        "lon": lon,
        "profundidad_km": depth,
        "url": link,
    }


def fetch_ssn():
    request = urllib.request.Request(
        SSN_RSS_URL,
        headers={
            "User-Agent": "EntornoSeguroGDL/1.0 (SSN recent-event monitor)",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
    root = ET.fromstring(raw)
    events = [normalized for item in root.iter() if rss_local_name(item.tag) == "item"
              if (normalized := normalize_ssn_event(item))]
    if not events:
        raise ValueError("El feed del SSN respondió, pero no contiene reportes de sismos")
    events = events[:20]
    magnitudes = [event["magnitud"] for event in events if event["magnitud"] is not None]
    return {
        "ok": True,
        "fuente": "Servicio Sismológico Nacional / UNAM",
        "url_fuente": SSN_RSS_URL,
        "url_catalogo": SSN_CATALOG_URL,
        "alcance": "Reportes recientes de México; no es un pronóstico ni un índice de riesgo por colonia.",
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(events),
        "magnitud_maxima": max(magnitudes) if magnitudes else None,
        "eventos": events,
        "stale": False,
    }


def get_ssn_events():
    now = time.time()
    if ssn_cache["payload"] and now - ssn_cache["fetched_at"] < SSN_CACHE_SECONDS:
        return ssn_cache["payload"]
    try:
        payload = fetch_ssn()
        ssn_cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        ssn_cache["last_error"] = str(error)
        if ssn_cache["payload"] and now - ssn_cache["fetched_at"] <= SSN_STALE_MAX_SECONDS:
            stale = dict(ssn_cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo actualizar; se muestran reportes guardados temporalmente."
            stale["edad_minutos"] = round((now - ssn_cache["fetched_at"]) / 60)
            return stale
        return {
            "ok": False,
            "fuente": "Servicio Sismológico Nacional / UNAM",
            "url_fuente": SSN_RSS_URL,
            "url_catalogo": SSN_CATALOG_URL,
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "eventos": [],
            "stale": False,
        }


def normalize_record(row):
    time_value = row.get("hloc") or row.get("dloc") or ""
    return {
        "estado": str(row.get("nes") or row.get("estado") or "Jalisco").strip(),
        "municipio": str(row.get("nmun") or row.get("municipio") or "Guadalajara").strip(),
        "fecha_hora": display_time(time_value),
        "descripcion": str(row.get("desciel") or "Sin descripción").strip(),
        "probabilidad_lluvia": number(row.get("probprec")),
        "precipitacion_mm": number(row.get("prec")),
        "viento_kmh": number(row.get("velvien")),
        "rafaga_kmh": number(row.get("raf")),
        "temperatura_c": number(row.get("temp")),
        "temperatura_min_c": number(row.get("tmin")),
        "temperatura_max_c": number(row.get("tmax")),
        "humedad_pct": number(row.get("hr")),
        "nubes_pct": number(row.get("cc")),
        "lat": number(row.get("lat")),
        "lon": number(row.get("lon")),
    }


def fetch_smn():
    request = urllib.request.Request(
        SMN_URL,
        headers={
            "User-Agent": "EntornoSeguroGDL/1.0 (SMN municipal forecast integration)",
            "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = maybe_decompress(response.read(), response.headers.get("Content-Encoding", ""))
    payload = json.loads(raw.decode("utf-8-sig"))
    rows = collect_records(payload)
    matches = [
        row for row in rows
        if normalize(row.get("nmun") or row.get("municipio")) == MUNICIPALITY_NAME
        and normalize(row.get("nes") or row.get("estado")) == STATE_NAME
    ]
    if not matches:
        raise ValueError("El SMN respondió, pero no se encontró Guadalajara, Jalisco en method=3")
    records = [normalize_record(row) for row in matches]
    records.sort(key=lambda row: row["fecha_hora"] or "")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "ok": True,
        "fuente": "Servicio Meteorológico Nacional / CONAGUA",
        "url_fuente": SMN_URL,
        "estado": "Jalisco",
        "municipio": "Guadalajara",
        "periodo": "Pronóstico municipal por hora, hasta 48 horas",
        "consultado_en": now,
        "registros": records,
        "stale": False,
    }


def get_forecast():
    now = time.time()
    if cache["payload"] and now - cache["fetched_at"] < CACHE_SECONDS:
        return cache["payload"]
    try:
        payload = fetch_smn()
        cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        cache["last_error"] = str(error)
        if cache["payload"] and now - cache["fetched_at"] <= STALE_MAX_SECONDS:
            stale = dict(cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo actualizar; se muestran datos guardados temporalmente."
            stale["edad_minutos"] = round((now - cache["fetched_at"]) / 60)
            return stale
        return {
            "ok": False,
            "fuente": "Servicio Meteorológico Nacional / CONAGUA",
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "registros": [],
            "stale": False,
        }


def fetch_radar_metadata():
    request = urllib.request.Request(
        RAINVIEWER_URL,
        headers={"User-Agent": "EntornoSeguroGDL/1.0 (weather radar map)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    host = str(payload.get("host") or "").rstrip("/")
    if urlsplit(host).scheme != "https" or urlsplit(host).hostname != "tilecache.rainviewer.com":
        raise ValueError("RainViewer respondió con un host de mosaicos no esperado")
    frames = payload.get("radar", {}).get("past", [])
    frames = [
        frame for frame in frames
        if isinstance(frame, dict)
        and re.fullmatch(r"/v2/radar/[A-Za-z0-9_-]+", str(frame.get("path", "")))
        and number(frame.get("time")) is not None
    ]
    if not frames:
        raise ValueError("RainViewer no publicó imágenes de radar disponibles")
    frames = sorted(frames, key=lambda item: number(item.get("time")) or 0)[-13:]
    frame = frames[-1]
    frame_time = int(frame["time"])
    return {
        "ok": True,
        "fuente": "RainViewer",
        "url_fuente": RAINVIEWER_URL,
        "tile_host": host,
        "frame_path": frame["path"],
        "frame_epoch": frame_time,
        "frame_hora": datetime.fromtimestamp(frame_time, timezone.utc).isoformat(timespec="seconds"),
        "frames": [
            {"time": int(item["time"]), "path": item["path"]}
            for item in frames
        ],
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nota": "Radar observado; historial disponible en pasos aproximados de 10 minutos. No es un pronóstico y la cobertura depende de los radares disponibles.",
        "stale": False,
    }


def get_radar_metadata():
    now = time.time()
    if radar_cache["payload"] and now - radar_cache["fetched_at"] < RADAR_CACHE_SECONDS:
        return radar_cache["payload"]
    try:
        payload = fetch_radar_metadata()
        radar_cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        radar_cache["last_error"] = str(error)
        if radar_cache["payload"] and now - radar_cache["fetched_at"] <= RADAR_STALE_MAX_SECONDS:
            stale = dict(radar_cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo refrescar la capa de radar; se conserva el último mosaico disponible."
            stale["edad_minutos"] = round((now - radar_cache["fetched_at"]) / 60)
            return stale
        return {
            "ok": False,
            "fuente": "RainViewer",
            "url_fuente": RAINVIEWER_URL,
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": False,
        }


def fetch_tectonic_boundaries():
    params = urlencode({
        "where": "1=1",
        "outFields": "NAME,LABEL",
        "returnGeometry": "true",
        "geometry": "-120,12,-94,33",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outSR": "4326",
        "f": "geojson",
    })
    request = urllib.request.Request(
        f"{USGS_PLATE_QUERY_URL}?{params}",
        headers={"User-Agent": "EntornoSeguroGDL/1.0 (tectonic boundary map)", "Accept": "application/geo+json, application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    if payload.get("error"):
        raise ValueError(payload["error"].get("message", "Error en la consulta cartográfica de USGS"))
    if payload.get("type") != "FeatureCollection":
        raise ValueError("USGS respondió sin una colección GeoJSON de límites tectónicos")
    return {
        "ok": True,
        "fuente": "USGS · Tectonic Plate Boundaries",
        "url_fuente": USGS_PLATE_QUERY_URL,
        "alcance": "Límites tectónicos regionales; modelo geológico de referencia, no es una capa en tiempo real ni un pronóstico sísmico.",
        "geojson": payload,
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stale": False,
    }


def get_tectonic_boundaries():
    now = time.time()
    if plates_cache["payload"] and now - plates_cache["fetched_at"] < PLATE_CACHE_SECONDS:
        return plates_cache["payload"]
    try:
        payload = fetch_tectonic_boundaries()
        plates_cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        plates_cache["last_error"] = str(error)
        if plates_cache["payload"] and now - plates_cache["fetched_at"] <= PLATE_CACHE_SECONDS:
            stale = dict(plates_cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo actualizar el servicio; se muestran límites tectónicos guardados en caché."
            return stale
        return {
            "ok": False,
            "fuente": "USGS · Tectonic Plate Boundaries",
            "url_fuente": USGS_PLATE_QUERY_URL,
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": False,
        }


def fetch_inegi_boundaries():
    geometries = {}
    for name, endpoint in (("estado", "mgee/14"), ("municipios", "mgem/14")):
        request = urllib.request.Request(
            f"{INEGI_GEO_URL}/{endpoint}",
            headers={"User-Agent": "EntornoSeguroGDL/1.0 (INEGI geoestadistical map)", "Accept": "application/geo+json, application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
        if payload.get("error"):
            raise ValueError(payload["error"].get("message", "Error en la consulta cartográfica de INEGI"))
        if payload.get("type") != "FeatureCollection" or not payload.get("features"):
            raise ValueError(f"INEGI respondió sin geometrías GeoJSON para {name}")
        geometries[name] = payload
    return {
        "ok": True,
        "fuente": "INEGI · Marco Geoestadístico de Jalisco",
        "url_fuente": "https://www.inegi.org.mx/servicios/catalogounico.html",
        "alcance": "Límites geoestadísticos de Jalisco; sirven como referencia cartográfica y no constituyen un deslinde legal.",
        **geometries,
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stale": False,
    }


def get_inegi_boundaries():
    now = time.time()
    if admin_cache["payload"] and now - admin_cache["fetched_at"] < ADMIN_CACHE_SECONDS:
        return admin_cache["payload"]
    try:
        payload = fetch_inegi_boundaries()
        admin_cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        admin_cache["last_error"] = str(error)
        if admin_cache["payload"]:
            stale = dict(admin_cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo actualizar INEGI; se conservan los últimos límites geoestadísticos guardados en caché."
            stale["edad_horas"] = round((now - admin_cache["fetched_at"]) / 3600, 1)
            return stale
        return {
            "ok": False,
            "fuente": "INEGI · Marco Geoestadístico de Jalisco",
            "url_fuente": "https://www.inegi.org.mx/servicios/catalogounico.html",
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": False,
        }


def arcgis_time_millis(value):
    parsed = number(value)
    if parsed is not None:
        return int(parsed if parsed > 100_000_000_000 else parsed * 1000)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed_date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        return int(parsed_date.timestamp() * 1000)
    except ValueError:
        return None


def iso_from_millis(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")


def fetch_goes_metadata():
    request = urllib.request.Request(
        f"{GOES_IMAGE_SERVICE_URL}?f=json",
        headers={"User-Agent": "EntornoSeguroGDL/1.0 (NOAA GOES satellite imagery)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    time_info = payload.get("timeInfo") or {}
    extent = time_info.get("timeExtent") or []
    if len(extent) < 2:
        raise ValueError("El servicio GOES de NOAA no publicó la hora de adquisición más reciente")
    start_ms, end_ms = (arcgis_time_millis(value) for value in extent[:2])
    if start_ms is None or end_ms is None:
        raise ValueError("El servicio GOES de NOAA devolvió una hora de imagen no válida")
    return {
        "ok": True,
        "fuente": "NOAA / NESDIS · GOES GeoColor",
        "url_fuente": GOES_IMAGE_SERVICE_URL,
        "inicio_epoch_ms": start_ms,
        "fin_epoch_ms": end_ms,
        "inicio_utc": iso_from_millis(start_ms),
        "fin_utc": iso_from_millis(end_ms),
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nota": "Imagen GeoColor del mosaico GOES más reciente; representa nubosidad y superficie, no un pronóstico.",
        "stale": False,
    }


def get_goes_metadata():
    now = time.time()
    if goes_cache["payload"] and now - goes_cache["fetched_at"] < GOES_CACHE_SECONDS:
        return goes_cache["payload"]
    try:
        payload = fetch_goes_metadata()
        goes_cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        goes_cache["last_error"] = str(error)
        if goes_cache["payload"] and now - goes_cache["fetched_at"] <= GOES_STALE_MAX_SECONDS:
            stale = dict(goes_cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo actualizar la hora GOES; se conserva temporalmente la última imagen conocida."
            stale["edad_minutos"] = round((now - goes_cache["fetched_at"]) / 60)
            return stale
        return {
            "ok": False,
            "fuente": "NOAA / NESDIS · GOES GeoColor",
            "url_fuente": GOES_IMAGE_SERVICE_URL,
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": False,
        }


def fetch_goes_history():
    query = urlencode({
        "where": "1=1",
        "outFields": "Start_Time,End_Time",
        "returnGeometry": "false",
        "orderByFields": "End_Time ASC",
        "f": "json",
    })
    request = urllib.request.Request(
        f"{GOES_ARCHIVE_SERVICE_URL}/query?{query}",
        headers={"User-Agent": "EntornoSeguroGDL/1.0 (NOAA GOES satellite imagery)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    if payload.get("error"):
        raise ValueError(payload["error"].get("message") or "NOAA rechazó la consulta del historial GOES")
    frames = []
    for feature in payload.get("features") or []:
        attributes = feature.get("attributes") or {}
        start_ms = arcgis_time_millis(attributes.get("Start_Time", attributes.get("start_time")))
        end_ms = arcgis_time_millis(attributes.get("End_Time", attributes.get("end_time")))
        if start_ms is None or end_ms is None or end_ms < start_ms:
            continue
        frames.append({
            "inicio_epoch_ms": start_ms,
            "fin_epoch_ms": end_ms,
            "inicio_utc": iso_from_millis(start_ms),
            "fin_utc": iso_from_millis(end_ms),
        })
    frames.sort(key=lambda frame: frame["fin_epoch_ms"])
    if not frames:
        raise ValueError("El archivo NOAA no devolvió fotogramas GOES con horas válidas")
    return {
        "ok": True,
        "fuente": "NOAA / NESDIS · GOES GeoColor · archivo de 24 horas",
        "url_fuente": GOES_ARCHIVE_SERVICE_URL,
        "frames": frames,
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stale": False,
    }


def get_goes_history():
    now = time.time()
    if goes_history_cache["payload"] and now - goes_history_cache["fetched_at"] < GOES_HISTORY_CACHE_SECONDS:
        return goes_history_cache["payload"]
    try:
        payload = fetch_goes_history()
        goes_history_cache.update(payload=payload, fetched_at=now, last_error=None)
        return payload
    except Exception as error:
        goes_history_cache["last_error"] = str(error)
        if goes_history_cache["payload"] and now - goes_history_cache["fetched_at"] <= GOES_HISTORY_STALE_MAX_SECONDS:
            stale = dict(goes_history_cache["payload"])
            stale["stale"] = True
            stale["aviso"] = "No se pudo actualizar el archivo GOES; se conserva temporalmente el historial en caché."
            stale["edad_minutos"] = round((now - goes_history_cache["fetched_at"]) / 60)
            return stale
        return {
            "ok": False,
            "fuente": "NOAA / NESDIS · GOES GeoColor · archivo de 24 horas",
            "url_fuente": GOES_ARCHIVE_SERVICE_URL,
            "error": str(error),
            "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": False,
        }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/clima":
            payload = get_forecast()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/sismos":
            payload = get_ssn_events()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/radar":
            payload = get_radar_metadata()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/placas":
            payload = get_tectonic_boundaries()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/limites":
            payload = get_inegi_boundaries()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/satelite":
            payload = get_goes_metadata()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/satelite/historial":
            payload = get_goes_history()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload.get("ok") else 502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def main():
    parser = argparse.ArgumentParser(description="Servidor local del tablero con fuentes cacheadas del SMN, SSN y GOES")
    parser.add_argument("--host", default="127.0.0.1", help="Por defecto solo acepta conexiones locales")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el tablero automáticamente")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Tablero: http://{args.host}:{args.port}/index.html")
    print(f"Clima SMN: http://{args.host}:{args.port}/api/clima (caché de 15 minutos)")
    print(f"Sismos SSN: http://{args.host}:{args.port}/api/sismos (caché de 5 minutos)")
    print(f"Radar lluvia: http://{args.host}:{args.port}/api/radar (metadatos cacheados 5 minutos)")
    print(f"Placas: http://{args.host}:{args.port}/api/placas (límites USGS cacheados 24 horas)")
    print(f"Límites Jalisco: http://{args.host}:{args.port}/api/limites (geometrías INEGI cacheadas 24 horas)")
    print(f"Satélite GOES: http://{args.host}:{args.port}/api/satelite (hora de imagen cacheada 2 minutos)")
    print(f"Archivo GOES: http://{args.host}:{args.port}/api/satelite/historial (escenas cacheadas 5 minutos)")
    print("Presiona Ctrl+C para detener el servidor.")
    try:
        if not args.no_browser:
            webbrowser.open(f"http://{args.host}:{args.port}/index.html")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
