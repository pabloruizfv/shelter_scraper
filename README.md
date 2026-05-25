# Monitor de disponibilidad de Respomuso

Pequeno monitor en Python para consultar la disponibilidad del refugio de Respomuso y guardar una serie temporal en SQLite. Esta pensado para ejecutarse periodicamente con GitHub Actions y avisar cuando cualquier fecha de alerta tenga plazas disponibles.

## Como funciona

El script consulta el endpoint ligero que usa el widget de reservas:

`https://api.alberguesyrefugios.com/refugios/get/9/getPlazas2/`

Hay dos niveles de fechas:

- `tracked_dates`: fechas que se consultan y se guardan siempre en SQLite.
- `alert_dates`: subconjunto de `tracked_dates` que dispara aviso si en la ejecucion actual tiene plazas disponibles.

Para cada fecha monitorizada:

- `green`: hay al menos una plaza con `estado = 1`.
- `red`: no hay plazas disponibles o la fecha no aparece en la respuesta.
- `available_places`: suma de plazas disponibles en habitaciones con `estado = 1`.

La base de datos se crea automaticamente en `data/availability.sqlite`. La tabla principal es `availability_snapshots`: cada fila es una combinacion de momento de busqueda (`checked_at`) y fecha (`target_date`).

Columnas principales:

- `checked_at`: momento UTC de la busqueda.
- `target_date`: fecha consultada.
- `status`: `green` o `red`.
- `is_available`: `1` si hay plazas reservables, `0` si no.
- `available_places`: numero exacto de plazas reservables detectadas.
- `raw_payload`: detalle crudo por habitacion para auditoria.

No hay una tabla separada de alertas. La notificacion se decide en cada ejecucion mirando si alguna fecha de `alert_dates` esta en `green`.

## Configuracion local

Instala dependencias:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copia la configuracion:

```bash
cp config.example.yaml config.yaml
```

Edita `config.yaml` con tus fechas y ejecuta:

```bash
python monitor.py --config config.yaml
```

Tambien puedes usar variables de entorno:

```bash
TRACKED_DATES=2026-07-12,2026-07-13,2026-07-14 \
ALERT_DATES=2026-07-12,2026-07-14 \
python monitor.py
```

`TARGET_DATES` sigue funcionando como alias antiguo de `TRACKED_DATES`, pero se recomienda usar `TRACKED_DATES`.

## Notificaciones

La opcion mas simple es ntfy:

```bash
export NOTIFY_PROVIDER=ntfy
export NTFY_TOPIC=un-topic-largo-y-privado
```

Suscribete en el movil al topic `un-topic-largo-y-privado` en la app de ntfy. El script envia aviso en cada ejecucion en la que alguna fecha incluida en `alert_dates` este en `green`.

Para Telegram:

```bash
export NOTIFY_PROVIDER=telegram
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=123456789
```

Para probar sin enviar avisos:

```bash
DRY_RUN_NOTIFICATIONS=true python monitor.py --config config.yaml
```

## GitHub Actions

El workflow `.github/workflows/availability-monitor.yml` se ejecuta cada 30 minutos y tambien manualmente con `workflow_dispatch`.

Configura en GitHub:

- `Settings > Secrets and variables > Actions > Variables`
- Variable `TRACKED_DATES`: `2026-07-12,2026-07-13,2026-07-14`
- Variable `ALERT_DATES`: `2026-07-12,2026-07-14`
- Variable opcional `NOTIFY_PROVIDER`: `ntfy`, `telegram` o `none`

Secrets para ntfy:

- `NTFY_TOPIC`, o bien `NTFY_URL`

Secrets para Telegram:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

El workflow guarda el historico en `data/availability.sqlite` y commitea el fichero de vuelta al repo si ha cambiado.

## Consultar la serie temporal

```bash
sqlite3 data/availability.sqlite \
  "select checked_at, target_date, status, is_available, available_places from availability_snapshots order by checked_at, target_date;"
```

Cada ejecucion anade una fila por fecha monitorizada. La notificacion se decide con el snapshot recien calculado para las fechas de `alert_dates`.

## Notebook de visualizacion

Hay un notebook en `notebooks/view_db.ipynb` para ver el estado actual, la tabla historica y una visualizacion temporal por fecha.

Dependencias opcionales:

```bash
pip install -r requirements-notebook.txt
jupyter lab
```
