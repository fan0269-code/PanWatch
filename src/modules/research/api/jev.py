"""个股 Jev 手动判断 API。认证由 bootstrap 统一挂载。"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.modules.research.jev_schemas import JudgmentRequest
from src.modules.research.jev_service import (
    JevDataError,
    JevStorageError,
    create_judgment,
    list_judgments,
)
from src.platform.ai.jev_client import JevClient, JevError
from src.platform.persistence.database import get_db
from src.platform.runtime.config import Settings

router = APIRouter()


@router.get("/status")
def status():
    settings = Settings()
    return {"configured": bool(settings.typesafe_api_key.get_secret_value().strip()), "model": settings.jev_model}


@router.post("/judgments")
async def judge(payload: JudgmentRequest, db: Session = Depends(get_db)):
    settings = Settings()
    secret = settings.typesafe_api_key.get_secret_value()
    if not secret.strip():
        raise HTTPException(503, "尚未配置 Jev，请在服务端设置 TYPESAFE_API_KEY 后重启。")
    client = JevClient(secret, model=settings.jev_model, timeout=settings.jev_timeout_seconds, proxy=settings.http_proxy)
    try:
        # 为前端180秒上限留余量；取消后不继续请求模型或写入迟到结果。
        return await asyncio.wait_for(create_judgment(payload, db, client), timeout=150)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Jev 判断超时，请稍后重试。") from None
    except JevDataError as exc:
        raise HTTPException(422, str(exc)) from None
    except JevError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except JevStorageError as exc:
        raise HTTPException(500, str(exc)) from None


@router.get("/judgments")
def history(
    symbol: str = Query(min_length=1, max_length=20),
    market: str = Query(default="CN", pattern="^(CN|HK|US)$"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return list_judgments(db, symbol.strip().upper(), market, limit)
