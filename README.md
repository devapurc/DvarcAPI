# Dvarc API

API de solo lectura para distribuir **versiones pre-hechas y bloqueadas** del
Dvarc Launcher. El launcher pide un token al instalarse, lo guarda y lo envía en
cada petición. Las versiones se administran únicamente desde el sistema de
archivos del host: la API **no tiene POST ni DELETE**.

## Requisitos

- Python 3.11+ (probado con 3.13)
- Windows/Linux

## Instalación y arranque

```powershell
cd D:\programacion\python\dvarcapi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

O simplemente:

```powershell
.\run.ps1
```

- Documentación interactiva: `http://127.0.0.1:8765/docs`
- Salud: `GET /api/v1/health`

## Agregar una versión

Crear una carpeta dentro de `versions/` con el nombre de la versión (slug:
letras, números, `.`, `_`, `-`):

```
versions/
└─ survival-1/
   ├─ version.json
   ├─ icon.png                (opcional)
   ├─ mods/
   │  ├─ sodium.jar
   │  └─ iris.jar
   ├─ resourcepacks/
   │  └─ faithful.zip
   └─ shaderpacks/
      └─ bsl.zip
```

`versions/survival-1/version.json`:

```json
{
  "name": "Dvarc Survival",
  "description": "Pack oficial de supervivencia",
  "minecraft": "1.21.1",
  "loader": { "type": "fabric", "version": "0.16.9" },
  "java": 21,
  "memory": "4G",
  "icon": "icon.png",
  "allowedUsers": []
}
```

- `loader.type`: `fabric`, `quilt`, `forge`, `neoforge` o `vanilla`.
- `loader.version`: obligatorio salvo `vanilla`.
- `allowedUsers`: vacío = todos. Con nombres = solo los tokens vinculados a
  esos usuarios pueden listar y descargar la versión.
- No hace falta reiniciar: la API detecta los cambios por fecha de modificación
  y calcula el manifiesto (tamaño + SHA-256) automáticamente.

Los archivos permitidos son `.jar` en `mods/`, `.zip` en `resourcepacks/` y
`shaderpacks/`, más el icono.

## Eventos

Los eventos se guardan en `data/events.json` y solo se entregan a los usuarios
vinculados al token que los puede ver. La API sigue siendo de solo lectura: los
eventos se administran con `tools/admin.py` o editando el archivo.

```json
{
  "version": 1,
  "events": [
    {
      "id": "apertura-temporada-2",
      "name": "Apertura de temporada",
      "description": "Opcional",
      "startsAt": "2026-12-01T20:00:00-03:00",
      "discordUrl": "https://discord.gg/xxxx",
      "users": ["Steve", "Alex"]
    }
  ]
}
```

- `startsAt`: ISO 8601 **con offset obligatorio** (`Z` o `-03:00`). El sitio lo
  muestra en la zona horaria del navegador.
- `users`: usuarios que pueden ver el evento (sin distinguir mayúsculas). Lista
  vacía = visible para todos los tokens válidos.
- `discordUrl`: obligatorio, `https` y dominio `discord.gg`/`discord.com`.
- `id`: minúsculas, números y guiones. Se genera solo si se omite.
- No hace falta reiniciar: la API recarga el archivo cuando cambia. Las entradas
  inválidas se ignoran con un aviso en el log.

## Endpoints

Todos son `GET`.

| Endpoint | Token | Descripción |
|---|---|---|
| `/api/v1/health` | No | Estado y cantidad de versiones |
| `/api/v1/token` | No | Emite un token nuevo |
| `/api/v1/token/bind?username=Steve` | Sí | Vincula el token a una cuenta |
| `/api/v1/me` | Sí | Cuentas vinculadas al token |
| `/api/v1/events` | Sí | Eventos visibles para las cuentas del token |
| `/api/v1/events/{id}` | Sí | Detalle de un evento visible |
| `/api/v1/versions` | Sí | Versiones disponibles para el token |
| `/api/v1/versions/{id}` | Sí | Detalle + manifiesto con SHA-256 |
| `/api/v1/versions/{id}/files/{ruta}` | Sí | Descarga un archivo (soporta `Range`) |

El token se envía en la cabecera `X-Dvarc-Token` o como
`Authorization: Bearer <token>`.

### Ejemplo con curl

