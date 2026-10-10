# 01 · Producto y Lógica de Negocio

## 1. Posicionamiento

Veyra.Trade es una plataforma multi-usuario de trading y operaciones sobre Polymarket,
no un script de bot en una sola máquina. Conecta «descubrimiento de mercados →
autorización de cuentas → ejecución de operaciones → liquidación de activos →
gobernanza de operaciones» en un solo ciclo auditable.

## 2. Tres superficies de ejecución

| Superficie | Entrada | Audiencia | Responsabilidades |
|---|---|---|---|
| Sitio de usuario + API de Dashboard | `polymarket_bot_v2/scripts/web_dashboard.py` | Usuarios | Renderizado, APIs pre/post-trade, sesiones y permisos, i18n |
| Sidecar Copytrader V2 | `polymarket_bot_v2/src/services/copytrader_v2/daemon.py` | Sistema | Ejecución de copy-trade, ciclo de vida independiente, aislamiento de recursos |
| Consola Admin V3 (privada) | `src/services/admin_v3/` (no publicada) | Ops / riesgo / admins | Gobernanza de configuración, puntos y tarifas, CMS, observabilidad |

## 3. Flujos de negocio principales

1. **Adquisición y registro** — los usuarios se registran/inician sesión, pueden adjuntar
   un código de referido y recibir puntos de registro según las reglas.
2. **Descubrimiento y órdenes** — filtran mercados en las páginas discover/event y envían
   órdenes; el sistema valida, gestiona nonce/firma y el envío al relayer.
3. **Fondos y activos** — deposit / withdraw / redeem son rutas de alto riesgo con
   registros observables y diagnósticos de fallos.
4. **Referidos y puntos** — referidos multinivel (L1/L2/L3); las recompensas son
   idempotentes, seguras contra bucles y antiabuso.
5. **Gobernanza de operaciones** — los admins configuran puntos, tarifas, textos y estado
   en ejecución; todas las acciones privilegiadas son auditables.

## 4. Principios de diseño

1. **Capacidades reales primero** — sin botones, guardados o estados falsos; cada control
   habla con una API real.
2. **Aislamiento multi-tenant primero** — los datos sensibles solo se leen del contexto de sesión.
3. **Auditabilidad primero** — despliegue, configuración y diagnósticos on-chain son trazables.
4. **Frontera V2 primero** — sin retrocesos al runtime V1 de un solo usuario.

## 5. Cómo ganan los operadores (Programa Builder de Polymarket)

Este proyecto se construye sobre el [Programa Builder de Polymarket](https://docs.polymarket.com/programs/builders/overview):
tu sitio enruta las órdenes de los usuarios al libro de órdenes de Polymarket, y cada
orden ejecutada lleva tu Builder Code, de modo que las comisiones, reembolsos y
recompensas de referidos se te atribuyen a ti.

| Flujo | Mecanismo | Ganas |
|---|---|---|
| **Comisión Builder** | Las órdenes ejecutadas llevan tu `bytes32` Builder Code; la comisión se acredita a tu cuenta Builder | Taker ≤ **1,00 %**, Maker ≤ **0,50 %** (autoconfigurado dentro de los límites, adicional a las comisiones de plataforma) |
| **Referidos** | Una parte de las comisiones netas que generan tus referidos | Directo **10 %**, indirecto **5 %**, pagado a diario en pUSD |
| **Reembolso Taker** | El volumen taker de tus usuarios acumula volumen ponderado, subiendo de nivel para reembolsos diarios | Bronze → Obsidian, 7 niveles |
| **Copytrader / premium** | Copy trading de Smart Money y estrategias automatizadas | Precio de suscripción / complemento que tú defines |
| **Admin V3 + alojamiento** | Consola privada y despliegue gestionado | Licencia comercial / tarifa de alojamiento (ver [07](./07-admin-console.md)) |

> **Las comisiones van directamente a ti**: configura `POLY_BUILDER_CODE` en `.env` para
> habilitar la atribución. Los límites y reglas los fija Polymarket — consulta
> [Builder Fees](https://docs.polymarket.com/programs/builders/fees) y la documentación oficial.

## 6. Negocio / asociación

Esta distribución open-source cubre el sitio de usuario, la API, el copytrader y el motor
de estrategia (Apache-2.0). La **consola Admin V3 es un módulo privado**: obténgala mediante
el nivel de patrocinio o una licencia comercial; véase [07](./07-admin-console.md).
