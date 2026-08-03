# CAS LeadTime AI — v2

## Qué cambió respecto a tu versión

**1. Bug de persistencia — resuelto.**
Tu versión guardaba la base de datos en `sqlite:////tmp/database.db`. En Render (plan free),
`/tmp` se borra cada vez que el contenedor se reinicia o se duerme por inactividad — por eso
nunca podías iniciar sesión con una cuenta vieja. Ahora `app.py` lee la variable de entorno
`DATABASE_URL`: si existe (Postgres en Render), la usa; si no (tu computador), usa SQLite local.

**2. SECRET_KEY ya no está hardcodeada** en el código público — ahora se lee de una variable
de entorno. Con la clave hardcodeada, cualquiera que viera tu repo podía falsificar sesiones.

**3. Rediseño visual completo** — misma identidad institucional (logo CAS, dorado/azul marino),
tipografía nueva (Fraunces + Plus Jakarta Sans), tarjetas con más jerarquía visual, iconos en vez
de emojis.

**4. Función nueva: Radar de Carga Semanal.** Extiende tu propia lógica de "alerta de cuelgue"
a una vista de 7 días de un vistazo, coloreada por nivel de carga (libre / moderado / crítico).

**5. Botón de eliminar tarea** agregado (antes solo se podía completar).

## Cómo desplegar en Render

1. Sube esta carpeta a un repo de GitHub (o reemplaza los archivos en tu repo actual).
2. En tu servicio de Render → **Environment**, agrega estas variables:
   - `SECRET_KEY` → cualquier string largo y aleatorio (ej. genera uno en https://randomkeygen.com)
   - `GEMINI_API_KEY` → tu API key de Gemini (la misma que ya usabas)
3. Crea una base de datos Postgres gratuita en Render (**New → PostgreSQL**, plan Free).
   Copia el **Internal Database URL** y agrégalo como variable de entorno `DATABASE_URL`
   en tu servicio web.
4. Redeploy. A partir de ahora, los usuarios y tareas persisten aunque el servidor se duerma
   o se reinicie.

## Correr localmente (opcional, para probar antes de subir)

```bash
pip install -r requirements.txt
python app.py
```
Sin `DATABASE_URL` configurada, usará automáticamente un archivo SQLite local
(`local_dev.db`) — perfecto para probar sin tocar tu base de datos de producción.
