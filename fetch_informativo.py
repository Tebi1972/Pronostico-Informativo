import json
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

HEADERS = {"User-Agent": "Pronostico-Informativo/1.0"}
UY_TZ = ZoneInfo("America/Montevideo")
URL_PRONOSTICO = "https://www.inumet.gub.uy/tiempo/pronostico"
URL_EXTENDIDO = "https://www.inumet.gub.uy/reportes/pronosticos/pronosticoV4.json"
API_OBSERVACIONES = (
    "https://w2b.inumet.gub.uy/oapi/collections/"
    "urn:wmo:md:uy-inumet:surface-based-observations.synop/items"
)
CARRASCO_WIGOS = "0-20000-0-86580"
ZONAS = {
    "E": "Este",
    "NE": "Noreste",
    "NW": "Noroeste",
    "M": "Área Metropolitana",
    "SO": "Suroeste",
}


def extraer_array_js(html, codigo):
    patron = re.compile(
        r'pronosticos\s*\[\s*["\']' + re.escape(codigo) + r'["\']\s*\]\s*=\s*',
        re.I,
    )
    m = patron.search(html)
    if not m:
        return None
    inicio = html.find("[", m.end())
    if inicio < 0:
        return None
    nivel = 0
    comilla = None
    escape = False
    for i in range(inicio, len(html)):
        ch = html[i]
        if comilla:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == comilla:
                comilla = None
            continue
        if ch in ('"', "'"):
            comilla = ch
        elif ch == "[":
            nivel += 1
        elif ch == "]":
            nivel -= 1
            if nivel == 0:
                return html[inicio:i + 1]
    return None


def unir_textos(*partes):
    return " ".join(str(x).strip() for x in partes if str(x or "").strip()).strip() or None


def convertir_periodo(periodo):
    datos = (periodo or {}).get("datos") or {}
    subs = datos.get("subgrupos") or []
    manana = next((x for x in subs if str(x.get("subgrupo", "")).lower().startswith("mañ")), None)
    tarde = next((x for x in subs if "tarde" in str(x.get("subgrupo", "")).lower()), None)

    def texto(sg):
        if not sg:
            return None
        return unir_textos(sg.get("descripcion"), sg.get("evolucion"), sg.get("descripcionExtra"))

    fecha = datos.get("grupo") or datos.get("grupoCorto")
    minimo = datos.get("tempMin")
    maximo = datos.get("tempMax")
    if fecha is None or minimo is None or maximo is None:
        return None
    return {
        "date": str(fecha),
        "min": str(minimo),
        "max": str(maximo),
        "morning": texto(manana),
        "evening": texto(tarde),
        "extended": False,
        "weather_code": datos.get("estadoTiempo"),
    }


def texto_extendido(item):
    subs = item.get("subgrupos") or []
    textos = []
    if isinstance(subs, list):
        for sg in subs:
            if isinstance(sg, dict):
                t = unir_textos(sg.get("descripcion"), sg.get("evolucion"), sg.get("descripcionExtra"))
                if t:
                    textos.append(t)
            elif sg:
                textos.append(str(sg).strip())
    if textos:
        return " ".join(dict.fromkeys(textos))

    for clave in (
        "probabilidadPrecipitaciones", "probabilidadPrecipitacion",
        "probPrecipitaciones", "probPrecipitacion", "probLluvia",
        "precipitaciones", "precipitacion",
    ):
        valor = item.get(clave)
        if valor not in (None, ""):
            bajo = str(valor).strip().lower()
            if bajo in {"nula", "ninguna", "0", "0%"}:
                return "Sin precipitaciones previstas"
            if bajo in {"baja", "bajo"}:
                return "Baja probabilidad de precipitaciones"
            if bajo in {"media", "moderada", "moderado"}:
                return "Probabilidad media de precipitaciones"
            if bajo in {"alta", "alto"}:
                return "Alta probabilidad de precipitaciones"
            return str(valor).strip()
    return "Pronóstico extendido"


def convertir_extendido(item):
    fecha = item.get("grupo") or item.get("grupoCorto")
    minimo = item.get("tempMin")
    maximo = item.get("tempMax")
    if fecha is None or minimo is None or maximo is None:
        return None
    return {
        "date": str(fecha),
        "min": str(minimo),
        "max": str(maximo),
        "morning": None,
        "evening": texto_extendido(item),
        "extended": True,
        "weather_code": item.get("estadoTiempo"),
    }


