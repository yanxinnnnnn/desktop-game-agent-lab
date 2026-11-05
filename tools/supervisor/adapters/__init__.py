"""离线适配器；不启动客户端，不认证真实来源，不提供通知入口。"""

from .dsh import CodeOrigin, DshOfflineAdapter, FixtureCodeEvidence, FixtureRunnerResult, OfflineCapture, TaskContext

__all__ = ['CodeOrigin', 'DshOfflineAdapter', 'FixtureCodeEvidence', 'FixtureRunnerResult', 'OfflineCapture', 'TaskContext']
