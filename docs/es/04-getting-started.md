# 04 · Primeros Pasos

## 1. Requisitos

- Python 3.12+
- PostgreSQL 14+
- Una app de [Privy](https://privy.io) (App ID / App Secret / clave de autorización)
- Credenciales de Polymarket CLOB v2 (`py-clob-client-v2`)
- Opcional: notificaciones de Telegram, clave RapidAPI/OddsAPI de cuotas deportivas

## 2. Configuración

```bash
cd polymarket_bot_v2
cp .env.example .env        # rellene valores reales; .env nunca se versiona
```

Las variables clave se listan en el [README.md](../README.md) §4 de la raíz.

## 3. Ejecución local

```bash
cd polymarket_bot_v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# inicializar el esquema de la base de datos
bash scripts/setup_postgres_local.sh
psql -d polymarket_v2 -f scripts/init_pg_schema.sql

# iniciar el sitio de usuario / API (puerto 8080 por defecto)
uvicorn scripts.web_dashboard:app --host 127.0.0.1 --port 8080 --proxy-headers
```

> Esta distribución no incluye la consola Admin V3, por lo que no hay servicio 8081 que iniciar.

## 4. Pruebas

```bash
cd polymarket_bot_v2
pytest -q                                   # ver rutas y marcadores en pytest.ini
pytest -q tests/test_mfa_totp.py            # pruebas unitarias de TOTP MFA en el servidor
```

Algunas pruebas requieren un PostgreSQL local y un `config/config.json` poblado; que no
se conecten en un checkout vacío es el comportamiento esperado.

## 5. Despliegue

El punto de entrada de despliegue es el script del propio repositorio:

```bash
bash polymarket_bot_v2/scripts/deploy_vps.sh update
```

`scripts/deploy_vps_v2.sh` lee el alias del host de destino desde `VPS_HOST`; reemplácelo
por su propio host SSH. Cuando el módulo privado Admin V3 está ausente, el script omite
automáticamente todos los pasos de admin.

## 6. MFA (verificación en dos pasos)

El inicio de sesión de usuario admite **TOTP en el servidor** (compatible con Google
Authenticator):

- Inscripción: `POST /api/user/mfa/enroll/start` devuelve un código QR y el secret;
  `POST /api/user/mfa/enroll/confirm` verifica el código de confirmación.
- Verificación: `POST /api/user/mfa/verify-mark` verifica un código TOTP de 6 dígitos y
  emite un marcador de sesión firmado de 30 minutos.
- Inicio de sesión: `POST /api/user/privy/verify` lleva el código TOTP en `mfa_code`.
- Desactivar: `POST /api/user/mfa/disable` verifica el código actual antes de desactivar.

El flujo de inicio de sesión del front-end muestra una entrada TOTP; la página de perfil
ofrece puntos de inscripción / verificación / desactivación.
