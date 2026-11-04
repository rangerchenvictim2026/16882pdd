"""单个浏览器接入的本机退避记录，不含登录资料，也不自动重试。"""
import json
import math
import time

from .providers import HumanActionRequired


class AccessCooldown:
    def __init__(self, path, seconds=300, clock=None):
        self.path, self.seconds, self.clock = path, seconds, clock or time.time
        self.until = 0
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            value = saved.get('next_attempt_at', 0) if isinstance(saved, dict) else 0
            if isinstance(value, (int, float)) and math.isfinite(value):
                self.until = min(value, self.clock() + 3600)
        except (OSError, ValueError, TypeError):
            pass

    @property
    def remaining(self):
        return max(0, math.ceil(self.until - self.clock()))

    def ensure(self):
        if self.remaining:
            raise HumanActionRequired(f'此浏览器上次提示系统繁忙或访问限制，已停止请求；约{self.remaining}秒后才允许再次验证，程序不会自动重试')

    def block(self):
        self.until = self.clock() + self.seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'next_attempt_at': self.until, 'reason': '系统繁忙或访问限制'}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.path)
