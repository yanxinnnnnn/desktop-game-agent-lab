"""Server酱 Turbo 通知发送器：默认关闭，凭据只从环境读取，诊断不包含服务端原文。"""

import json
import os
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .events import Result, _description, _result, _validate_event

_MAX_RESPONSE_BYTES = 8192
_MAX_SENDS = 5
_QUOTA_COOLDOWN_SECONDS = 300
_TIMEOUT_SECONDS = 10


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # SendKey 在路径中；任何 3xx 都不跟随，即使目标貌似同源。
        return None


def _deliver(sendkey, event):
    payload = urlencode({'title': event['title'], 'desp': _description(event)}).encode('utf-8')
    request = Request('https://sctapi.ftqq.com/' + sendkey + '.send', data=payload,
                      headers={'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'},
                      method='POST')
    # 使用 stdlib 默认 HTTPS 证书校验与继承的代理；不记录 URL/请求/异常字符串。
    with build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT_SECONDS) as response:
        if not 200 <= response.status < 300:
            return _result('E_HTTP', '通知服务 HTTP 请求失败。')
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return _result('E_RESPONSE', '通知服务响应过大。')
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get('code')) is not int:
            return _result('E_RESPONSE', '通知服务响应格式无效。')
        if data['code'] != 0:
            return _result('E_PROVIDER', '通知服务拒绝了请求。')
    return _result(data={'provider_accepted': True, 'wechat_receipt_confirmed': False})


class NotificationSender:
    """同一实例内存中的去重/冷却/预算；没有后台线程、持久化或自动重试。"""

    def __init__(self):
        self._submitted_ids = set()
        self._last_quota_alert = None
        self._provider_cooldown_until = None
        self._send_count = 0
        self._lock = threading.Lock()

    def send(self, event: dict, *, enabled: bool = False) -> Result:
        """enabled=True 才可能读取 SERVERCHAN_SENDKEY；最多一次 HTTP 尝试。"""
        if enabled is not True:
            return _result('E_DISABLED', '通知未显式启用。')
        checked = _validate_event(event)
        if not checked['Ok']:
            return checked
        # 使用重建副本，避免调用方在校验后替换成未审核文本。
        safe_event = checked['Data']
        sendkey = os.environ.get('SERVERCHAN_SENDKEY')
        if not sendkey:
            return _result('E_CREDENTIAL_MISSING', '未配置通知凭据。')
        if not re.fullmatch(r'SCT[A-Za-z0-9]{5,200}', sendkey):
            return _result('E_CREDENTIAL_INVALID', '通知凭据格式无效。')
        with self._lock:
            now = time.monotonic()
            if safe_event['event_id'] in self._submitted_ids:
                return _result('E_DUPLICATE_EVENT', '本发送器实例已提交该事件；不重复发送。')
            if self._provider_cooldown_until is not None and now < self._provider_cooldown_until:
                return _result('E_COOLDOWN', '通知服务限流冷却中。')
            if (safe_event['event_type'] == 'BLOCKED_QUOTA' and self._last_quota_alert is not None and
                    now - self._last_quota_alert < _QUOTA_COOLDOWN_SECONDS):
                return _result('E_COOLDOWN', '重复额度告警冷却中。')
            if self._send_count >= _MAX_SENDS:
                return _result('E_SEND_LIMIT', '已达到本发送器实例发送尝试上限。')
            # 失败/超时也占用 event_id 与预算，防止盲目重发可能已送达的通知。
            self._submitted_ids.add(safe_event['event_id'])
            self._send_count += 1
            if safe_event['event_type'] == 'BLOCKED_QUOTA':
                self._last_quota_alert = now
        try:
            return _deliver(sendkey, safe_event)
        except HTTPError as error:
            if error.code == 429:
                with self._lock:
                    self._provider_cooldown_until = time.monotonic() + _QUOTA_COOLDOWN_SECONDS
                return _result('E_RATE_LIMIT', '通知服务限流；请等待并人工检查配额。')
            return _result('E_HTTP', '通知服务 HTTP 请求失败；未重试。')
        except (TimeoutError, socket.timeout):
            return _result('E_TIMEOUT', '通知请求超时；未重试，送达状态未知。')
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                return _result('E_TIMEOUT', '通知请求超时；未重试，送达状态未知。')
            return _result('E_TRANSPORT', '通知网络连接失败；未重试。')
        except (ValueError, UnicodeError):
            return _result('E_RESPONSE', '通知服务响应无法解析。')
        except Exception:
            # 不转发异常文本，避免第三方或 urllib 将带密钥 URL 带入诊断。
            return _result('E_INTERNAL', '通知请求失败；详细信息已抑制，未重试。')
