import re
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from app.core.exceptions import CustomException, ErrorCode
from app.core.schema import CamelBaseModel

_EMAIL_RE = re.compile(r"^[\w\.\-]+@[\w\.\-]+\.\w+$")
_LETTER_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")


def _check_email(email: str) -> None:
    if not (1 <= len(email) <= 255) or not _EMAIL_RE.match(email):
        raise CustomException(ErrorCode.INVALID_EMAIL_FORMAT)


def _check_password(password: str) -> None:
    if not (8 <= len(password) <= 64):
        raise CustomException(ErrorCode.INVALID_PASSWORD_FORMAT)
    if not _LETTER_RE.search(password) or not _DIGIT_RE.search(password):
        raise CustomException(ErrorCode.INVALID_PASSWORD_FORMAT)


def _check_name(name: str) -> None:
    if not (1 <= len(name.strip()) <= 100):
        raise CustomException(ErrorCode.INVALID_NAME)


class UserCreateRequest(CamelBaseModel):
    email: str
    name: str
    password: str
    language: Literal["ko", "en"] = "ko"

    @model_validator(mode="after")
    def _validate(self) -> "UserCreateRequest":
        _check_email(self.email)
        _check_password(self.password)
        _check_name(self.name)
        return self


class UserUpdateRequest(CamelBaseModel):
    name: str | None = None
    password: str | None = None
    current_password: str | None = None  # password 를 바꿀 때만 필요 — 검증은 service(해시 비교)
    language: Literal["ko", "en"] | None = None

    @model_validator(mode="after")
    def _validate(self) -> "UserUpdateRequest":
        if self.password is not None:
            _check_password(self.password)
        if self.name is not None:
            _check_name(self.name)
        return self


class UserResponse(CamelBaseModel):
    id: UUID
    email: str
    name: str
    language: str
