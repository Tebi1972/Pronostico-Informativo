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
URL_ESTADO_DINAMICO = "https://www.inumet.gub.uy/reportes/estadoActual/estadoActualDatosHorarios.mch"
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



def cielo_desde_codigo_extendido(weather_code):
    codigo = str(weather_code).strip() if weather_code is not None else ""
    return {"4": "Cubierto"}.get(codigo)


def resumir_pronostico(texto, weather_code=None):
    if texto and "baja probabilidad" in str(texto).lower():
        cielo = cielo_desde_codigo_extendido(weather_code)
        return cielo

    """Resume para TV: cielo + lluvia relevante + tormentas/nieblas/vientos fuertes."""
    original = re.sub(r"\s+", " ", str(texto or "")).strip()
    bajo = original.lower()

    patrones_cielo = [
        (r"\balgo nuboso\b", "Algo nuboso"),
        (r"\bparcialmente nuboso\b", "Parcialmente nuboso"),
        (r"\bcubierto\b", "Cubierto"),
        (r"\bnuboso\b", "Nuboso"),
        (r"\bclaro\b", "Claro"),
        (r"\bdespejado\b", "Despejado"),
    ]
    encontrados = []
    for patron, etiqueta in patrones_cielo:
        m = re.search(patron, bajo)
        if m:
            encontrados.append((m.start(), etiqueta))
    cielo = min(encontrados, key=lambda x: x[0])[1] if encontrados else None

    if not cielo and isinstance(weather_code, str):
        wc = weather_code.strip().lower()
        encontrados = []
        for patron, etiqueta in patrones_cielo:
            m = re.search(patron, wc)
            if m:
                encontrados.append((m.start(), etiqueta))
        if encontrados:
            cielo = min(encontrados, key=lambda x: x[0])[1]

    baja_prob = bool(re.search(r"\bbaja\s+probabilidad\b", bajo))
    lluvia = bool(re.search(r"\b(precipit\w*|lluv\w*|lloviz\w*|chaparr\w*)\b", bajo)) and not baja_prob
    tormenta = bool(re.search(r"\btorment\w*\b", bajo))
    niebla = bool(re.search(r"\bnieblas?\b", bajo))
    viento_fuerte = bool(re.search(r"\bvientos?\s+(?:muy\s+)?fuertes?\b", bajo))

    partes = [cielo] if cielo else []
    if lluvia:
        partes.append("con lluvias" if partes else "Lluvias")
    if tormenta:
        partes.append((("y" if lluvia else "con") + " tormentas") if partes else "Tormentas")
    if niebla:
        partes.append((("y" if partes else "Con") + " nieblas"))
    if viento_fuerte:
        partes.append((("y" if partes else "Con") + " vientos fuertes"))

    return " ".join(partes) if partes else (original or "Pronóstico extendido")


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
        "evening": resumir_pronostico(texto(tarde), datos.get("estadoTiempo")),
        "extended": False,
        "weather_code": datos.get("estadoTiempo"),
    }


def texto_extendido(item):
    subs = item.get("subgrupos") or []
    textos = []
    if isinstance(subs, list):
        tarde_noche = [
            sg for sg in subs
            if isinstance(sg, dict)
            and ("tarde" in str(sg.get("subgrupo", "")).lower()
                 or "noche" in str(sg.get("subgrupo", "")).lower())
        ]
        fuente = tarde_noche if tarde_noche else subs
        for sg in fuente:
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
        "evening": resumir_pronostico(texto_extendido(item), item.get("estadoTiempo")),
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
    "Norte": ("0-20000-0-86330", "Artigas", 16),
    "Este": ("0-20000-0-86565", "Rocha", 236),
    "Sur": ("0-20000-0-86580", "Carrasco", 39),
    "Oeste": ("0-20000-0-86490", "Mercedes", 162),
}

VARIABLES_ESTADO = {
    "temperature": 47,
    "humidity": 25,
    "wind_direction_deg": 8,
    "wind_speed": 29,
    "visibility": 74,
    "cloud_amount": 3,
    "present_weather": 123,
}


