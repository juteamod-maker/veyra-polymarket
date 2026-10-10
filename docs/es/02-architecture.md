# 02 · Arquitectura y Límites

## 1. Servicios y puertos

| Unidad | Punto de entrada | Puerto | Notas |
|---|---|---|---|
| `polymarket-dashboard-v2` | `scripts.web_dashboard:app` | `8080` (público, tras una puerta de enlace) | Sitio de usuario + API |
| `polymarket-copytrader-v2` | `python3 -m src.services.copytrader_v2.daemon` | — (demonio) | Sidecar de copy-trade |
| `polymarket-admin-v3` | `src.services.admin_v3.app:app` | `127.0.0.1:8081` (solo local) | Consola privada, no incluida en esta distribución |

## 2. Límites de código

| Ruta | Responsabilidad |
|---|---|
| `scripts/web_dashboard.py` | Entrada del sitio de usuario, monta routers vía `include_router` |
| `scripts/dashboard_routes/` | Módulos de rutas del usuario (auth / user / discover / copytrader / redeem_withdraw, …) |
| `scripts/frontend/templates/` + `scripts/frontend/static/js/` | Plantillas y JS de cara al usuario |
| `src/services/copytrader_v2/` | Sidecar de copy-trade |
| `src/` | Motor de estrategia, sincronización de datos, utilidades on-chain |

## 3. Límites de configuración

| Configuración | Ruta |
|---|---|
| Configuración general | `config/config.json` (ignorada por git; ver `config/config_docs.json`) |
| Configuración del copytrader | `config/copytrader_v2.json` |
| Plantilla de entorno | `.env.example` |
| Diccionarios i18n | `scripts/frontend/static/i18n/*.json` |

## 4. Puntos de entrada heredados prohibidos

- `main.py`
- `polymarket-bot*.service` (servicios V1 de un solo usuario)
- la pestaña de Logs heredada y el `/api/logs` heredado
- cualquier retroceso al auto-trading V1 de un solo usuario

## 5. Restricciones on-chain y de seguridad

1. Las constantes on-chain provienen solo de `src/utils/pm_v2_constants.py`.
2. Las consultas de nonce SAFE usan el EOA propietario.
3. La construcción de transacciones Safe reutiliza helpers unificados — sin bifurcaciones ad-hoc.
4. Se conservan los registros de diagnóstico `[Relayer 4xx]` para la resolución de problemas.

## 6. Estrategia de cambios

1. Cambiar primero los puntos de entrada y las rutas centrales, luego la UI.
2. Cualquier cambio que toque permisos / fondos / recompensas de referidos debe actualizar
   la documentación y las notas de seguridad.
3. Evitar cambios transversales grandes; aterrizar un incremento verificable a la vez.
