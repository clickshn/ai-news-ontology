# eval/scores/

`eval/runner.py` 실행 결과가 쌓이는 곳. **gitignore 대상** (`.gitkeep` 만 커밋).

파일명: `{yyyymmdd-HHMMSS}-{git_sha}.json`

기록할 내용 (재현에 필요한 것 전부):
- 실행 시각, git commit SHA
- 사용한 모델 ID / effort / 프롬프트 파일 해시
- 골든셋 버전(건수 + 해시)
- 항목별 점수와 집계 지표
- 임계값 통과 여부

프롬프트나 모델을 바꿨을 때 점수가 왜 움직였는지 추적하려면 위 메타데이터가
점수 자체보다 중요하다.
