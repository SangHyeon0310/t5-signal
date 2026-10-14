# t5-signal — 무료 금(XAUUSD) 신호 알림 (반자동, 주문 없음)

매시간 GitHub Actions(공개 저장소라 무료)가 금 시세를 받아 T5 전략 H0/D1의 목표 비중을 계산하고,
바뀌면 텔레그램(또는 ntfy)으로 알립니다. 주문은 MT5 앱에서 직접 넣습니다. 브로커 로그인 정보는 쓰지 않습니다.

- 전략 원본·연구·검증 기록: 비공개 저장소 `SangHyeon0310/T5` (`research/GOLD_PROTOCOL_V1.md`, `V2.md`)
- 시세: PAXG(금 토큰, Binance 공개 데이터)를 금 장 시간만 사용, 실패 시 야후 금 선물(GC=F).
  연구용 현물 데이터 대비 H0 판단 일치율 98% (2025-01 ~ 2026-09)
- 투자 조언이 아닙니다. 신호는 과거 검증 결과일 뿐 미래 수익을 보장하지 않습니다.

## 설정
1. 텔레그램 @BotFather(파란 체크) → `/newbot` → 토큰 받기. 만든 봇에게 `/start` 보내기.
2. Settings → Secrets and variables → Actions → `TELEGRAM_BOT_TOKEN` 등록.
3. Actions → t5-signal → Run workflow → 봇이 "이 채팅의 ID"를 보냄 → `TELEGRAM_CHAT_ID` 등록 → 한 번 더 Run workflow.
4. `config/signal_cloud.json`의 `equity_usd`(계좌 달러), `contract_size`(1랏 온스 수: 표준 100, XAUUSD247 1) 수정.

ntfy를 쓰려면 `NTFY_TOPIC` Secret에 추측하기 어려운 주제 이름을 넣습니다.
상태 기록은 `t5-signal-state` 브랜치에 저장됩니다.
