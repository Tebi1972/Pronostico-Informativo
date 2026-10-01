import json
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Pronostico-Informativo/1.0"}
UY_TZ = ZoneInfo("America/Montevideo")
URL_PRONOSTICO = "https://www.inumet.gub.uy/tiempo/pronostico"
URL_EXTENDIDO = "https://www.inumet.gub.uy/reportes/pronosticos/pronosticoV4.json"
URL_ESTADO = "https://www.inumet.gub.uy/index.php/tiempo/estado-actual"
API_OBSERVACIONES = (
    "https://w2b.inumet.gub.uy/oapi/collections/"
    "urn:wmo:md:uy-inumet:surface-based-observations.synop/items"
)
CARRASCO_WIGOS = "0-20000-0-86580"
ZONAS = {
    "E": "Este",
    "NW": "Norte",
    "M": "Sur",
    "SO": "Oeste",
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



# Identificadores WIGOS comprobados en Tiempo Uruguay.
ESTACIONES = {
    "Norte": ("0-20000-0-86330", "Artigas"),
    "Este": ("0-20000-0-86565", "Rocha"),
    "Sur": ("0-20000-0-86580", "Carrasco"),
    "Oeste": ("0-20000-0-86490", "Mercedes"),
}


def obtener_estado_actual_oficial():
    """Lee de INUMET el estado actual publicado para las cuatro estaciones.

    Se usa solamente como complemento del SYNOP para tiempo presente/nubosidad.
    Las temperaturas y demás variables continúan saliendo de la API SYNOP.
    """
    try:
        r = requests.get(URL_ESTADO, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        buscadas = {
            "Norte": "artigas",
            "Este": "rocha",
            "Sur": "carrasco",
            "Oeste": "mercedes",
        }
        salida = {}
        for tr in soup.find_all("tr"):
            c = [re.sub(r"\s+", " ", x.get_text(" ", strip=True)).strip()
                 for x in tr.find_all(["td", "th"])]
            if len(c) < 4:
                continue
            nombre = c[0].lower()
            zona = next((z for z, prefijo in buscadas.items() if nombre.startswith(prefijo)), None)
            if not zona:
                continue
            cielo = c[2] if len(c) > 2 and c[2] not in ("", "-") else None
            presente = c[3] if len(c) > 3 and c[3] not in ("", "-") else None
            salida[zona] = {"sky": cielo, "present_weather": presente}
        return salida
    except Exception as e:
        print("Aviso: no se pudo leer estado actual de INUMET:", e)
        return {}


def obtener_actuales():
    # Mismo procedimiento de Tiempo Uruguay: recorrer todas las páginas SYNOP,
    # identificar cada estación por WIGOS y elegir observaciones recientes.
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(hours=24)
    rango = desde.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" + ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    por_wigos = {meta[0]: [] for meta in ESTACIONES.values()}
    url, params, pagina = API_OBSERVACIONES, {"f": "json", "limit": 1000, "datetime": rango}, 1
    while url and pagina <= 20:
        r = requests.get(url, params=params, headers=HEADERS, timeout=60)
        r.raise_for_status()
        datos = r.json()
        for f in datos.get("features", []):
            prop = f.get("properties", {})
            wigos = str(prop.get("wigos_station_identifier"))
            if wigos in por_wigos and prop.get("phenomenonTime"):
                por_wigos[wigos].append(prop)
        url = next((x.get("href") for x in datos.get("links", []) if x.get("rel") == "next"), None)
        params = None
        pagina += 1
    print("Páginas SYNOP consultadas:", pagina - 1)

    def recientes(registros, nombre, max_horas=3):
        xs = [x for x in registros if x.get("name") == nombre and x.get("value") is not None
              and 0 <= edad_horas(instante(x), ahora) <= max_horas]
        xs.sort(key=instante, reverse=True)
        if not xs:
            return []
        ultima_hora = instante(xs[0])
        return [x for x in xs if instante(x) == ultima_hora]

    def descripcion(xs):
        return " | ".join(dict.fromkeys(str(x.get("description") or "").strip()
                                     for x in xs if x.get("description"))) or None

    def condicion(weather, clouds, totals):
        # No inferir lluvia a partir del pronóstico: solo del tiempo presente observado.
        texto = descripcion(weather)
        if texto and any(k in texto.upper() for k in (
            "RAIN", "DRIZZLE", "SHOWER", "THUNDER", "PRECIP", "HAIL", "SNOW",
            "LLUV", "LLOV", "LLOVIZ", "CHAPARR", "TORMENT", "GRANIZ", "NIEV")):
            return texto
        nube = descripcion(clouds)
        if nube:
            t = nube.upper()
            oktas = [int(x) for x in re.findall(r"(\d+)\s*OKTAS?", t)]
            if oktas:
                n = max(oktas)
                return "Cubierto" if n >= 7 else "Nuboso" if n >= 5 else "Algo nuboso" if n >= 3 else "Despejado"
            return nube
        if totals:
            try:
                n = float(totals[0]["value"])
                if n > 8: n = n * 8 / 100
                return "Cubierto" if n >= 7 else "Nuboso" if n >= 5 else "Algo nuboso" if n >= 3 else "Despejado"
            except (TypeError, ValueError):
                pass
        return texto

    estado_oficial = obtener_estado_actual_oficial()
    actuales = {}
    for zona, (wigos, estacion) in ESTACIONES.items():
        registros = por_wigos[wigos]
        temp = recientes(registros, "air_temperature")
        hum = recientes(registros, "relative_humidity")
        viento = recientes(registros, "wind_speed")
        direccion = recientes(registros, "wind_direction")
        vis = recientes(registros, "horizontal_visibility")
        weather = recientes(registros, "present_weather")
        clouds = recientes(registros, "cloud_amount")
        totals = recientes(registros, "cloud_cover_total")
        condicion_synop = condicion(weather, clouds, totals)
        oficial = estado_oficial.get(zona, {})
        # Prioridad: fenómeno observado; luego cielo SYNOP; si falta, tabla oficial
        # de estado actual de INUMET para esa misma estación.
        condicion_final = condicion_synop or oficial.get("present_weather") or oficial.get("sky")
        actuales[zona] = {
            "station": estacion,
            "wigos": wigos,
            "temperature": temp[0]["value"] if temp else None,
            "condition": condicion_final,
            "present_weather": descripcion(weather) or oficial.get("present_weather"),
            "cloud_amount": descripcion(clouds) or oficial.get("sky"),
            "observation_time": instante(temp[0]) if temp else None,
            "humidity": round(float(hum[0]["value"])) if hum else None,
            "wind_speed_kmh": round(float(viento[0]["value"]) * 3.6, 1) if viento else None,
            "wind_direction_deg": round(float(direccion[0]["value"])) if direccion else None,
            "visibility_m": round(float(vis[0]["value"])) if vis else None,
        }
        print(zona, estacion, "registros:", len(registros), "temperatura:",
              actuales[zona]["temperature"], "cielo:", actuales[zona]["condition"])
    if all(x["temperature"] is None for x in actuales.values()):
        raise RuntimeError("INUMET no devolvió temperaturas recientes para las cuatro estaciones")
    return actuales


def main():
    forecasts = obtener_pronosticos()
    actuales = obtener_actuales()
    carrasco = actuales["Sur"]
    salida = {
        "updated": datetime.now(UY_TZ).strftime("%d/%m/%Y %H:%M"),
        "source": "INUMET",
        "current": carrasco,
        "current_regions": actuales,
        "locations": {"montevideo_carrasco": carrasco},
        "forecasts": forecasts,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)
    print("data.json actualizado correctamente")


if __name__ == "__main__":
    main()
