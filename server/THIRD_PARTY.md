# 외부 구성 요소

`assets/player.js`, `assets/player.css`는 기존 운영 서버에서 가져온
[asciinema-player](https://github.com/asciinema/asciinema-player) 배포본이다.
Apache-2.0 라이선스 원문은 `assets/asciinema-player-LICENSE`에 포함한다.
정확한 원본 버전은 이전 배포 기록에 남아 있지 않으므로 버전을 추정하지 않았다.
이번 통합에서 두 파일의 내용은 변경하지 않았다. 다음 업데이트 시 upstream 버전과
체크섬을 고정하고 기존 녹화의 재생 호환성을 확인한다.

`assets/replay.js`, `assets/replay.css`는 Netlab 리플레이 UI다.
JWT 검증에는 Ubuntu의 `python3-jwt`, `python3-cryptography`를 사용한다.
실시간 채팅 서버에는 [websockets](https://github.com/python-websockets/websockets)
15.0.1 (BSD-3-Clause)을 사용한다. 격리 venv에 설치하며 라이선스는 설치된 배포본에 포함된다.
게스트의 ttyd, asciinema, Docker, containerlab과 실습 컨테이너는 별도 설치하며
각 프로젝트의 라이선스를 따른다.
