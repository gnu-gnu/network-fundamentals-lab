"""Netlab 관리 서비스(labd)와 CLI가 공유하는 패키지.

모듈 경계
  paths       호스트 경로·리스너 상수 (한 곳에서만 정의)
  util        시각·로그·원자적 쓰기·계정명 규칙
  procs       외부 명령 실행 (lxc 포함) — 셸을 거치지 않는다
  store       설정/상태/사용자 테이블과 전역 잠금
  access      Cloudflare Access JWT 검증과 계정 자동 생성
  nginx       계정·인스턴스 현황에 맞춘 nginx 스니펫 생성
  instances   VM 생성·배정·시작·정지·파기·초기화
  recordings  세션 기록 조회·회수
  maintainer  배경 유지 작업과 재시작 시 상태 정합
  health      관리자 콘솔용 감시 지표
  db          SQLite 연결 컨텍스트 (채팅·공지·튜터 공용)
"""
