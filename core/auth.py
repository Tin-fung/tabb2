import time
import hmac
import hashlib
import logging

from fastapi import Request, HTTPException
from jose import jwt, JWTError

from core.config import ConfigManager, hash_password, verify_password_hash

logger = logging.getLogger("tabbit2openai")

TOKEN_EXPIRY = 86400  # 24 小时


def create_jwt(config: ConfigManager) -> str:
    """创建 JWT token（使用标准 jose 库）"""
    secret = config.get("admin", "jwt_secret")
    payload = {
        "role": "admin",
        "exp": int(time.time()) + TOKEN_EXPIRY,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_jwt(token: str, config: ConfigManager) -> dict:
    """验证 JWT token（使用标准 jose 库）"""
    try:
        secret = config.get("admin", "jwt_secret")
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        return payload
    except JWTError as e:
        logger.warning("JWT verification failed: %s", e)
        raise HTTPException(status_code=401, detail=str(e))


def verify_password(password: str, config: ConfigManager) -> bool:
    """验证密码，支持 bcrypt 新格式和 SHA-256 旧格式（迁移兼容）"""
    stored_hash = config.get("admin", "password_hash")
    salt = config.get("admin", "salt")

    if not stored_hash:
        return False

    # 新格式：bcrypt 哈希（salt 为空）
    if not salt:
        return verify_password_hash(password, stored_hash)

    # 旧格式：SHA-256 哈希（salt 非空）- 向后兼容
    computed = hashlib.sha256((password + salt).encode()).hexdigest()
    return hmac.compare_digest(computed, stored_hash)


def require_admin(config: ConfigManager):
    """返回一个 FastAPI 依赖函数"""

    async def dependency(request: Request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing token")
        token = auth[7:]
        return verify_jwt(token, config)

    return dependency
