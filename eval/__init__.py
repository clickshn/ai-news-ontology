"""평가(Evaluation) 레이어.

역할: extraction/ 의 출력 품질을 두 축으로 검증한다.
    1) 골든셋 대조 — 통제어휘 필드(기술영역/발표유형)와 영향도 점수를
       사람이 라벨링한 정답과 비교한다. 결정적(deterministic) 채점.
    2) LLM-judge — 요약문의 충실성/완결성/간결성을 1~5로 채점한다.
       자유서술이라 정답 문자열 비교가 불가능하므로 judge 를 쓴다.

구성:
    golden_set/     사람이 라벨링한 정답 (커밋 대상)
    judge_prompts/  judge 루브릭 프롬프트 (버전 관리 대상)
    scores/         실행 결과 (gitignore, .gitkeep 만 유지)
    runner.py       두 채점을 실행하고 scores/ 에 기록. TODO
    metrics.py      정확도/일치도(예: quadratic weighted kappa) 계산. TODO

주의: 패키지명 `eval` 은 파이썬 내장 함수 `eval()` 과 이름이 겹치지만,
      내장 함수는 모듈 네임스페이스를 가리지 않으므로 충돌하지 않는다.
      (README 결정 로그 D-005)
"""
