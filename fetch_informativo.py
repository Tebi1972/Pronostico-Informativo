import requests
import re
import json
import math
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

HEADERS = {"User-Agent": "Tiempo-Uruguay/1.0"}
URUGUAY_TZ = ZoneInfo("America/Montevideo")

# V4.12.2: conservar como respaldo la última observación publicada.
# Se usa únicamente cuando una nueva consulta de la API omite transitoriamente
# la temperatura de una estación y la observación anterior sigue teniendo
# menos de 2 horas. Nunca prolonga una observación ya vencida.
try:
    with open("data.json", "r", encoding="utf-8") as _f:
        PREVIOUS_DATA = json.load(_f)
except (OSError, json.JSONDecodeError):
    PREVIOUS_DATA = {}

# =================================================
# 1. PRONÓSTICO INUMET - ÁREA METROPOLITANA
# =================================================
URL_PRONOSTICO = "https://www.inumet.gub.uy/tiempo/pronostico"
r = requests.get(URL_PRONOSTICO, headers=HEADERS, timeout=30)
r.raise_for_status()
soup = BeautifulSoup(r.text, "html.parser")
texto = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

patron = re.compile(
    r"(Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo)"
    r"\s+(\d{1,2})"
    r".{0,100}?"
    r"Temp\.\s*min\s*(\d{1,2})\s*°C"
    r"\s*máx\s*(\d{1,2})\s*°C",
    re.IGNORECASE,
)

matches = list(patron.finditer(texto))
days, vistos = [], set()
for i, m in enumerate(matches):
    fecha = f"{m.group(1)} {m.group(2)}"
    if fecha in vistos:
        continue
    vistos.add(fecha)
    inicio = m.end()
    fin = matches[i + 1].start() if i + 1 < len(matches) else len(texto)
    bloque = texto[inicio:fin]

    manana = re.search(r"Mañana\s+(.*?)(?=\s+Viento:|\s+Tarde/Noche)", bloque, re.IGNORECASE)
    tarde = re.search(r"Tarde/Noche\s+(.*?)(?=\s+Viento:|$)", bloque, re.IGNORECASE)
    vientos = re.findall(r"Viento:\s*(.*?)(?=\s+(?:Tarde/Noche|Mañana)|$)", bloque, re.IGNORECASE)

    morning = manana.group(1).strip() if manana else None
    evening = tarde.group(1).strip() if tarde else None
    wind = " ".join(vientos) if vientos else "—"
    lluvia = "Precipitaciones" if re.search(r"precipit|lluvia|chaparr", bloque, re.IGNORECASE) else "No indicada"

    days.append({
        "date": fecha,
        "min": m.group(3),
        "max": m.group(4),
        "morning": morning,
        "evening": evening,
        "wind": wind,
        "rain": lluvia,
    })
    if len(days) == 3:
        break

if not days:
    raise SystemExit("No se encontraron temperaturas del pronóstico.")


# =================================================
# 1B. PRONÓSTICOS REGIONALES - 7 ZONAS INUMET
# =================================================
ZONE_NAMES = {
    "M": "Área Metropolitana",
    "NW": "Noroeste",
    "NE": "Noreste",
    "SO": "Suroeste",
    "C": "Centro-Sur",
    "E": "Este",
    "PE": "Punta del Este",
}

