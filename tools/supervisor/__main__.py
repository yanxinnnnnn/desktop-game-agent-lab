"""本地记账 CLI：默认离线，不提供任意命令、原始日志或客户端启动入口。"""

import argparse
import json
from uuid import uuid4

from tools.notifications import NotificationSender
from . import Supervisor


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Supervisor 参数无效；详情已抑制，请使用 --help。\n')


def _synthetic(task, case):
    event = dict(schema_version='1.0', task_id=task['task_id'], event_id=str(uuid4()),
                 attempt_no=task['attempt_no'], agent=task['agent'], issue_url=task['issue_url'],
                 source='synthetic', kind='finished', exit_code=1, http_status=None, signal='none',
                 retry_after_seconds=None, resume_token=None)
    if case == 'quota':
        event.update(signal='quota_exhausted', http_status=429)
    elif case == 'rate-limit':
        event.update(http_status=429, retry_after_seconds=60)
    elif case == 'auth':
        event['http_status'] = 401
    elif case == 'timeout':
        event['signal'] = 'timeout'
    elif case == 'success':
        event['exit_code'] = 0
    elif case in ('founder-merge', 'founder-game'):
        event.update(kind='founder_merge_required' if case == 'founder-merge' else 'founder_game_action_required',
                     exit_code=None)
    return event


def _main():
    parser = _SafeParser(description='离线优先 M0.2a 检查点；不运行 Codex/DeepSeek/游戏。')
    parser.add_argument('--base-dir', required=True, help='Git 仓库之外的本地检查点目录；不输出该路径。')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'check'):
        commands.add_parser(name).add_argument('--task-id')
    simulation = commands.add_parser('simulate')
    simulation.add_argument('--case', choices=('quota', 'rate-limit', 'auth', 'timeout', 'unknown',
                                               'success', 'founder-merge', 'founder-game'), default='quota')
    simulation.add_argument('--agent', choices=('codex', 'deepseek'), default='codex')
    simulation.add_argument('--issue-number', type=int, default=18)
    for name in ('pause', 'resume', 'notify'):
        command = commands.add_parser(name)
        command.add_argument('--task-id', required=True)
        if name == 'resume':
            command.add_argument('--acknowledge', action='store_true')
        elif name == 'notify':
            command.add_argument('--enable-notifications', action='store_true')
    args = parser.parse_args()
    try:
        enabled = args.command == 'notify' and args.enable_notifications
        supervisor = Supervisor(args.base_dir, sender=NotificationSender() if enabled else None)
        if args.command == 'simulate':
            result = supervisor.register(str(uuid4()),
                f'https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/{args.issue_number}', args.agent)
            if result['Ok']:
                event = _synthetic(result['Data'], args.case)
                started = event | {'event_id': str(uuid4()), 'kind': 'started', 'exit_code': None,
                                   'http_status': None, 'signal': 'none', 'retry_after_seconds': None}
                result = supervisor.ingest(started)
                if result['Ok']:
                    result = supervisor.ingest(event)
        elif args.command in ('status', 'check'):
            result = getattr(supervisor, args.command)(args.task_id)
        elif args.command == 'pause':
            result = supervisor.pause(args.task_id)
        elif args.command == 'resume':
            result = supervisor.resume(args.task_id, acknowledge=args.acknowledge)
        else:
            result = supervisor.notify(args.task_id, enabled=enabled)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['Ok'] else 1
    except Exception:
        print('{"Ok": false, "Code": "E_CLI", "Message": "操作失败；路径及异常详情已抑制。", "Data": null}')
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
