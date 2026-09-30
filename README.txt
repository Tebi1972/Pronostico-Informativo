PRONÓSTICO PARA INFORMATIVO

Archivos:
- index.html: hoja de consulta e impresión.
- fetch_informativo.py: obtiene datos exclusivamente de INUMET.
- actualizar_pronostico.yml: workflow de GitHub Actions. Debe guardarse como .github/workflows/actualizar_pronostico.yml
- data.json: se genera automáticamente al ejecutar fetch_informativo.py.

Criterios:
- Estado actual: Carrasco.
- Pronóstico: tres días futuros.
- Regiones: Este, Noreste, Noroeste, Sur y Suroeste.
- Sur usa Área Metropolitana de INUMET.
- Los primeros dos días futuros salen del pronóstico detallado.
- El tercer día futuro sale del pronóstico extendido de INUMET.
- Fuente exclusiva: INUMET.