def extraer_array_js_asignado(html, codigo):
    # INUMET publica literalmente:
    # pronosticos["NW"] = [{...},{...},...];
    patron = re.compile(
        r'pronosticos\s*\[\s*["\']' + re.escape(codigo) + r'["\']\s*\]\s*=\s*',
        re.I
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

def texto_subgrupo(sg):
    partes = [
        str(sg.get("descripcion") or "").strip(),
        str(sg.get("evolucion") or "").strip(),
        str(sg.get("descripcionExtra") or "").strip(),
    ]
    return " ".join(x for x in partes if x).strip() or None

def convertir_periodo(periodo):
    datos = (periodo or {}).get("datos") or {}
    subs = datos.get("subgrupos") or []

    manana = next(
        (x for x in subs if str(x.get("subgrupo", "")).lower().startswith("mañ")),
        None
    )
    tarde = next(
        (x for x in subs if "tarde" in str(x.get("subgrupo", "")).lower()),
        None
    )

    morning = texto_subgrupo(manana) if manana else None
    evening = texto_subgrupo(tarde) if tarde else None

    vientos = []
    for sg in subs:
        v = str(sg.get("vientos") or "").strip()
        if v:
            vientos.append(v)
    wind = " ".join(vientos) if vientos else "—"

    texto_lluvia = " ".join(
        str(x or "") for x in [
            morning,
            evening,
            datos.get("descripcion"),
            datos.get("evolucion"),
        ]
    )
    rain = "Precipitaciones" if re.search(
        r"precipit|lluvia|chaparr|torment", texto_lluvia, re.I
    ) else "No indicada"

    fecha = datos.get("grupo") or datos.get("grupoCorto")
    minimo = datos.get("tempMin")
    maximo = datos.get("tempMax")

    if fecha is None or minimo is None or maximo is None:
        return None

    return {
        "date": str(fecha),
        "min": str(minimo),
        "max": str(maximo),
        "morning": morning,
        "evening": evening,
        "wind": wind,
        "rain": rain,
    }

forecasts = {}

for codigo, nombre in ZONE_NAMES.items():
    bruto = extraer_array_js_asignado(r.text, codigo)
    if not bruto:
        print("Zona no encontrada en HTML:", codigo)
        continue

    try:
        periodos = json.loads(bruto)
    except Exception as e:
        print("Error JSON zona", codigo, ":", e)
        continue

    dias_zona = []
    for periodo in periodos:
        dia = convertir_periodo(periodo)
        if dia:
            dias_zona.append(dia)
        if len(dias_zona) == 3:
            break

    if dias_zona:
        forecasts[codigo] = {
            "name": nombre,
            "days": dias_zona,
        }

# Red de seguridad: nunca dejamos a Montevideo sin el pronóstico histórico.
if "M" not in forecasts:
    forecasts["M"] = {
        "name": ZONE_NAMES["M"],
        "days": days,
    }

print("Zonas regionales extraídas:", ", ".join(forecasts.keys()))
for codigo in ZONE_NAMES:
    if codigo in forecasts:
        muestra = forecasts[codigo]["days"][0]
        print(
            codigo, "->", len(forecasts[codigo]["days"]), "días |",
            muestra["date"], "|",
            muestra["min"], "/", muestra["max"], "|",
            muestra["morning"]
        )
    else:
        print(codigo, "-> NO EXTRAÍDA")


# =================================================
# 1C. PRONÓSTICO EXTENDIDO INUMET
#     Completa los 3 días FUTUROS que muestra la app.
# =================================================
# El pronóstico corto de INUMET contiene el día actual + dos días más.
# INUMET publica aparte un JSON extendido. Tomamos desde diaMasN >= 3 y
# lo anexamos por zona, sin sustituir ni inventar el pronóstico corto.
URL_PRONOSTICO_EXTENDIDO = (
    "https://www.inumet.gub.uy/reportes/pronosticos/pronosticoV4.json"
)

def _valor_extendido(item, nombres):
    """Busca un campo directo tolerando pequeñas variaciones de nombre."""
    for nombre in nombres:
        if nombre in item and item.get(nombre) not in (None, ""):
            return item.get(nombre)

    normalizados = {
        re.sub(r"[^a-z0-9]", "", str(k).lower()): v
        for k, v in item.items()
    }
    for nombre in nombres:
        clave = re.sub(r"[^a-z0-9]", "", nombre.lower())
        if clave in normalizados and normalizados[clave] not in (None, ""):
            return normalizados[clave]
    return None

def _texto_precipitacion_extendida(item):
    # El JSON ha cambiado de forma en distintas versiones del sitio.
    # Primero probamos nombres esperables y luego cualquier clave relacionada
    # con probabilidad/precipitación.
    candidatos = [
        "probabilidadPrecipitaciones",
        "probabilidadPrecipitacion",
        "probPrecipitaciones",
        "probPrecipitacion",
        "precipitaciones",
        "precipitacion",
        "probLluvia",
    ]
    valor = _valor_extendido(item, candidatos)

    if valor in (None, ""):
        for clave, posible in item.items():
            nombre = str(clave).lower()
            if ("precip" in nombre or "lluv" in nombre) and posible not in (None, ""):
                valor = posible
                break

    if valor in (None, ""):
        return "Pronóstico extendido"

    texto_valor = str(valor).strip()
    bajo = texto_valor.lower()
    if bajo in {"nula", "ninguna", "0", "0%"}:
        return "Sin precipitaciones previstas"
    if bajo in {"baja", "bajo"}:
        return "Baja probabilidad de precipitaciones"
    if bajo in {"media", "moderada", "moderado"}:
        return "Probabilidad media de precipitaciones"
    if bajo in {"alta", "alto"}:
        return "Alta probabilidad de precipitaciones"

    if "precip" in bajo or "lluv" in bajo:
        return texto_valor
    return f"Probabilidad de precipitaciones: {texto_valor}"

def _convertir_extendido(item):
    fecha = _valor_extendido(item, ["grupo", "grupoCorto", "fecha"])
    minimo = _valor_extendido(
        item, ["tempMin", "temperaturaMinima", "temperatura_minima", "min"]
    )
    maximo = _valor_extendido(
        item, ["tempMax", "temperaturaMaxima", "temperatura_maxima", "max"]
    )

    if fecha in (None, "") or minimo in (None, "") or maximo in (None, ""):
        return None

    detalle = _texto_precipitacion_extendida(item)
    return {
        "date": str(fecha),
        "min": str(minimo),
        "max": str(maximo),
        "morning": None,
        "evening": detalle,
        "wind": "—",
        "rain": detalle,
        "extended": True,
        "weather_code": item.get("estadoTiempo"),
    }

def _anexar_sin_duplicados(base, nuevos, limite=7):
    salida = list(base or [])
    fechas = {str(x.get("date", "")).strip().lower() for x in salida}
    for dia in nuevos:
        fecha = str(dia.get("date", "")).strip().lower()
        if not fecha or fecha in fechas:
            continue
        salida.append(dia)
        fechas.add(fecha)
        if len(salida) >= limite:
            break
    return salida

try:
    respuesta_ext = requests.get(
        URL_PRONOSTICO_EXTENDIDO, headers=HEADERS, timeout=30
    )
    print("Código respuesta pronóstico extendido:", respuesta_ext.status_code)
    respuesta_ext.raise_for_status()
    datos_ext = respuesta_ext.json()
    items_ext = datos_ext.get("items", []) if isinstance(datos_ext, dict) else []

    print("Ítems de pronóstico extendido recibidos:", len(items_ext))
    for item in items_ext:
    if item.get("diaMasN") == 3:
        print(
            "EXTENDIDO DÍA 3:",
            item.get("zonaCorta"),
            "| estadoTiempo:", item.get("estadoTiempo"),
            "| min:", item.get("tempMin"),
            "| max:", item.get("tempMax"),
            "| grupo:", item.get("grupo")
        )
    if items_ext:
        print("Campos del primer ítem extendido:", sorted(items_ext[0].keys()))

    por_zona_ext = {}
    for item in items_ext:
        if not isinstance(item, dict):
            continue
        zona = str(item.get("zonaCorta") or "").strip().upper()
        if zona == "SW":
            zona = "SO"
        try:
            dia_mas_n = int(item.get("diaMasN"))
        except Exception:
            dia_mas_n = -1
        if not zona or dia_mas_n < 3:
            continue
        por_zona_ext.setdefault(zona, []).append((dia_mas_n, item))

    for codigo, pares in por_zona_ext.items():
        if codigo not in forecasts:
            continue
        convertidos = []
        for _, item in sorted(pares, key=lambda x: x[0]):
            dia = _convertir_extendido(item)
            if dia:
                convertidos.append(dia)
        forecasts[codigo]["days"] = _anexar_sin_duplicados(
            forecasts[codigo].get("days", []), convertidos
        )

    # El campo histórico `days` representa Montevideo; lo dejamos también
    # completo para compatibilidad con versiones anteriores del index.
    if "M" in forecasts:
        days = list(forecasts["M"]["days"])

    for codigo in ZONE_NAMES:
        if codigo in forecasts:
            print(
                "Pronóstico total", codigo, "->",
                len(forecasts[codigo].get("days", [])), "días"
            )

except Exception as error:
    # Si falla el extendido, la actualización principal sigue funcionando.
    # La interfaz mostrará únicamente los días oficiales disponibles.
    print("Error obteniendo pronóstico extendido:", error)


# =================================================
# 2. OBSERVACIONES REALES - RED NACIONAL SYNOP
# =================================================
API_OBSERVACIONES = (
    "https://w2b.inumet.gub.uy/oapi/collections/"
    "urn:wmo:md:uy-inumet:surface-based-observations.synop/items"
)

# Estaciones verificadas por el diagnóstico nacional.
# Conservamos Prado como "current" para no alterar todavía la PWA.
ESTACIONES = {
    "montevideo_prado": ("0-20000-0-86585", "Prado", -34.860639, -56.207389),
    "montevideo_carrasco": ("0-20000-0-86580", "Carrasco", -34.832923, -56.012876),
    "artigas": ("0-20000-0-86330", "Artigas", -30.39911, -56.51267),
    "bella_union": ("0-858-0-A000000000000009", "Bella Unión", -30.253235, -57.602608),
    "colonia": ("0-20000-0-86560", "Colonia", -34.45182199, -57.76804181),
    "durazno": ("0-20000-0-86530", "Durazno", -33.350522, -56.4972385),
    "florida": ("0-20000-0-86545", "Florida", -34.086325, -56.18795),
    "laguna_del_sauce": ("0-20000-0-86586", "Laguna del Sauce", -34.8604, -55.1071),
    "lavalleja": ("0-858-0-A000000000000001", "Lavalleja", -34.336, -55.0841),
    "melilla": ("0-20000-0-86575", "Melilla", -34.781, -56.2663),
    "melo": ("0-20000-0-86440", "Melo", -32.36675, -54.19257),
    "mercedes": ("0-20000-0-86490", "Mercedes", -33.2506, -58.0692),
    "paysandu": ("0-20000-0-86430", "Paysandú", -32.381, -58.0312),
    "punta_del_este": ("0-858-0-86595", "Punta del Este", -34.9689, -54.9512),
    "rocha": ("0-20000-0-86565", "Rocha", -34.4936, -54.3125),
    "salto": ("0-20000-0-86360", "Salto", -31.4388, -57.981),
    "san_jose": ("0-858-0-86550", "San José", -34.3519, -56.7497),
    "atlantida": ("0-858-0-A000000000000003", "Atlántida", -34.7797, -55.7528),
    "paso_de_los_toros": ("0-20000-0-86460", "Paso de los Toros", -32.7967, -56.5147),
    "rivera_aeropuerto": ("0-858-0-A000000000000004", "Rivera Aeropuerto", -30.9703, -55.4735),
    "san_jacinto": ("0-858-0-A000000000000002", "San Jacinto", -34.5145, -55.8464),
    "tacuarembo": ("0-20000-0-86370", "Tacuarembó", -31.7499634024, -55.9288138511),
    "treinta_y_tres": ("0-858-0-A000000000000005", "Treinta y Tres Aeropuerto", -33.1968, -54.3481),
    "trinidad": ("0-858-0-A000000000000007", "Trinidad", -33.486257, -56.890246),
    "vichadero": ("0-858-0-A000000000000008", "Vichadero", -31.74309, -54.58984),
    "young": ("0-858-0-A000000000000006", "Young", -32.66447, -57.58991),
}
WIGOS_A_KEY = {v[0]: k for k, v in ESTACIONES.items()}

# Zona oficial de pronóstico asociada a cada estación de la aplicación.
FORECAST_ZONE_BY_LOCATION = {
    "montevideo_prado": "M",
    "montevideo_carrasco": "M",
    "melilla": "M",
    "atlantida": "M",
    "san_jacinto": "M",
    "artigas": "NW",
    "bella_union": "NW",
    "salto": "NW",
    "paysandu": "NW",
    "young": "NW",
    "rivera_aeropuerto": "NE",
    "tacuarembo": "NE",
    "vichadero": "NE",
    "melo": "NE",
    "colonia": "SO",
    "mercedes": "SO",
    "san_jose": "SO",
    "durazno": "C",
    "florida": "C",
    "paso_de_los_toros": "C",
    "trinidad": "C",
    "lavalleja": "E",
    "rocha": "E",
    "treinta_y_tres": "E",
    "laguna_del_sauce": "E",
    "punta_del_este": "PE",
}

def instante(props):
    return str(props.get("phenomenonTime", "")).split("/")[0]

def edad_horas(hora, ahora):
    try:
        dt = datetime.fromisoformat(hora.replace("Z", "+00:00"))
        return (ahora - dt).total_seconds() / 3600
    except Exception:
        return None

def observacion_vacia(meta):
    wigos, nombre, lat, lon = meta
    return {
        "temperature": None, "station": nombre, "wigos": wigos,
        "latitude": lat, "longitude": lon, "observation_time": None,
        "condition": "neutral", "condition_time": None,
        "present_weather": None, "cloud_amount": None,
        "cloud_types": [], "wind_speed_kmh": None,
        "humidity": None, "pressure_hpa": None,
        "dewpoint_c": None, "feels_like_c": None,
        "visibility_m": None, "wind_direction_deg": None,
    }

def calcular_sensacion_termica(out):
    """Calcula temperatura aparente de Steadman sin radiación solar.

    Prioriza humedad relativa observada. Si INUMET no entrega RH pero sí
    punto de rocío, obtiene directamente de éste la presión de vapor, que es
    el término que realmente necesita la fórmula de Steadman.
    """
    if out.get("temperature") is None or out.get("wind_speed_kmh") is None:
        out["feels_like_c"] = None
        return

    try:
        t = float(out["temperature"])
        ws = float(out["wind_speed_kmh"]) / 3.6

        if out.get("humidity") is not None:
            rh = float(out["humidity"])
            e = (rh / 100.0) * 6.105 * math.exp((17.27 * t) / (237.7 + t))
        elif out.get("dewpoint_c") is not None:
            td = float(out["dewpoint_c"])
            e = 6.105 * math.exp((17.27 * td) / (237.7 + td))
        else:
            out["feels_like_c"] = None
            return

        out["feels_like_c"] = round(t + 0.33 * e - 0.70 * ws - 4.00, 1)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        out["feels_like_c"] = None

def recuperar_temperatura_previa_si_fresca(key, out, ahora):
    """Evita que una respuesta parcial de la API borre una temperatura válida."""
    if out.get("temperature") is not None:
        return out

    previa = (PREVIOUS_DATA.get("locations") or {}).get(key) or {}
    temperatura_previa = previa.get("temperature")
    hora_previa = previa.get("observation_time")
    edad = edad_horas(hora_previa, ahora) if hora_previa else None

    if (temperatura_previa is not None and edad is not None
            and 0 <= edad <= 2):
        out["temperature"] = temperatura_previa
        out["observation_time"] = hora_previa
        print(f"{key}: temperatura recuperada de data.json ({edad:.2f} h de antigüedad)")
        calcular_sensacion_termica(out)

    return out

def construir_observacion(meta, registros, ahora):
    out = observacion_vacia(meta)

    temps = [p for p in registros if p.get("name") == "air_temperature"
             and p.get("value") is not None]
    temps.sort(key=instante, reverse=True)
    if temps:
        hora = instante(temps[0])
        edad = edad_horas(hora, ahora)
        if edad is not None and 0 <= edad <= 2:
            out["temperature"] = temps[0].get("value")
            out["observation_time"] = hora

    nombres = {
        "present_weather", "cloud_amount", "cloud_cover_total",
        "cloud_type", "wind_speed", "wind_direction", "relative_humidity",
        "horizontal_visibility", "pressure_reduced_to_mean_sea_level", "non_coordinate_pressure",
        "dewpoint_temperature"
    }
    recientes = []
    for p in registros:
        if p.get("name") not in nombres:
            continue
        hora = instante(p)
        edad = edad_horas(hora, ahora)
        if edad is not None and 0 <= edad <= 2:
            recientes.append(p)

    if not recientes:
        return out

    recientes.sort(key=instante, reverse=True)
    out["condition_time"] = instante(recientes[0])

    def ultimo(nombre):
        xs = [p for p in recientes if p.get("name") == nombre]
        xs.sort(key=instante, reverse=True)
        if not xs:
            return []
        h = instante(xs[0])
        return [p for p in xs if instante(p) == h]

    weather = ultimo("present_weather")
    clouds = ultimo("cloud_amount")
    totals = ultimo("cloud_cover_total")
    types = ultimo("cloud_type")
    winds = ultimo("wind_speed")
    wind_direction = ultimo("wind_direction")
    visibility = ultimo("horizontal_visibility")
    humidity = ultimo("relative_humidity")
    pressure_msl = ultimo("pressure_reduced_to_mean_sea_level")
    pressure_station = ultimo("non_coordinate_pressure")
    dewpoint = ultimo("dewpoint_temperature")

    wd = [str(p.get("description") or "") for p in weather]
    cd = [str(p.get("description") or "") for p in clouds]
    out["present_weather"] = " | ".join(x for x in wd if x) or None
    out["cloud_amount"] = " | ".join(x for x in cd if x) or None
    out["cloud_types"] = [str(p["description"]) for p in types
                          if p.get("description")]

    if winds and winds[0].get("value") is not None:
        out["wind_speed_kmh"] = round(float(winds[0]["value"]) * 3.6, 1)

    if wind_direction and wind_direction[0].get("value") is not None:
        try:
            out["wind_direction_deg"] = round(float(wind_direction[0]["value"]), 0)
        except (TypeError, ValueError):
            pass

    if visibility and visibility[0].get("value") is not None:
        try:
            out["visibility_m"] = round(float(visibility[0]["value"]), 0)
        except (TypeError, ValueError):
            pass

    if humidity and humidity[0].get("value") is not None:
        try:
            out["humidity"] = round(float(humidity[0]["value"]), 0)
        except (TypeError, ValueError):
            pass

    # Para una app meteorológica mostramos presión reducida al nivel medio del mar,
    # que es la presión comparable entre estaciones. Si no existe, usamos la de estación.
    pressure_source = pressure_msl or pressure_station
    if pressure_source and pressure_source[0].get("value") is not None:
        try:
            out["pressure_hpa"] = round(float(pressure_source[0]["value"]), 1)
        except (TypeError, ValueError):
            pass

    if dewpoint and dewpoint[0].get("value") is not None:
        try:
            out["dewpoint_c"] = round(float(dewpoint[0]["value"]), 1)
        except (TypeError, ValueError):
            pass

    # Sensación térmica / temperatura aparente (Steadman, sin radiación solar).
    # Usa RH observada cuando está disponible. Si falta RH pero existe punto
    # de rocío, calcula la presión de vapor directamente a partir de Td.
    calcular_sensacion_termica(out)

    weather_text = " ".join(wd).upper()
    cloud_text = " ".join(cd).upper()

    if any(x in weather_text for x in ("THUNDER", "LIGHTNING")):
        out["condition"] = "stormy"
    elif any(x in weather_text for x in
             ("RAIN", "DRIZZLE", "SHOWER", "PRECIPIT", "HAIL", "SNOW")):
        out["condition"] = "rainy"
    elif any(x in weather_text for x in ("FOG", "MIST")):
        out["condition"] = "cloudy"
    else:
        oktas = [int(x) for x in re.findall(r"(\d+)\s*OKTAS?", cloud_text)]
        cobertura = max(oktas) if oktas else None

        if cobertura is None and totals:
            try:
                valor = float(totals[0].get("value"))
                cobertura = round(valor) if 0 <= valor <= 8 else (
                    round(valor * 8 / 100) if 0 <= valor <= 100 else None
                )
            except (TypeError, ValueError):
                pass

        if cobertura is not None:
            out["condition"] = (
                "cloudy" if cobertura >= 7 else
                "partly" if cobertura >= 3 else
                "sunny"
            )
    return out

locations = {k: observacion_vacia(v) for k, v in ESTACIONES.items()}
for _key in locations:
    locations[_key]["forecast_zone"] = FORECAST_ZONE_BY_LOCATION.get(_key, "M")

try:
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(hours=24)
    rango_tiempo = (desde.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" +
                    ahora.strftime("%Y-%m-%dT%H:%M:%SZ"))

    registros = {k: [] for k in ESTACIONES}
    url_actual = API_OBSERVACIONES
    params = {"f": "json", "limit": 1000, "datetime": rango_tiempo}
    pagina = 1

    while url_actual and pagina <= 20:
        print("Consultando página nacional:", pagina)
        respuesta = requests.get(url_actual, params=params, headers=HEADERS, timeout=60)
        print("Código respuesta API:", respuesta.status_code)
        respuesta.raise_for_status()
        api_data = respuesta.json()
        print("Registros recibidos:", len(api_data.get("features", [])))

        for feature in api_data.get("features", []):
            props = feature.get("properties", {})
            key = WIGOS_A_KEY.get(str(props.get("wigos_station_identifier")))
            if key and props.get("phenomenonTime"):
                registros[key].append(props)

        siguiente = next((x.get("href") for x in api_data.get("links", [])
                          if x.get("rel") == "next"), None)
        if siguiente:
            url_actual, params, pagina = siguiente, None, pagina + 1
        else:
            url_actual = None

    for key, meta in ESTACIONES.items():
        nueva = construir_observacion(meta, registros[key], ahora)
        nueva = recuperar_temperatura_previa_si_fresca(key, nueva, ahora)
        nueva["forecast_zone"] = FORECAST_ZONE_BY_LOCATION.get(key, "M")
        locations[key] = nueva
        o = locations[key]
        print(key, o["temperature"], o["condition"], o["wind_speed_kmh"],
              "RH", o.get("humidity"), "Td", o.get("dewpoint_c"),
              "ST", o.get("feels_like_c"))

except Exception as error:
    print("Error obteniendo observaciones nacionales:", error)

# Compatibilidad con la aplicación actual: current sigue siendo Prado.
prado = locations["montevideo_prado"]
current_temp = prado["temperature"]
current_time = prado["observation_time"]
current_station = prado["station"]
condition = prado["condition"]
condition_time = prado["condition_time"]
present_weather = prado["present_weather"]
cloud_amount = prado["cloud_amount"]
cloud_types = prado["cloud_types"]
wind_speed_kmh = prado["wind_speed_kmh"]
humidity = prado.get("humidity")
pressure_hpa = prado.get("pressure_hpa")
dewpoint_c = prado.get("dewpoint_c")
feels_like_c = prado.get("feels_like_c")

# =================================================
# 3. ADVERTENCIAS METEOROLÓGICAS OFICIALES INUMET - V4
# =================================================
# V4 mantiene las zonas oficiales del objeto "alerta", pero añade:
# - tres lecturas sin caché;
# - comprobación independiente de la portada de INUMET;
# - diagnóstico de frescura;
# - estado "data_unavailable" cuando INUMET dice que hay advertencia
#   pero la fuente estructurada todavía no entrega zonas vigentes.
#
# Importante: nunca inventa localidades ni prolonga una advertencia vencida.

URL_ALERTA = "https://www.inumet.gub.uy/alerta"
URL_INICIO = "https://www.inumet.gub.uy/"
alert = {"active": False}

def extraer_objeto_js_variable(html, nombre):
    patrones = [
        r'(?:var|let|const)\s+' + re.escape(nombre) + r'\s*=\s*',
        r'\b' + re.escape(nombre) + r'\s*=\s*',
    ]
    m = None
    for patron in patrones:
        m = re.search(patron, html, re.I)
        if m:
            break
    if not m:
        return None

    inicio = html.find("{", m.end())
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
        elif ch == "{":
            nivel += 1
        elif ch == "}":
            nivel -= 1
            if nivel == 0:
                return html[inicio:i + 1]
    return None

def nivel_advertencia(riesgos):
    valores = []
    for valor in (riesgos or {}).values():
        try:
            valores.append(int(valor))
        except (TypeError, ValueError):
            pass
    maximo = max(valores) if valores else 1
    return {2: "amarilla", 3: "naranja", 4: "roja"}.get(maximo)

def texto_zonas(zonas):
    if zonas is None:
        return ""
    return re.sub(
        r"\s+", " ",
        BeautifulSoup(str(zonas), "html.parser").get_text(" ", strip=True)
    ).strip()

def fecha_uruguay(fecha):
    if not fecha:
        return None

    texto_fecha = str(fecha).strip()
    formatos = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    )
    for formato in formatos:
        try:
            return datetime.strptime(texto_fecha, formato).replace(tzinfo=URUGUAY_TZ)
        except ValueError:
            pass

    try:
        dt = datetime.fromisoformat(texto_fecha.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=URUGUAY_TZ)
        return dt.astimezone(URUGUAY_TZ)
    except Exception:
        return None

