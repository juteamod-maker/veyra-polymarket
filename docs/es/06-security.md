# 06 · Modelo de Seguridad

La política de seguridad completa está en el [SECURITY.md](../SECURITY.md) de la raíz.
Esta guía ofrece un resumen.

## 1. Modelo de identidad

- `user_session` es el único canal de identidad de usuario, firmado con HMAC-SHA256,
  derivado de `USER_SESSION_SECRET`.
- Los bypass heredados `DASHBOARD_KEY` / TOTP / `?key=` fueron eliminados y se rechazan
  explícitamente.
- Inicio de sesión Privy: el access token se verifica en el servidor; wallets, wallet
  embebida y correo provienen **solo de la respuesta de la API de Privy** — el
  `user.linkedAccounts` enviado por el cliente se ignora.

## 2. MFA (verificación en dos pasos)

- El MFA de inicio de sesión se impone mediante **TOTP en el servidor**: los usuarios con
  `totp_enabled` deben proporcionar un código TOTP válido de 6 dígitos para iniciar sesión.
- El marcador de sesión `mfa_verified` está firmado con HMAC y no puede falsificarse.
- El booleano `mfa_verified` del cuerpo de la petición se ignora.

## 3. Límites de autorización

- Las mutaciones de estado global (`/api/emergency-freeze`, `/api/weather-cities*`,
  `/api/evolution/trigger`, `/api/risk/resume`, `/api/map/markers*`, `/api/copy-tasks*`)
  requieren admin / super_admin.
- Las sentencias de Copytrader V2 están acotadas por `owner_wallet_address`.
- Las acciones de fondos resuelven la wallet desde la sesión, no del cuerpo de la petición.

## 4. Inyección y codificación segura

- SQL totalmente parametrizado; los identificadores dinámicos provienen solo de listas
  blancas fijas o de validación de enteros.
- Sin `shell=True` / `eval` / `exec` / `pickle.load` / `yaml.load`.
- El JSON incrustado en `<script>` pasa por `escape_script_json`, que escapa `</`.
- Las URLs salientes usan hosts fijos; la entrada del usuario solo llega a segmentos
  query/path.
- Las cookies (`user_session` / `privy-*-token` / `mfa_verified`) son
  `Secure + HttpOnly + SameSite`.
- El secreto de sesión no tiene fallback fijo; si no está configurado, se usa una clave
  aleatoria efímera y se emite un log CRITICAL.

## 5. Restricciones on-chain

- Las constantes on-chain provienen solo de `src/utils/pm_v2_constants.py`.
- Las consultas de nonce SAFE usan el EOA propietario.
- La construcción de transacciones Safe reutiliza helpers unificados.
- Se conservan los registros de diagnóstico `[Relayer 4xx]`.

## 6. Reporte de vulnerabilidades

No las divulgue públicamente. Repórtelas de forma privada vía GitHub Security Advisories
o por correo; ver [SECURITY.md](../SECURITY.md).
