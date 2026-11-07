"""데모 계정 판정 — 자격증명이 공개된 체험 계정이라 남들이 못 쓰게 만드는 동작은 막는다."""
from app.core.config import settings
from app.domain.user.model import User


def is_demo_user(user: User) -> bool:
    # 데모를 안 띄우는 배포에선 같은 이메일로 가입한 실제 사용자를 데모로 보지 않는다
    if not settings.DEMO_SEED_ENABLED:
        return False
    # 시드는 설정값을 그대로 저장하므로 양쪽 다 정규화해서 비교
    return user.email.strip().lower() == settings.DEMO_EMAIL.strip().lower()
