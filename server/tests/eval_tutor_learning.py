#!/usr/bin/env python3
"""Explicit live-model smoke eval; synthetic observations, no learner VM changes.

Run on the installed test server: python3 tests/eval_tutor_learning.py
Uses the configured model endpoint. This is not evidence of human learning gains.
"""
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tutor_chat as chat


def main():
    cases = [
        ('01', 'ARP의 역할과 초기 h2/h3 주소를 짧게 설명해 줘.', 0, 'chat'),
        ('04', '정답은 말하지 말고 힌트만 주세요. 왕복 경로를 어디서 비교해야 하나요?', 1, 'hint'),
        ('04', '원인은 돌아오는 경로가 없는 것이고 근거는 r2 라우팅 테이블이에요. 이 설명에서 부족한 근거는?', 2, 'reflection'),
    ]
    with tempfile.TemporaryDirectory(prefix='netlab-learning-eval-') as folder, \
            patch.object(chat, 'DB', folder + '/chat.sqlite3'):
        for index, (chapter, question, level, mode) in enumerate(cases):
            sid = chat.handle('eval', 'synthetic-fixture', 'eval', {})['session']
            with chat.connection() as db:
                db.execute('INSERT INTO jobs (id,session,request,question,status,created,help_level,mode) VALUES (?,?,?,?,?,?,?,?)',
                           (str(index), sid, 'eval-' + str(index), question, 'pending', time.time(), level, mode))
            with patch.object(chat.tutor, 'deployed_chapter', return_value=chapter), \
                    patch.object(chat.tutor, 'lxc_exec', return_value=(0, 'synthetic-fixture', '')), \
                    patch.object(chat.tutor, 'node_exec', return_value=(0, '관찰 자료 없음', '')):
                chat.respond(sid, str(index), 'synthetic-fixture', False, shared_vm=True)
            with chat.connection() as db:
                job = db.execute('SELECT * FROM jobs WHERE id=?', (str(index),)).fetchone()
            answer = job['answer'] or ''
            checks = {'answer_generated': job['status'] == 'done' and len(answer) > 20,
                      'no_incorrect_01_address': chapter != '01' or '10.20.20.1' not in answer,
                      'no_fix_commands': not re.search(r'ip route (add|replace|del)|iptables\s.*-[ADF]', answer)}
            print(json.dumps(dict(case=index, chapter=chapter, checks=checks, answer=answer), ensure_ascii=False), flush=True)
            if not all(checks.values()):
                raise SystemExit('Live-model smoke eval failed')


if __name__ == '__main__':
    main()