```bash
TOKEN=$(curl -s http://127.0.0.1:8765/api/v1/token | jq -r .token)

curl -s -H "X-Dvarc-Token: $TOKEN" http://127.0.0.1:8765/api/v1/versions

curl -s -H "X-Dvarc-Token: $TOKEN" \
  "http://127.0.0.1:8765/api/v1/token/bind?username=Steve"

curl -s -H "X-Dvarc-Token: $TOKEN" \
  http://127.0.0.1:8765/api/v1/versions/survival-1/files/mods/sodium.jar \
  -o sodium.jar
```

## CORS

El sitio público (`https://dvarc.apurc.site`) consume la API desde otro origen.
Por defecto se permiten `https://dvarc.apurc.site`,
`http://127.0.0.1:5500` y `http://localhost:5500`. Se puede cambiar con
`DVARC_CORS_ORIGINS` (lista separada por comas).

## Reglas del token

- En disco solo se guarda el **SHA-256** del token (`data/tokens.json`).
- Un token puede tener varias cuentas vinculadas.
- Un nombre de usuario pertenece a **un solo token**. Gana el primero que se
  vinculó: si otro token intenta usar el mismo nombre, se responde `409` y el
  token nuevo se descarta cuando no tiene otras cuentas.
- Al agregar una cuenta en el launcher se llama a `/token/bind` automáticamente.
- `allowedUsers` permite restringir versiones por usuario en el futuro (ya
  implementado): el filtrado usa las cuentas vinculadas al token.

## Administración local

```powershell
.\.venv\Scripts\python.exe tools\admin.py validate
.\.venv\Scripts\python.exe tools\admin.py list-tokens
.\.venv\Scripts\python.exe tools\admin.py list-bindings
.\.venv\Scripts\python.exe tools\admin.py revoke-token <prefijo-del-hash>

# Eventos
.\.venv\Scripts\python.exe tools\admin.py events list
.\.venv\Scripts\python.exe tools\admin.py events validate
.\.venv\Scripts\python.exe tools\admin.py events add --name "Apertura" ^
    --start "2026-12-01T20:00:00-03:00" --discord https://discord.gg/xxxx ^
    --user Steve --user Alex --description "Primer evento"
.\.venv\Scripts\python.exe tools\admin.py events users apertura-a1b2 --user Steve
.\.venv\Scripts\python.exe tools\admin.py events remove apertura-a1b2
```

`events users` sin `--user` deja el evento visible para todos. `events validate`
reporta errores de formato, fechas sin offset, links que no son de Discord y
usuarios inválidos.

`validate` informa errores de `version.json`, loaders desconocidos y archivos.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Cubren: emisión de tokens, autenticación, vinculación idempotente, conflicto de
usuario (primer token gana), `allowedUsers`, manifiestos con SHA-256, descargas
con `Range`, rechazo de path traversal, eventos por usuario, recarga del archivo
de eventos, CORS y validación de eventos.

## Configuración

Copiar `.env.example` a `.env` o definir variables de entorno:

| Variable | Default | Descripción |
|---|---|---|
| `DVARC_HOST` | `127.0.0.1` | Interfaz de escucha |
| `DVARC_PORT` | `8765` | Puerto |
| `DVARC_VERSIONS_DIR` | `versions` | Carpeta de versiones |
| `DVARC_DATA_DIR` | `data` | Carpeta de tokens |
| `DVARC_TOKEN_RATE_LIMIT` | `10` | Tokens por ventana y por IP (0 = sin límite) |
| `DVARC_TOKEN_RATE_WINDOW` | `60` | Ventana en segundos |
| `DVARC_PUBLIC_BASE_URL` | vacío | URL pública para manifiestos absolutos |
| `DVARC_CORS_ORIGINS` | `https://dvarc.apurc.site,http://127.0.0.1:5500,http://localhost:5500` | Orígenes permitidos por CORS |

Las rutas relativas se resuelven contra la carpeta del proyecto, no contra el
directorio de trabajo.

## Producción

1. Ejecutar con un servidor ASGI (uvicorn) detrás de Caddy/Nginx con HTTPS.
2. `DVARC_TOKEN_RATE_LIMIT` a un valor razonable.
3. No exponer `data/tokens.json` por el servidor web.
4. Hacer backup de `versions/` y `data/`.

Ejemplo con Caddy:

```
api.dvarc.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

## Nota de seguridad

`GET /api/v1/token` es público: cualquiera puede obtener un token, así que el
token identifica instalaciones y cuentas, pero no es por sí solo un control de
acceso. Para restringir contenido, usar `allowedUsers` en `version.json` y
vincular las cuentas autorizadas.
