# [LESSONS]
Учитывай повторяющиеся ошибки и принятые фиксы при генерации сигнала. Ниже последние случаи (свежие внизу):

- 2025-10-21T20:30:00+03:00 • AAVE/USDT • result=skip; issues=[none]; fixes=[respect_cancel_rules]; note: Сетап подтверждён, но без безопасной точки входа. Пропуск корректный и дисциплинированный.
- 2025-10-21T20:30:00+03:00 • AAVE/USDT • result=skip; issues=[none]; fixes=[respect_cancel_rules]; note: Сетап подтверждён, но без безопасной точки входа. Пропуск корректный и дисциплинированный.
- 2025-10-21T10:30:00+03:00 • LINK/USDT • result=breakeven; issues=[btc_flip_ignored]; fixes=[btc_context_check]; note: Фаза корректного возврата к EMA20, но BTC сменил тон — решение выйти в breakeven оправдано.
- 2025-10-22T01:15:00+03:00 • LINK/USDT • result=loss; issues=[ema_touch_no_volume, weak_rsi, btc_context_ignored]; fixes=[require_m15_volume, wait_for_rsi>50, btc_context_check]; note: Вход от нижней границы 17.92 без свечи-объёма и при RSI<45, SL сработал сразу.
- 2025-10-22T09:30:00+03:00 • SUI/USDT • result=loss; issues=[counter_trend_entry, early_entry, no_htf_confirmation]; fixes=[restrict_counter_trend, wait_for_htf_volume, cancel_if_rsi<45]; note: Вход против локального тренда без подтверждения объёмом. RSI и старшие ТФ не подтверждали разворот. Стоп сработал быстро.