def marca_frescura_alerta(datos):
    # NO usamos "finalizacion" para decidir qué respuesta es más nueva:
    # una hora futura de vencimiento no es una marca de actualización.
    fechas = []

    for campo in ("fechaActualizacion", "actualizacion", "updated"):
        dt = fecha_uruguay(datos.get(campo))
        if dt:
            fechas.append(dt)

    for adv in datos.get("advertencias") or []:
        for campo in ("actualizacion", "emision", "emitido", "comienzo"):
            dt = fecha_uruguay(adv.get(campo))
            if dt:
                fechas.append(dt)

    return max(fechas) if fechas else None

def portada_indica_vigente():
    try:
        headers = dict(HEADERS)
        headers.update({
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
        cache_buster = int(datetime.now(timezone.utc).timestamp() * 1000)
        respuesta = requests.get(
            URL_INICIO,
            params={"_": cache_buster},
            headers=headers,
            timeout=30,
        )
        respuesta.raise_for_status()

        texto_portada = re.sub(
            r"\s+", " ",
            BeautifulSoup(
                respuesta.text, "html.parser"
            ).get_text(" ", strip=True)
        )

        vigente = bool(re.search(
            r"Advertencia\s+Meteorol[oó]gica.{0,300}\bVigente\b",
            texto_portada,
            re.I,
        ))
        print("Portada INUMET indica advertencia vigente:", vigente)
        return vigente

    except Exception as error:
        print("No se pudo verificar portada INUMET:", error)
        return None

def descargar_objetos_alerta():
    candidatos = []
    headers_alerta = dict(HEADERS)
    headers_alerta.update({
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })

    for intento in range(1, 4):
        try:
            cache_buster = int(
                datetime.now(timezone.utc).timestamp() * 1000
            ) + intento

            respuesta = requests.get(
                URL_ALERTA,
                params={"_": cache_buster},
                headers=headers_alerta,
                timeout=30,
            )

            print(
                f"Advertencias intento {intento}:",
                respuesta.status_code,
                "| bytes:",
                len(respuesta.text),
            )
            respuesta.raise_for_status()

            bruto = extraer_objeto_js_variable(
                respuesta.text, "alerta"
            )
            print(
                f"Objeto alerta intento {intento}:",
                bool(bruto),
            )
            if not bruto:
                continue

            datos = json.loads(bruto)
            if not isinstance(datos, dict):
                continue

            marca = marca_frescura_alerta(datos)
            print(
                f"Marca de frescura intento {intento}:",
                marca.strftime("%d/%m/%Y %H:%M:%S")
                if marca else "SIN FECHA",
            )
            print(
                f"Bloques recibidos intento {intento}:",
                len(datos.get("advertencias") or []),
            )

            candidatos.append((marca, datos))

        except Exception as error:
            print(
                f"Error leyendo advertencias intento {intento}:",
                error,
            )

    return candidatos

try:
    portada_vigente = portada_indica_vigente()
    candidatos = descargar_objetos_alerta()
    ahora_local = datetime.now(URUGUAY_TZ)

    candidatos.sort(
        key=lambda item: item[0]
        or datetime(1970, 1, 1, tzinfo=URUGUAY_TZ),
        reverse=True,
    )

    datos_alerta = candidatos[0][1] if candidatos else None
    marca_fuente = candidatos[0][0] if candidatos else None

    edad_fuente_h = None
    if marca_fuente:
        edad_fuente_h = (
            ahora_local - marca_fuente
        ).total_seconds() / 3600

    print(
        "Objeto de advertencias seleccionado:",
        marca_fuente.strftime("%d/%m/%Y %H:%M:%S")
        if marca_fuente else "SIN FECHA",
    )
    print(
        "Edad de la fuente (h):",
        round(edad_fuente_h, 2)
        if edad_fuente_h is not None else "?",
    )

    vigentes = []

    if isinstance(datos_alerta, dict):
        for adv in datos_alerta.get("advertencias") or []:
            inicio_dt = fecha_uruguay(adv.get("comienzo"))
            fin_dt = fecha_uruguay(adv.get("finalizacion"))

            if fin_dt is not None and fin_dt < ahora_local:
                continue
            if inicio_dt is not None and inicio_dt > ahora_local:
                continue

            nivel = nivel_advertencia(
                adv.get("riesgoFenomeno")
            )
            if not nivel:
                continue

            vigentes.append({
                "level": nivel,
                "phenomenon": adv.get("fenomeno"),
                "probability": adv.get("probabilidad"),
                "start": adv.get("comienzo"),
                "end": adv.get("finalizacion"),
                "description": adv.get("descripcion"),
                "zones": texto_zonas(adv.get("zonas")),
                "risk": adv.get("riesgoFenomeno") or {},
            })

    if vigentes:
        prioridad = {
            "amarilla": 2,
            "naranja": 3,
            "roja": 4,
        }
        vigentes.sort(
            key=lambda x: prioridad.get(
                x.get("level"), 0
            ),
            reverse=True,
        )

        principal = vigentes[0]

        alert = {
            "active": True,
            "level": principal["level"],
            "phenomenon": principal["phenomenon"],
            "probability": principal["probability"],
            "start": principal["start"],
            "end": principal["end"],
            "description": principal["description"],
            "zones": principal["zones"],
            "warnings": vigentes,
            "updated": (
                datos_alerta.get("fechaActualizacion")
                or datos_alerta.get("actualizacion")
            ),
            "pdf": datos_alerta.get("pdf"),
            "source": "inumet_alerta_object_v4",
            "source_timestamp": (
                marca_fuente.isoformat()
                if marca_fuente else None
            ),
            "source_age_hours": (
                round(edad_fuente_h, 2)
                if edad_fuente_h is not None else None
            ),
            "homepage_says_active": portada_vigente,
            "data_unavailable": False,
        }

        print(
            "Advertencias INUMET vigentes:",
            len(vigentes),
        )
        for adv in vigentes:
            print(
                "ALERTA",
                adv["level"].upper(),
                "|",
                adv.get("phenomenon"),
                "|",
                adv.get("start"),
                "->",
                adv.get("end"),
            )
            print("ZONAS:", adv.get("zones"))

    elif portada_vigente is True:
        # La portada dice que hay advertencia, pero la fuente estructurada
        # todavía no ofrece zonas vigentes. No inventamos ni reutilizamos
        # zonas vencidas.
        alert = {
            "active": False,
            "data_unavailable": True,
            "homepage_says_active": True,
            "source": "inumet_sources_out_of_sync",
            "source_timestamp": (
                marca_fuente.isoformat()
                if marca_fuente else None
            ),
            "source_age_hours": (
                round(edad_fuente_h, 2)
                if edad_fuente_h is not None else None
            ),
            "message": (
                "INUMET informa una advertencia vigente, "
                "pero la fuente estructurada de zonas aún "
                "no entrega datos vigentes."
            ),
        }

        print(
            "ATENCIÓN: portada INUMET = VIGENTE, "
            "pero no hay zonas estructuradas vigentes."
        )

    else:
        alert = {
            "active": False,
            "data_unavailable": False,
            "homepage_says_active": portada_vigente,
            "source": "inumet_alerta_object_v4",
            "source_timestamp": (
                marca_fuente.isoformat()
                if marca_fuente else None
            ),
            "source_age_hours": (
                round(edad_fuente_h, 2)
                if edad_fuente_h is not None else None
            ),
        }
        print(
            "INUMET no entrega advertencias vigentes."
        )

except Exception as error:
    alert = {
        "active": False,
        "data_unavailable": True,
        "check_error": True,
        "source_error": str(error),
        "source": "inumet_alert_v4_error",
    }
    print(
        "Error procesando advertencias INUMET:",
        error,
    )

# =================================================
# 4. RESUMEN DEL PRONÓSTICO
# =================================================
t = days[0]
parts = [f"Temperaturas de {t['min']}° a {t['max']}°"]
texto_hoy = f"{t.get('morning') or ''} {t.get('evening') or ''}"
if re.search(r"precipit|lluvia|chaparr", texto_hoy, re.IGNORECASE):
    parts.append("con posibilidad de precipitaciones")
if re.search(r"viento|ráfaga", t["wind"], re.IGNORECASE):
    parts.append("y viento a tener en cuenta")

# =================================================
# 5. CREAR DATA.JSON
# =================================================
ahora_uruguay = datetime.now(URUGUAY_TZ)
data = {
    "updated": ahora_uruguay.strftime("%d/%m/%Y %H:%M"),
    "current": {
        "temperature": current_temp,
        "station": current_station,
        "observation_time": current_time,
        "condition": condition,
        "condition_time": condition_time,
        "present_weather": present_weather,
        "cloud_amount": cloud_amount,
        "cloud_types": cloud_types,
        "wind_speed_kmh": wind_speed_kmh,
        "humidity": humidity,
        "pressure_hpa": pressure_hpa,
        "dewpoint_c": dewpoint_c,
        "feels_like_c": feels_like_c,
    },
    "alert": alert,
    "locations": locations,
    "forecasts": forecasts,
    "days": days,
    "summary": "; ".join(parts) + ".",
}

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("--------------------------------")
print("Temperatura Prado:", current_temp)
print("Humedad Prado:", humidity)
print("Presión Prado:", pressure_hpa)
print("Sensación térmica Prado:", feels_like_c)
print("Condición visual observada:", condition)
print("Hora condición UTC:", condition_time)
print("Advertencia:", alert)
print("Hora actualización Uruguay:", ahora_uruguay.strftime("%d/%m/%Y %H:%M"))
print("Pronóstico, observación y advertencia actualizados correctamente.")
