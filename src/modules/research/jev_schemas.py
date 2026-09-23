"""Jev 手动判断的 HTTP 输入。"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class JudgmentRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9][A-Z0-9.\-^=]*$")
    market: Literal["CN", "HK", "US"] = "CN"
    horizon: Literal[1, 3, 5] = 1
    flat_threshold_pct: float = Field(default=1.0, ge=0.1, le=10, allow_inf_nan=False)
    analysis_id: int | None = Field(default=None, gt=0)

    @field_validator("symbol", "market", mode="before")
    @classmethod
    def normalize_identifier(cls, value):
        return value.strip().upper() if isinstance(value, str) else value
