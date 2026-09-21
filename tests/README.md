# tests/

`.venv/Scripts/python.exe -m pytest tests/ -q` 로 돌린다. 시스템 파이썬에는
`feedparser` 등이 없어 수집 모듈 import 에서 깨진다.

## 오프라인 보장 — 이 디렉터리의 전제

**테스트는 루프백 밖으로 나가지 않는다.** `conftest.py` 의 `no_outbound_network` 가
**모든 테스트에 무조건**(autouse) 걸려 있고, 판정 로직은 `offline_guard.py` 에 있다
(D-074). 연결과 **이름 해석(DNS)** 을 함께 가로채며, 루프백만 통과시킨다.

| | |
|---|---|
| 왜 전량 차단이 아닌가 | 타임아웃 검사가 **루프백 실소켓**을 쓴다 (D-071). 막으면 "우리가 던진 예외를 우리가 잡았다"로 되돌아간다 |
| 무엇을 보장하지 **않는가** | 잡는 것은 **"나갔다"** 지 "나가지 않는다"가 아니다. 목을 거는 층이 바뀌면 요청은 여전히 만들어지고, 다만 **즉시 실패로 드러난다** |
| 터졌다면 | 트립와이어를 끄지 말고 **목을 거는 층을 내린다.** session-07 이 그 사고였다 — 목이 한 층 위로 올라가면서 3건이 진짜 arXiv 로 나갔다 |

`test_offline_guard.py` 는 **이 장치 자체**를 잰다. 전체가 초록불이라는 사실은
트립와이어가 돈다는 증거가 아니다 — 아예 안 걸려 있어도 초록불은 똑같이 나온다.

## 그 밖의 전제

- **실제 Obsidian Vault 에 쓰지 않는다.** 모든 쓰기는 `tmp_path` 안에서만 일어난다.
  관측 로그(`occurrence_count` 누적이 오염된다)와 보존소(`data/extractions/`),
  `data/replays/` 도 같다.
- **다른 레포를 입력으로 읽지 않는다.** MARA 코퍼스·골든셋이 필요하면 `make_mara_root`
  픽스처로 `tmp_path` 에 그 모양을 흉내 낸다.
- 공용 입력 공장은 `conftest.py` 에 있다 — `make_payload` / `make_store` /
  `make_mara_root`.

## 게이트 검사

`gate/external_llm_cases.sh` 는 pytest 밖에서 돈다. 벤더 호출 차단 훅이 도구 계층에서
같은 판정을 하는지 46건으로 확인한다.

```bash
bash tests/gate/external_llm_cases.sh
```
