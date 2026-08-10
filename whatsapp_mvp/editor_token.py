# WhatsApp MVP - Preview editor link tokens
#
# 预览编辑器（Phase 2）的私有链接鉴权：一个 job 的编辑器 URL 里带一个签名
# token，证明"这条链接确实是本服务发给这个 job 的合法用户的"，不需要账号
# 系统。跟 whatsapp_client.py 的 webhook 签名同一套 HMAC 思路，但目的不同——
# 那边校验"这条请求真的来自 Meta"，这边校验"这条 URL 真的是我们签发的"。
#
# 刻意 fail CLOSED：密钥没配置时一律拒绝签发/校验，绝不退化成"不校验直接
# 放行"。这个仓库已经在 whatsapp webhook 签名上吃过一次 fail-open 的亏
# （signature header 缺失时静默跳过校验）——editor_token 不重蹈覆辙。

from __future__ import annotations

import hashlib
import hmac
import time

from .config import get_config


class EditorTokenError(RuntimeError):
    """密钥未配置（部署错误）或 token 校验失败（缺失/格式错误/过期/签名不对）
    时抛出。调用方（webhook.py 的编辑器路由）统一按 403 处理——不区分"密钥
    没配"和"token 不对"两种原因回给客户端，避免向未认证的请求方泄露服务端
    配置状态。"""


def _secret() -> str:
    config = get_config()
    secret = config.editor_token_secret or config.whatsapp_app_secret
    if not secret:
        raise EditorTokenError(
            "EDITOR_TOKEN_SECRET 和 WHATSAPP_APP_SECRET 都未配置——"
            "预览编辑器的链接鉴权没有密钥可用，拒绝签发/校验任何 token"
            "（fail closed，不会退化成不校验）。"
        )
    return secret


def _sign(job_id: str, exp: int) -> str:
    secret = _secret()
    payload = f"{job_id}:{exp}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def make_token(job_id: str, ttl_s: int = 7 * 86400) -> str:
    """签发一个 `{exp}.{sig}` 形式的 token，`ttl_s` 秒后过期（默认 7 天——
    用户在 WhatsApp 里点开预览链接可能是几天后的事，不宜设太短）。

    完整 64 位十六进制摘要，不截断——截断不省下什么，只白白削弱抗碰撞性。
    """
    exp = int(time.time()) + ttl_s
    return f"{exp}.{_sign(job_id, exp)}"


def verify_token(job_id: str, token: str) -> bool:
    """校验 token 是否对得上这个 job_id、且未过期。用
    `hmac.compare_digest` 防时序攻击，不用 `==`。

    格式错误、过期、签名不匹配，一律返回 False——调用方按 403 处理，不需要
    区分具体原因（同一份 EditorTokenError 语义，见模块顶部说明）。
    """
    if not token or "." not in token:
        return False
    exp_str, _, sig = token.partition(".")
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    try:
        expected = _sign(job_id, exp)
    except EditorTokenError:
        return False
    return hmac.compare_digest(expected, sig)
