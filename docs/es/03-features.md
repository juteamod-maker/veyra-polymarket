# 03 · Funciones Principales

Una página Landing unificada es la entrada; cuatro páginas principales ofrecen valor
distinto al usuario.

## 1. WeatherEventMap — mapa de eventos meteorológicos

Una entrada visual para traders de mercados meteorológicos.

- **Resumen del mapa**: puntos de ciudades, calor de eventos y estado.
- **Detalle de ciudad**: clic en una ciudad para ver el clima a nivel de ciudad y mercados relacionados.
- **Historial y tendencias**: historial de precios, historial de operaciones del mercado, historial de la ciudad.
- **Sincronización de precios en vivo**: obtiene precios en vivo para decisiones en curso.
- **Vinculación de posiciones**: relaciona apuestas / posiciones abiertas del usuario a nivel de ciudad.
- **Gráficos**: visualización con Leaflet + Chart.

## 2. SportsLive — cuotas deportivas en vivo

Páginas de cuotas y oportunidades para traders de mercados deportivos.

- **Flujo de tarjetas de eventos**: estado en vivo / próximos, enfrentamientos, probabilidades, cuotas.
- **Filtro multi-deporte**: filtrado general y por deporte.
- **Filtro de estado**: cambio de vista All / Live / Upcoming.
- **Primera pintura SSR**: carga rápida de la primera pantalla.
- **Espacios promocionales**: banner de IA + banner de página (configurables por admin).
- **Ruta a detalle**: clic en la tarjeta abre `/event/{slug}`.

## 3. ValueLab — laboratorio de valor

Una página de oportunidades cuantitativas para inversores de valor.

- **Etiquetas de categoría**: Crypto / Earnings / Economy / Finance.
- **Búsqueda y ordenación**: filtrar por score, EV, tiempo de actualización.
- **SSR + carga progresiva**: primera pintura de esqueleto + paginación por scroll.
- **Salud de datos**: visualización de freshness grade y lag.
- **Tarjetas de valor**: probabilidad implícita, value score, EV, recomendación.
- **Ruta a detalle**: abre `/valuelab/{slug}`.

## 4. Copytrader — copy trading inteligente

Una herramienta de copy trading para usuarios que no quieren operar manualmente.

- **Filtro y búsqueda de wallets**: filtrar por score, PNL, tasa de acierto, número de operaciones, categoría.
- **Tarjetas de líderes**: score, PNL, tasa de acierto, etiquetas de riesgo (incl. riesgo de bots).
- **Pestañas de análisis de wallet**: analytics / positions / activity / closed positions.
- **Gestión de tareas**: crear / editar / pausar / reanudar / eliminar tareas de copia.
- **Configuración global**: interruptor del copytrader y configuración de parámetros.
- **Health check**: endpoint de estado para observabilidad y alertas de seguridad.

## 5. Página Landing

La Landing unificada agrega las cuatro entradas, con:

- Tarjetas de carrusel (TABS: Veyra / Weather / Sports / ValueLab / Copytrader).
- Cambio de tarjeta por arrastre / rueda / teclado.
- Textos multilingües (i18n).
- Visuales de marca consistentes con el logo y el lenguaje del personaje VeYra.
