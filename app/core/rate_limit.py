"""간단한 요청 횟수 제한 — 프로세스 메모리 슬라이딩 윈도우.

운영은 uvicorn 프로세스 1개(entrypoint.sh, --workers 없음)라 메모리로 충분하다. 인스턴스를 늘리면
인스턴스 수만큼 느슨해진다. 엔드포인트 1~2개 제한에 새 의존성(slowapi 등)을 들이지 않으려고 둔다.
"""
import time
from collections import deque
from collections.abc import Callable

from app.core.exceptions import CustomException, ErrorCode


class SlidingWindowLimiter:
    """키별로 최근 window_sec 초 안의 요청이 limit 개면 그다음 요청을 429 로 막는다."""

    def __init__(
        self, limit: int, window_sec: float, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_sec
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = clock()

    def hit(self, key: str) -> None:
        now = self._clock()
        self._sweep(now)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            raise CustomException(ErrorCode.TOO_MANY_REQUESTS)
        hits.append(now)

    def reset(self) -> None:
        self._hits.clear()

    def _sweep(self, now: float) -> None:
        """창이 한 번 지날 때마다 마지막 요청이 창 밖인 키를 지운다 — 키가 끝없이 쌓이지 않게."""
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        stale = [k for k, q in self._hits.items() if not q or now - q[-1] >= self._window]
        for k in stale:
            del self._hits[k]


# 이메일 검색(멤버 초대) — 초대 화면에서 몇 번 고쳐 치는 정도면 충분, 대량 조회는 막는다
SEARCH_LIMIT_PER_MINUTE = 10
_search_limiter = SlidingWindowLimiter(SEARCH_LIMIT_PER_MINUTE, 60)


def get_search_limiter() -> SlidingWindowLimiter:
    return _search_limiter


# 비번 변경의 현재 비번 확인 — access 토큰을 훔친 사람이 현재 비번을 무제한 추측하지 못하게(bcrypt 부하도)
PASSWORD_ATTEMPTS_PER_10_MIN = 5
_password_limiter = SlidingWindowLimiter(PASSWORD_ATTEMPTS_PER_10_MIN, 600)


def get_password_limiter() -> SlidingWindowLimiter:
    return _password_limiter
