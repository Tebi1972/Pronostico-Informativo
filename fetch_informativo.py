import json, re
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

HEADERS={"User-Agent":"Pronostico-Informativo/2.0"}
UY_TZ=ZoneInfo("America/Montevideo")
URL_PRONOSTICO="https://www.inumet.gub.uy/tiempo/pronostico"
URL_EXTENDIDO="https://www.inumet.gub.uy/reportes/pronosticos/pronosticoV4.json"
URL_ESTADO="https://www.inumet.gub.uy/tiempo/estado-actual"
ZONAS={"NW":"Norte","M":"Sur","E":"Este","SO":"Oeste"}
ESTACIONES={"Norte":"Artigas","Este":"Rocha","Sur":"Carrasco","Oeste":"Mercedes"}

def clean(s): return re.sub(r"\s+"," ",str(s or "")).strip()
def unir(*xs): return " ".join(clean(x) for x in xs if clean(x)) or None

def extraer_array_js(html,codigo):
    m=re.search(r'pronosticos\s*\[\s*["\']'+re.escape(codigo)+r'["\']\s*\]\s*=\s*',html,re.I)
    if not m:return None
    ini=html.find('[',m.end()); nivel=0; q=None; esc=False
    for i in range(ini,len(html)):
        ch=html[i]
        if q:
            if esc: esc=False
            elif ch=='\\': esc=True
            elif ch==q:q=None
            continue
        if ch in ('"',"'"):q=ch
        elif ch=='[':nivel+=1
        elif ch==']':
            nivel-=1
            if nivel==0:return html[ini:i+1]
    return None

def convertir_periodo(p):
    d=(p or {}).get('datos') or {}; subs=d.get('subgrupos') or []
    man=next((x for x in subs if clean(x.get('subgrupo')).lower().startswith('mañ')),None)
    tar=next((x for x in subs if 'tarde' in clean(x.get('subgrupo')).lower()),None)
    def txt(x): return unir(x.get('descripcion'),x.get('evolucion'),x.get('descripcionExtra')) if x else None
    if d.get('tempMin') is None or d.get('tempMax') is None:return None
    return {'date':str(d.get('grupo') or d.get('grupoCorto') or ''),'min':str(d['tempMin']),'max':str(d['tempMax']),
            'morning':txt(man),'evening':txt(tar),'extended':False,'weather_code':d.get('estadoTiempo')}

def texto_extendido(item):
    textos=[]
    for sg in item.get('subgrupos') or []:
        if isinstance(sg,dict):
            t=unir(sg.get('descripcion'),sg.get('evolucion'),sg.get('descripcionExtra'))
            if t:textos.append(t)
    if textos:return ' '.join(dict.fromkeys(textos))
    for k in ('probabilidadPrecipitaciones','probabilidadPrecipitacion','probPrecipitaciones','probPrecipitacion','probLluvia','precipitaciones','precipitacion'):
        if item.get(k) not in (None,''):return clean(item[k])
    return 'Pronóstico extendido'

def convertir_extendido(item):
    if item.get('tempMin') is None or item.get('tempMax') is None:return None
    return {'date':str(item.get('grupo') or item.get('grupoCorto') or ''),'min':str(item['tempMin']),'max':str(item['tempMax']),
            'morning':None,'evening':texto_extendido(item),'extended':True,'weather_code':item.get('estadoTiempo')}

def obtener_pronosticos():
    html=requests.get(URL_PRONOSTICO,headers=HEADERS,timeout=30).text
    out={}
    for code,name in ZONAS.items():
        bruto=extraer_array_js(html,code)
        if not bruto: raise RuntimeError(f'No se encontró zona {code}')
        dias=[]
        for p in json.loads(bruto):
            d=convertir_periodo(p)
            if d:dias.append(d)
            if len(dias)>=3:break
        out[code]={'name':name,'days':dias}
    try:
        items=requests.get(URL_EXTENDIDO,headers=HEADERS,timeout=30).json().get('items',[])
        for code in ZONAS:
            candidatos=[]
            for item in items:
                z=clean(item.get('zonaCorta')).upper()
                if z=='SW':z='SO'
                if z!=code:continue
                try:n=int(item.get('diaMasN'))
                except:continue
                if n>=3:candidatos.append((n,item))
            existentes={x['date'].lower() for x in out[code]['days']}
            for _,item in sorted(candidatos,key=lambda x:x[0]):
                d=convertir_extendido(item)
                if d and d['date'].lower() not in existentes:
                    out[code]['days'].append(d); existentes.add(d['date'].lower())
                if len(out[code]['days'])>=4:break
    except Exception as e: print('Aviso extendido:',e)
    return out

def numero(s):
    m=re.search(r'-?\d+(?:[.,]\d+)?',clean(s)); return float(m.group(0).replace(',','.')) if m else None

def obtener_actuales():
    r=requests.get(URL_ESTADO,headers=HEADERS,timeout=30); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
    texto=soup.get_text(' ',strip=True)
    mf=re.search(r'Fecha:\s*(\d{4}-\d{2}-\d{2})',texto,re.I); mh=re.search(r'Observaciones realizadas a la hora\s*(\d{1,2}:\d{2})',texto,re.I)
    fecha=mf.group(1) if mf else datetime.now(UY_TZ).strftime('%Y-%m-%d'); hora=mh.group(1) if mh else None
    encontrados={}
    for tr in soup.find_all('tr'):
        c=[clean(x.get_text(' ',strip=True)) for x in tr.find_all(['td','th'])]
        if len(c)<8:continue
        nombre=c[0].lower()
        zona=None
        if nombre.startswith('artigas'):zona='Norte'
        elif nombre.startswith('rocha'):zona='Este'
        elif nombre.startswith('carrasco'):zona='Sur'
        elif nombre.startswith('mercedes'):zona='Oeste'
        if not zona:continue
        # columnas INUMET: estación, viento, nubosidad, tiempo presente, temp, humedad, presión, visibilidad
        cielo=c[2] if c[2] not in ('','-') else ''
        presente=c[3] if c[3] not in ('','-') else ''
        condicion=presente if presente else cielo
        encontrados[zona]={'temperature':numero(c[4]),'condition':condicion or '—','humidity':numero(c[5]),
                           'wind':c[1] if c[1] else '—','visibility_km':numero(c[7]),'observation_date':fecha,'observation_time':hora}
    faltan=[z for z in ESTACIONES if z not in encontrados]
    if faltan:raise RuntimeError('Faltan estaciones en estado actual INUMET: '+', '.join(faltan))
    return encontrados

def main():
    actuales=obtener_actuales(); forecasts=obtener_pronosticos(); carrasco=actuales['Sur']
    salida={'updated':datetime.now(UY_TZ).strftime('%d/%m/%Y %H:%M'),'source':'INUMET','current':carrasco,
            'current_regions':actuales,'forecasts':forecasts}
    with open('data.json','w',encoding='utf-8') as f:json.dump(salida,f,ensure_ascii=False,indent=2)
    print('data.json actualizado correctamente')
    for z,d in actuales.items():print(z,d['temperature'],d['condition'])
    for z in ZONAS:print(z,'->',len(forecasts[z]['days']),'días')
if __name__=='__main__':main()