def obtener_estado_actual_oficial():
    """Lee la tabla oficial Estado actual de INUMET.

    Esta tabla es la referencia que ve el editor en la web de INUMET y publica
    temperatura, humedad, viento, visibilidad y cielo para una misma hora de observación.
    """
    try:
        r = requests.get(URL_ESTADO, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        texto = soup.get_text(" ", strip=True)
        mf = re.search(r"Fecha:\s*(\d{4}-\d{2}-\d{2})", texto, re.I)
        mh = re.search(r"Observaciones realizadas a la hora\s*(\d{1,2}:\d{2})", texto, re.I)
        fecha = mf.group(1) if mf else datetime.now(UY_TZ).strftime("%Y-%m-%d")
        hora = mh.group(1) if mh else None
        obs_iso = f"{fecha}T{hora}:00-03:00" if hora else None

        buscadas = {"Norte":"artigas", "Este":"rocha", "Sur":"carrasco", "Oeste":"mercedes"}
        dirs = {"N":0,"NNE":22.5,"NE":45,"ENE":67.5,"E":90,"ESE":112.5,"SE":135,"SSE":157.5,
                "S":180,"SSW":202.5,"SW":225,"WSW":247.5,"W":270,"WNW":292.5,"NW":315,"NNW":337.5}
        def num(x):
            m=re.search(r"-?\d+(?:[.,]\d+)?", str(x or ""))
            return float(m.group(0).replace(",",".")) if m else None
        salida={}
        for tr in soup.find_all("tr"):
            c=[re.sub(r"\s+"," ",x.get_text(" ",strip=True)).strip() for x in tr.find_all(["td","th"])]
            if len(c)<8: continue
            nombre=c[0].lower()
            zona=next((z for z,pref in buscadas.items() if nombre.startswith(pref)),None)
            if not zona: continue
            viento=c[1]
            partes=[x.strip().upper() for x in viento.split("/")]
            direccion=dirs.get(partes[0]) if partes and partes[0] not in ("CALMO","-") else None
            velocidad=num(partes[1]) if len(partes)>1 else (0.0 if partes and partes[0]=="CALMO" else None)
            cielo=c[2] if c[2] not in ("","-") else None
            presente=c[3] if c[3] not in ("","-") else None
            salida[zona]={
                "temperature":num(c[4]), "humidity":num(c[5]),
                "visibility_m": None if num(c[7]) is None else round(num(c[7])*1000),
                "wind_speed_kmh":velocidad, "wind_direction_deg":direccion,
                "condition":presente or cielo, "present_weather":presente,
                "cloud_amount":cielo, "observation_time":obs_iso,
                "official_observation_date":fecha, "official_observation_hour":hora,
            }
        return salida
    except Exception as e:
        print("Aviso: no se pudo leer estado actual de INUMET:", e)
        return {}

def obtener_actuales_synop():
    # INUMET publica las observaciones SYNOP con phenomenonTime y reportTime.
    # Para saber cuál es realmente la observación más nueva usamos reportTime
    # como referencia principal. Esto evita quedar atados a una hora anterior
    # cuando phenomenonTime representa un intervalo (por ejemplo, en viento).
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(hours=24)
    rango = desde.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" + ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    por_wigos = {meta[0]: [] for meta in ESTACIONES.values()}

    url = API_OBSERVACIONES
    params = {"f": "json", "limit": 1000, "datetime": rango}
    pagina = 1
    while url and pagina <= 30:
        r = requests.get(url, params=params, headers=HEADERS, timeout=60)
        r.raise_for_status()
        datos = r.json()
        for f in datos.get("features", []):
            prop = f.get("properties", {})
            wigos = str(prop.get("wigos_station_identifier") or "")
            if wigos in por_wigos:
                por_wigos[wigos].append(prop)
        url = next((x.get("href") for x in datos.get("links", []) if x.get("rel") == "next"), None)
        params = None
        pagina += 1
    print("Páginas SYNOP consultadas:", pagina - 1)

    def tiempo_registro(p):
        # reportTime es la hora del informe SYNOP. Si no existe, usamos
        # phenomenonTime como respaldo.
        return str(p.get("reportTime") or instante(p) or "")

    def ultimo(registros, nombre, max_horas=12):
        candidatos = []
        for x in registros:
            if x.get("name") != nombre:
                continue
            if x.get("value") is None and not str(x.get("description") or "").strip():
                continue
            iso = tiempo_registro(x)
            if not iso:
                continue
            if 0 <= edad_horas(iso, ahora) <= max_horas:
                candidatos.append(x)
        candidatos.sort(key=tiempo_registro, reverse=True)
        if not candidatos:
            return []
        hora = tiempo_registro(candidatos[0])
        return [x for x in candidatos if tiempo_registro(x) == hora]

    def descripcion(xs):
        return " | ".join(dict.fromkeys(
            str(x.get("description") or "").strip()
            for x in xs if str(x.get("description") or "").strip()
        )) or None

    def condicion(weather, clouds, totals):
        texto = descripcion(weather)
        if texto and any(k in texto.upper() for k in (
            "RAIN", "DRIZZLE", "SHOWER", "THUNDER", "PRECIP", "HAIL", "SNOW",
            "LLUV", "LLOV", "LLOVIZ", "CHAPARR", "TORMENT", "GRANIZ", "NIEV"
        )):
            return texto
        nube = descripcion(clouds)
        if nube:
            t = nube.upper()
            oktas = [int(x) for x in re.findall(r"(\d+)\s*OKTAS?", t)]
            if oktas:
                n = max(oktas)
                return "Cubierto" if n >= 8 else "Nuboso" if n >= 4 else "Algo nuboso" if n >= 1 else "Despejado"
            return nube
        if totals:
            try:
                n = float(totals[0]["value"])
                if n > 8:
                    n = n * 8 / 100
                return "Cubierto" if n >= 8 else "Nuboso" if n >= 4 else "Algo nuboso" if n >= 1 else "Despejado"
            except (TypeError, ValueError):
                pass
        return texto

    def hora_uy(p):
        if not p:
            return None
        iso = tiempo_registro(p)
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UY_TZ)
            return dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return iso

    actuales = {}
    for zona, (wigos, estacion, _id_inumet) in ESTACIONES.items():
        registros = por_wigos[wigos]
        temp = ultimo(registros, "air_temperature")
        hum = ultimo(registros, "relative_humidity")
        viento = ultimo(registros, "wind_speed")
        direccion = ultimo(registros, "wind_direction")
        vis = ultimo(registros, "horizontal_visibility")
        weather = ultimo(registros, "present_weather")
        clouds = ultimo(registros, "cloud_amount")
        totals = ultimo(registros, "cloud_cover_total")

        condicion_final = condicion(weather, clouds, totals)
        observation_time = tiempo_registro(temp[0]) if temp else None

        actuales[zona] = {
            "station": estacion,
            "wigos": wigos,
            "temperature": temp[0].get("value") if temp else None,
            "condition": condicion_final,
            "present_weather": descripcion(weather),
            "cloud_amount": descripcion(clouds),
            "observation_time": observation_time,
            "humidity": round(float(hum[0]["value"])) if hum and hum[0].get("value") is not None else None,
            "wind_speed_kmh": round(float(viento[0]["value"]) * 3.6, 1) if viento and viento[0].get("value") is not None else None,
            "wind_direction_deg": round(float(direccion[0]["value"])) if direccion and direccion[0].get("value") is not None else None,
            "visibility_m": round(float(vis[0]["value"])) if vis and vis[0].get("value") is not None else None,
        }

        print(
            zona, estacion,
            "registros:", len(registros),
            "hora:", hora_uy(temp[0]) if temp else None,
            "temperatura:", actuales[zona]["temperature"],
            "humedad:", actuales[zona]["humidity"],
            "viento_kmh:", actuales[zona]["wind_speed_kmh"],
            "vis_m:", actuales[zona]["visibility_m"],
            "cielo:", actuales[zona]["condition"],
        )

    if all(x["temperature"] is None for x in actuales.values()):
        raise RuntimeError("INUMET no devolvió temperaturas recientes para las cuatro estaciones")
    return actuales



