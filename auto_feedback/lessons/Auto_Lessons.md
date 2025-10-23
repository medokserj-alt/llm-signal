AUTO_LESSONS v1.8
- Trend Consistency Filter (soft): если на H4 цена < EMA60 или на H1 цена < EMA60 — пометь risk=Low и добавь флаг counter_trend в risk_flags (вход не запрещён).
- RSI Trend Alignment (soft): если RSI(H1) < 45 — пометь risk=Low и добавь флаг weak_htf_rsi.
- Volume Pre-Confirmation (soft): вход после закрытия свечи над EMA20(M15) с объёмом ≥ 1.2× среднего (20 свечей); иначе risk=Low и флаг no_m15_volume.
- Multi-TF EMA Check (soft): M15 и H1 EMA-фаны должны совпадать по направлению; при расхождении — risk=Low и флаг htf_conflict.
- Итоговая маркировка риска (не меняя JSON-схему): В начале поля "technical_rationale" добавляй префикс: "risk=<High|Medium|Low>; flags=[flag1,flag2] — ".

# Update 2025-10-23T10:47:16Z
- Soft-guard: не входить в long при RSI(H1)<45 без объёмного подтверждения на M15.
