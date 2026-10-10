# 03 · Core Features

A unified Landing page is the entry; four core pages deliver distinct user value.

## 1. WeatherEventMap — weather event map

A visual entry for weather-market traders.

- **Map overview**: city points, event heat and status.
- **City detail**: click a city for city-level weather and related markets.
- **History & trends**: price history, market trade history, city history.
- **Live price sync**: pulls live prices for in-play decisions.
- **Position linkage**: relates the user's bets / open positions at city level.
- **Charts**: Leaflet + Chart visualisation.

## 2. SportsLive — live sports odds

Odds and opportunity pages for sports-market traders.

- **Event card stream**: live / upcoming state, matchups, probabilities, odds.
- **Multi-sport filter**: all and per-sport filtering.
- **State filter**: All / Live / Upcoming view switching.
- **SSR first paint**: fast first-screen load.
- **Promo slots**: AI banner + page banner (admin-configurable).
- **Detail routing**: card click opens `/event/{slug}`.

## 3. ValueLab — value laboratory

A quantitative opportunity page for value investors.

- **Category tags**: Crypto / Earnings / Economy / Finance.
- **Search & sort**: filter by score, EV, update time.
- **SSR + progressive loading**: skeleton first paint + scroll pagination.
- **Data health**: freshness grade and lag visualisation.
- **Value cards**: implied prob, value score, EV, recommendation.
- **Detail routing**: opens `/valuelab/{slug}`.

## 4. Copytrader — smart copy trading

A copy-trading tool for users who don't want to trade manually.

- **Wallet filter & search**: filter by score, PNL, win rate, trade count, category.
- **Leaderboard cards**: score, PNL, win rate, risk labels (incl. bot risk).
- **Wallet analysis tabs**: analytics / positions / activity / closed positions.
- **Task management**: create / edit / pause / resume / delete copy tasks.
- **Global settings**: copytrader switch and parameter config.
- **Health check**: status endpoint for observability and safety alerts.

## 5. Landing page

The unified Landing aggregates the four entries, with:

- Carousel cards (TABS: Veyra / Weather / Sports / ValueLab / Copytrader).
- Drag / wheel / keyboard card switching.
- Multilingual (i18n) copy.
- Brand visuals consistent with the VeYra logo and character language.
