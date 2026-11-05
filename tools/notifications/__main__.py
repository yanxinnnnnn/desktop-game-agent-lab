"""仅提供固定 NOTIFIER_TEST；默认 dry-run，不接受外部消息正文或密钥参数。"""

import argparse
import json
from uuid import uuid4

from . import NotificationSender, build_event


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # 不回显未知参数，避免用户误把凭据写进命令行后进入日志。
        self.exit(2, '通知参数无效；仅接受 --send 或 --help，禁止传递凭据或正文。\n')


def _main():
    parser = _SafeParser(description='固定模板的 Server酱通知测试；默认不联网。')
    parser.add_argument('--send', action='store_true', help='显式启用发送；仅供 Founder 合并后手动测试。')
    args = parser.parse_args()
    try:
        event = build_event('NOTIFIER_TEST', str(uuid4()),
                            'https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/16', 'founder')
        if not args.send:
            print(json.dumps({'Ok': True, 'Code': 'DRY_RUN', 'Data': event['Data']}, ensure_ascii=False))
            return 0
        result = NotificationSender().send(event['Data'], enabled=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['Ok'] else 1
    except Exception:
        # CLI 最外层也不打印可能包含凭据的 traceback。
        print('{"Ok": false, "Code": "E_INTERNAL", "Message": "通知测试失败，详细信息已抑制。", "Data": null}')
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
