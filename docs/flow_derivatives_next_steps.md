# Flow / Derivatives: next steps

## 1. Current live coverage

Сейчас в production реально используются только derivatives-метрики:

- funding
- open interest
- OI change
- price change

Этого достаточно для observe-only overlay и execution modifiers, но этого недостаточно для полного подтверждения market-wide liquidity flow.

Текущее ограничение должно оставаться явным в render/prompt/payload:

- derivatives-only != full liquidity-flow confirmation
- `unavailable` != `low`
- stale snapshot != usable current signal

## 2. Phase 2 scope

Следующий реальный этап: добавить два недостающих блока, не ломая текущую architecture.

Phase 2A:

- exchange pressure
- stablecoin support / drain

Phase 2B:

- tokenomics / unlock pressure

Это должен быть новый data layer для modifiers и disclosure, а не новый directional engine.

## 3. Target payload contract

Новый слой должен продолжать текущий формат `flow_derivatives_context_v2.json` и расширять его, а не переписывать:

- `coverage.derivatives.source`
- `coverage.exchange_flows.source`
- `coverage.stablecoin_flows.source`
- `coverage.tokenomics.source`
- `market_context.exchange_pressure`
- `market_context.stablecoin_support`
- `market_context.unlock_pressure`
- `market_context.flow_derivatives_modifiers`

Для каждого не-derivatives блока источник должен иметь один из статусов:

- `live`
- `mixed`
- `unavailable`
- `stale`

Правила интерпретации:

- `live`: данные свежие и покрывают нужную метрику напрямую.
- `mixed`: часть метрик live, часть proxy / lagged / incomplete.
- `unavailable`: данных нет, нельзя делать bearish/bullish вывод.
- `stale`: последнее известное состояние можно показать в diagnostics, но нельзя применять к текущему решению.

## 4. Phase 2A: exchange / stablecoin pressure

### 4.1 Required metrics

Минимальный набор для Phase 2A:

- BTC/ETH aggregate exchange reserve delta or netflow proxy
- pool-asset exchange pressure proxy for tracked universe (`BTC`, `ETH`, `BNB`, `SOL`, `XRP`)
- stablecoin supply delta
- stablecoin on-exchange balance delta or chain-level issuance / drain proxy
- freshness timestamp per source
- coverage per metric, а не только per file

### 4.2 Normalized interpretation

Нормализованные состояния:

- `exchange_pressure`: `high` | `medium` | `low` | `unavailable`
- `stablecoin_support`: `high` | `medium` | `low` | `unavailable`

Правила:

- высокий exchange inflow / reserve growth в risk assets = pressure up
- отток с бирж без stablecoin drain = pressure down / supply relief
- рост stablecoin supply / dry powder = support up
- падение stablecoin balances / issuance stall = support down
- если exchange и stablecoin сигналы конфликтуют, итоговый source state = `mixed`, а не forced low/high

### 4.3 Modifier mapping

Новые источники должны влиять только на modifiers:

- `exchange_pressure=high`:
  - повышает `chase_risk`
  - может повышать `positioning_risk`
  - чаще включает `confirmation_required=true`
- `exchange_pressure=low` + `stablecoin_support=high`:
  - допускает более clean continuation framing
  - но не переворачивает direction сам по себе
- `stablecoin_support=low`:
  - убирает лишний optimism для dip-buy / continuation
  - усиливает требование подтверждения

### 4.4 Implementation plan

1. Добавить source-adapter interface для flow group:
   - `fetch() -> raw`
   - `normalize() -> coverage + metrics + diagnostics`
   - `staleness_limit_minutes`
2. Добавить `exchange_flows` collector и `stablecoin_flows` collector как независимые optional adapters.
3. Собрать group-level normalizer:
   - raw metric -> normalized state
   - explicit `source`, `freshness_minutes`, `coverage_notes`
4. Расширить current `market_context.flow_derivatives_modifiers` только через overlay logic.
5. В render/prompt явно печатать source coverage:
   - `live`
   - `mixed`
   - `unavailable`
   - `stale, ignored for current decision`

## 5. Phase 2B: tokenomics / unlocks

### 5.1 Required metrics

Минимальный unlock block:

- next unlock timestamp
- unlock value USD
- unlock as % of circulating supply
- unlock as % of free float, если источник даёт
- unlock type / category, если доступно
- freshness timestamp
- per-asset coverage

### 5.2 Coverage expectations for current pool

Пул сейчас фиксированный:

- BTC
- ETH
- BNB
- SOL
- XRP

Ожидаемое поведение coverage:

- BTC: обычно `unavailable` / `not_applicable` для classic unlock pressure
- ETH: чаще `unavailable` или weak unlock relevance
- BNB / SOL / XRP: проверять индивидуально по поддержке источника

Важно: отсутствие unlock data по BTC/ETH не означает low risk; это означает, что unlock factor не участвует в оценке.

### 5.3 Modifier mapping

`unlock_pressure` должен быть строго advisory:

- `high`:
  - повышает `positioning_risk`
  - может повышать `confirmation_required`
  - может понижать confidence continuation
- `medium`:
  - усиливает caution, но не блокирует сделку
- `low`:
  - только снимает часть tokenomics pressure
- `unavailable`:
  - никакого implied relief

### 5.4 Implementation plan

1. Добавить tokenomics adapter отдельно от exchange/stablecoin.
2. Нормализовать unlock pressure через ближайшее окно:
   - `< 7d`
   - `< 30d`
   - outside window
3. Использовать asset-level mapping, затем поднимать summary в `market_context` только если источник действительно покрывает актив.
4. Печатать unlock coverage отдельно от derivatives coverage.

## 6. Stale guard and disclosure

Stale guard обязателен для каждого нового блока, не только для whole-file snapshot.

Минимальные правила:

- `exchange_flows`: stale after source-specific TTL
- `stablecoin_flows`: stale after source-specific TTL
- `tokenomics`: stale after source-specific TTL

Поведение:

- stale block не участвует в текущем modifier calculation
- stale block можно показывать как last known context в diagnostics
- render/prompt должны явно отмечать `ignored_for_current_decision=true`

## 7. Integration constraints

Нельзя:

- трактовать `unavailable` как `low`
- считать derivatives-only snapshot полным подтверждением liquidity / flow
- использовать derivatives-only bullish/bearish как замену exchange/stablecoin/tokenomics confirmation
- подмешивать stale блок в current modifiers
- silently collapse `mixed` в `low`

Нужно:

- держать source coverage явным в payload и render
- сохранять provenance и freshness по каждому group source
- применять новый слой как overlay/modifier only
- не менять direction logic
- не менять entry / SL / TP engine радикально

## 8. Verified source notes for next patch

На этом шаге большой collector не внедряется. По состоянию на 26 April 2026:

- CoinGecko API имеет free demo tier, но всё равно требует API key.
- CoinGlass exchange transparency / exchange asset endpoints требуют API key.
- DefiLlama public surface показывает stablecoin / unlock / transparency datasets, но API/download coverage для unlocks / CEX transparency ориентирована на Pro/API plans.

Практический вывод:

- clean no-credentials production source для всех нужных блоков сейчас не зафиксирован;
- самый реалистичный следующий шаг — выбрать low-friction source с key-based access и сразу строить explicit coverage/stale semantics, а не временно маскировать gaps.