def _valor_matriz_estado(datos, estacion_id, variable_id):
    """Devuelve el valor [0] de la matriz oficial de Estado actual de INUMET."""
    estaciones = datos.get("estaciones") or []
    variables = datos.get("variables") or []
    observaciones = datos.get("observaciones") or []
    ids_est = [x.get("id") for x in estaciones]
    ids_var = [x.get("idInt") for x in variables]
    try:
        ie = ids_est.index(estacion_id)
        iv = ids_var.index(variable_id)
        fila = (observaciones[iv] or {}).get("datos") or []
        celda = fila[ie] if ie < len(fila) else None
        if isinstance(celda, list):
            return celda[0] if celda else None
        return celda
    except (ValueError, IndexError, TypeError):
        return None


def _numero(v):
    if v in (None, "", "-", "null"):
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _texto_cielo_estado(cielo):
    if cielo in (None, "", "-"):
        return None
    t = str(cielo).strip().lower()
    mapa = {
        "des": "Despejado", "desp": "Despejado",
        "poc": "Poco nuboso", "poco": "Poco nuboso",
        "alg": "Algo nuboso",
        "nub": "Nuboso",
        "muy": "Muy nuboso",
        "cub": "Cubierto",
    }
    return mapa.get(t, str(cielo))


def _texto_tiempo_presente(presente):
    if presente in (None, "", "-"):
        return None
    # Códigos que usa la interfaz de INUMET para representar el fenómeno/ícono.
    # Si aparece un código no conocido, no inventamos una descripción.
    mapa = {
        "2": "Lluvias",
        "4": "Nuboso",
        "7": "Lluvias y nieblas",
        "11": "Lluvias y tormentas",
        "13": "Algo nuboso",
    }
    t = str(presente).strip()
    return mapa.get(t)