def obtener_pronosticos():
    r = requests.get(URL_PRONOSTICO, headers=HEADERS, timeout=30)
    r.raise_for_status()
    forecasts = {}
    for codigo, nombre in ZONAS.items():
        bruto = extraer_array_js(r.text, codigo)
        if not bruto:
            print("Zona no encontrada:", codigo)
            continue
        try:
            periodos = json.loads(bruto)
        except Exception as e:
            print("JSON inválido en zona", codigo, e)
            continue
        dias = []
        for periodo in periodos:
            dia = convertir_periodo(periodo)
            if dia:
                dias.append(dia)
            if len(dias) >= 3:
                break
        if dias:
            forecasts[codigo] = {"name": nombre, "days": dias}

    faltantes = [z for z in ZONAS if z not in forecasts]
    if faltantes:
        raise RuntimeError("Faltan zonas del pronóstico INUMET: " + ", ".join(faltantes))

    try:
        e = requests.get(URL_EXTENDIDO, headers=HEADERS, timeout=30)
        e.raise_for_status()
        datos = e.json()
        items = datos.get("items", []) if isinstance(datos, dict) else []
        por_zona = {z: [] for z in ZONAS}
        for item in items:
            if not isinstance(item, dict):
                continue
            zona = str(item.get("zonaCorta") or "").upper().strip()
            if zona == "SW":
                zona = "SO"
            if zona not in por_zona:
                continue
            try:
                n = int(item.get("diaMasN"))
            except (TypeError, ValueError):
                continue
            if n >= 3:
                por_zona[zona].append((n, item))

        for zona, pares in por_zona.items():
            existentes = {d["date"].strip().lower() for d in forecasts[zona]["days"]}
            for _, item in sorted(pares, key=lambda x: x[0]):
                dia = convertir_extendido(item)
                if not dia:
                    continue
                clave = dia["date"].strip().lower()
                if clave not in existentes:
                    forecasts[zona]["days"].append(dia)
                    existentes.add(clave)
                if len(forecasts[zona]["days"]) >= 4:
                    break
    except Exception as e:
        print("Aviso: no se pudo completar pronóstico extendido:", e)

    for zona in ZONAS:
        print(zona, "->", len(forecasts[zona]["days"]), "días")
    return forecasts


def instante(p):
    return str(p.get("phenomenonTime") or "").split("/")[0]


def edad_horas(iso, ahora):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (ahora - dt).total_seconds() / 3600
    except Exception:
        return 999


def obtener_carrasco():
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(hours=24)
    rango = desde.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" + ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    # BBOX pequeño alrededor de Carrasco: reduce drásticamente la respuesta.
    params = {
        "f": "json", "limit": 1000, "datetime": rango,
        "bbox": "-56.08,-34.90,-55.95,-34.77",
    }
    registros = []
    url = API_OBSERVACIONES
    pagina = 1
    while url and pagina <= 10:
        r = requests.get(url, params=params, headers=HEADERS, timeout=60)
        r.raise_for_status()
        datos = r.json()
        for f in datos.get("features", []):
            p = f.get("properties", {})
            if str(p.get("wigos_station_identifier")) == CARRASCO_WIGOS:
                registros.append(p)
        siguiente = next((x.get("href") for x in datos.get("links", []) if x.get("rel") == "next"), None)
        url, params = siguiente, None
        pagina += 1

    if not registros:
        raise RuntimeError("INUMET no devolvió observaciones de Carrasco")

    def ultimo(nombre, max_horas=3):
        xs = [p for p in registros if p.get("name") == nombre and p.get("value") is not None]
        xs.sort(key=instante, reverse=True)
        for p in xs:
            if edad_horas(instante(p), ahora) <= max_horas:
                return p
        return None

    temp = ultimo("air_temperature")
    hum = ultimo("relative_humidity")
    viento = ultimo("wind_speed")
    dir_viento = ultimo("wind_direction")
    vis = ultimo("horizontal_visibility")

    # La hora principal es la de la temperatura; si falta, la del dato más reciente.
    horas = [instante(x) for x in (temp, hum, viento, dir_viento, vis) if x and instante(x)]
    obs_time = instante(temp) if temp else (max(horas) if horas else None)

    return {
        "temperature": temp.get("value") if temp else None,
        "station": "Carrasco",
        "wigos": CARRASCO_WIGOS,
        "observation_time": obs_time,
        "condition_time": obs_time,
        "humidity": round(float(hum["value"])) if hum else None,
        "wind_speed_kmh": round(float(viento["value"]) * 3.6, 1) if viento else None,
        "wind_direction_deg": round(float(dir_viento["value"])) if dir_viento else None,
        "visibility_m": round(float(vis["value"])) if vis else None,
        "forecast_zone": "M",
    }


def main():
    forecasts = obtener_pronosticos()
    carrasco = obtener_carrasco()
    salida = {
        "updated": datetime.now(UY_TZ).strftime("%d/%m/%Y %H:%M"),
        "source": "INUMET",
        "locations": {"montevideo_carrasco": carrasco},
        "current": carrasco,
        "forecasts": forecasts,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)
    print("data.json actualizado correctamente")
    print("Carrasco:", carrasco)


if __name__ == "__main__":
    main()
