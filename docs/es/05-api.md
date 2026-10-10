# 05 · API y Modelo de Datos

## 1. Particiones de API

| Partición | Entrada | Prefijo |
|---|---|---|
| API del sitio de usuario | `scripts/web_dashboard.py` (montaje unificado de routers) | `/api/user/*`, `/api/pm/*`, `/api/leaderboard/*`, `/api/public/*` |
| API de admin (privada) | `src/services/admin_v3/routes_admin.py` | `/admin-v3/api` (no incluida en esta distribución) |

## 2. Identidad y autenticación

- Canal de identidad de usuario único: cookie `user_session` (firmada con HMAC, derivada de
  `USER_SESSION_SECRET`).
- Inicio de sesión: `POST /api/user/privy/verify` — el access token de Privy se verifica en
  el servidor; la identidad proviene de la respuesta de la API de Privy, nunca del objeto
  `user` enviado por el cliente.
- MFA: TOTP en el servidor (ver [04](./04-getting-started.md) §6).

## 3. Endpoints clave (lado usuario)

| Endpoint | Propósito |
|---|---|
| `POST /api/user/privy/verify` | Inicio de sesión / establecimiento de sesión (con TOTP como segundo factor) |
| `GET /api/user/mfa/status` | Estado actual de MFA |
| `POST /api/user/mfa/enroll/start` | Iniciar inscripción TOTP (devuelve QR y secret) |
| `POST /api/user/mfa/enroll/confirm` | Verificar código de confirmación, completar inscripción |
| `POST /api/user/mfa/verify-mark` | Verificar código TOTP, emitir sesión firmada de 30 minutos |
| `POST /api/user/mfa/disable` | Verificar el código actual, desactivar MFA |
| `GET /api/user/referral` | Cadena de referidos, recompensas y paginación |
| `GET /api/pm/*` | Datos de Polymarket y acciones de fondos (deposit / withdraw / redeem / vault) |
| `GET /api/leaderboard/*` | Clasificaciones |
| `GET /api/public/*` | Datos públicos |
| `GET /api/copy-tasks` … | Tareas de copia (operaciones de admin) |

> La lista completa de rutas vive en `scripts/dashboard_routes/`. Las acciones de fondos
> (withdraw / redeem) resuelven la wallet del llamador desde la sesión y nunca aceptan una
> dirección de wallet del cuerpo de la petición.

## 4. Resumen del modelo de datos

| Tabla | Propósito |
|---|---|
| `users` | Tabla maestra de usuarios (wallet_address PK, rol, vínculo Privy, TOTP, estado MFA) |
| `user_sessions` | Sesiones (token HMAC, expiración, IP de origen) |
| `copy_tasks` | Tareas de copy-trade (globales, creadas por admins) |
| `point_transactions` / `points_tiers` | Libro mayor de puntos y niveles |
| `fee_config` / `fee_discount_tiers` | Tarifas y descuentos |
| `user_pm_profiles` | Perfiles de vínculo Polymarket del usuario |

Las definiciones de esquema viven en `scripts/init_pg_schema.sql` y las migraciones en
`src/utils/db.py`.