def _condicion_estado(presente, cielo):
    # Para la portada informativa privilegiamos una descripción legible.
    # Un fenómeno presente conocido tiene prioridad; de lo contrario usamos cielo.
    return _texto_tiempo_presente(presente) or _texto_cielo_estado(cielo)

def obtener_actuales_dinamicos():
    """Fuente primaria: matriz dinámica que alimenta Estado actual de INUMET."""
    r = requests.get(URL_ESTADO_DINAMICO, headers=HEADERS, timeout=30)
    r.raise_for_status()
    datos = r.json()
    if not all(k in datos for k in ("estaciones", "variables", "observaciones")):
        raise RuntimeError("Formato inesperado en Estado actual dinámico de INUMET")

    # La matriz no siempre expone una marca temporal por celda. La tomamos de
    # la propia página oficial, que publica la hora común de observación.
    tabla = obtener_estado_actual_oficial()
    # Si la página HTML no expone la hora (actualmente puede cargarse por JS),
    # usamos exclusivamente la marca temporal del SYNOP oficial como respaldo.
    # Los valores meteorológicos siguen viniendo de la matriz dinámica.
    synop_respaldo = {}
    if not tabla or any(not tabla.get(z, {}).get("observation_time") for z in ESTACIONES):
        try:
            synop_respaldo = obtener_actuales_synop()
        except Exception as e:
            print("Aviso: no se pudo obtener hora SYNOP de respaldo:", e)
    actuales = {}
    for zona, (wigos, estacion, estacion_id) in ESTACIONES.items():
        temp = _numero(_valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["temperature"]))
        hum = _numero(_valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["humidity"]))
        vel = _numero(_valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["wind_speed"]))
        dire = _numero(_valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["wind_direction_deg"]))
        vis = _numero(_valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["visibility"]))
        cielo = _valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["cloud_amount"])
        presente = _valor_matriz_estado(datos, estacion_id, VARIABLES_ESTADO["present_weather"])
        oficial = tabla.get(zona, {})

        # Para Pronóstico Informativo, "condition" debe representar solamente
        # el estado del cielo. No reinterpretamos el código bruto de tiempo
        # presente como si fuera un código de pronóstico.
        cielo_texto = oficial.get("cloud_amount") or _texto_cielo_estado(cielo)
        respaldo = synop_respaldo.get(zona, {})
        # La variable 29 se publica habitualmente en km/h en esta interfaz.
        actuales[zona] = {
            "station": estacion,
            "wigos": wigos,
            "temperature": temp,
            "condition": cielo_texto,
            # Se conserva el valor bruto de la matriz dinámica. Su tabla de
            # códigos se validará por separado antes de usarla en Tiempo Uruguay.
            "present_weather": presente,
            "cloud_amount": cielo_texto,
            "observation_time": oficial.get("observation_time") or respaldo.get("observation_time"),
            "humidity": round(hum) if hum is not None else oficial.get("humidity"),
            "wind_speed_kmh": vel if vel is not None else oficial.get("wind_speed_kmh"),
            "wind_direction_deg": round(dire) if dire is not None else oficial.get("wind_direction_deg"),
            # La interfaz dinámica maneja visibilidad en km; data.json la guarda en metros.
            "visibility_m": round(vis * 1000) if vis is not None else oficial.get("visibility_m"),
        }
        print("Estado dinámico", zona, estacion, actuales[zona])

    if any(x["temperature"] is None for x in actuales.values()):
        faltan = [z for z, x in actuales.items() if x["temperature"] is None]
        raise RuntimeError("Estado actual dinámico sin temperatura para: " + ", ".join(faltan))
    return actuales


def obtener_actuales():
    try:
        actuales = obtener_actuales_dinamicos()
        print("Fuente de datos actuales: Estado actual dinámico de INUMET")
        return actuales
    except Exception as e:
        print("Aviso: falló Estado actual dinámico; se usa SYNOP como respaldo:", e)
        return obtener_actuales_synop()

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
