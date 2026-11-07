from enum import Enum


class ErrorCode(Enum):
    """비즈니스 에러 코드 (접두사로 카테고리 분류)"""

    # 공통 (CM)
    SUCCESS = (200, "CM000", "성공")
    BAD_REQUEST = (400, "CM001", "잘못된 요청입니다.")
    UNAUTHORIZED = (401, "CM002", "인증이 필요합니다.")
    FORBIDDEN = (403, "CM003", "권한이 없습니다.")
    NOT_FOUND = (400, "CM004", "데이터를 찾을 수 없습니다.")
    SERVICE_UNAVAILABLE = (503, "CM005", "서비스를 사용할 수 없습니다.")
    TOO_MANY_REQUESTS = (429, "CM006", "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    INTERNAL_ERROR = (500, "CM999", "서버 오류가 발생했습니다.")

    # 인증 (AU)
    INVALID_TOKEN = (401, "AU001", "유효하지 않은 토큰입니다.")
    EXPIRED_TOKEN = (401, "AU002", "만료된 토큰입니다.")
    INVALID_PASSWORD = (400, "AU003", "비밀번호가 일치하지 않습니다.")
    LOGIN_FAILED = (401, "AU004", "아이디 또는 비밀번호가 올바르지 않습니다.")
    INVALID_REFRESH_TOKEN = (401, "AU005", "유효하지 않은 리프레시 토큰입니다.")

    # 사용자 (US)
    USER_DUPLICATE_LOGIN_ID = (400, "US001", "이미 존재하는 아이디입니다.")
    USER_DUPLICATE_EMAIL = (400, "US002", "이미 사용 중인 이메일입니다.")
    INVALID_EMAIL_FORMAT = (400, "US003", "올바른 이메일 형식이 아닙니다.")
    INVALID_PASSWORD_FORMAT = (400, "US004", "비밀번호는 8~64자, 영문과 숫자를 포함해야 합니다.")
    INVALID_NAME = (400, "US005", "이름은 1~100자여야 합니다.")
    DEMO_READ_ONLY = (403, "US006", "체험 계정에서는 할 수 없는 작업입니다.")

    # 가계부 그룹 (HH)
    HOUSEHOLD_NOT_MEMBER = (403, "HH001", "해당 가계부의 멤버가 아닙니다.")
    HOUSEHOLD_NOT_FOUND = (400, "HH002", "가계부를 찾을 수 없습니다.")
    HOUSEHOLD_NOT_OWNER = (403, "HH003", "가계부 소유자만 가능한 작업입니다.")
    HOUSEHOLD_MEMBER_ALREADY = (400, "HH004", "이미 가계부 멤버입니다.")
    HOUSEHOLD_MEMBER_NOT_FOUND = (400, "HH005", "해당 멤버를 찾을 수 없습니다.")
    HOUSEHOLD_OWNER_CANNOT_LEAVE = (400, "HH006", "소유자는 가계부에서 나갈 수 없습니다.")

    # 포트폴리오 (PT)
    STOCK_LOOKUP_FAILED = (404, "PT001", "종목 조회에 실패했습니다.")
    PORTFOLIO_HAS_HOLDINGS = (400, "PT002", "보유 중인 종목은 삭제할 수 없습니다. 먼저 전량 매도하세요.")

    # 통장 (AC)
    ACCOUNT_HAS_DEPENDENTS = (400, "AC001", "보유 종목이 있는 통장은 삭제할 수 없습니다. 먼저 전량 매도하세요.")
    ACCOUNT_TYPE_LOCKED = (400, "AC002", "거래나 종목이 있는 통장은 투자·자산·일반 사이로 종류를 바꿀 수 없습니다.")

    # 카테고리 (CT)
    CATEGORY_IN_USE = (400, "CT001", "거래가 있는 카테고리는 수입/지출 구분을 바꿀 수 없습니다.")

    # 멱등성 (ID)
    IDEMPOTENCY_KEY_CONFLICT = (422, "ID001", "다른 요청에 사용된 키입니다.")
    IDEMPOTENCY_BODY_MISMATCH = (422, "ID002", "같은 키로 다른 요청 본문이 왔습니다.")
    IDEMPOTENCY_IN_PROGRESS = (409, "ID003", "동일 요청 처리 중입니다. 잠시 후 재시도해주세요.")

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
