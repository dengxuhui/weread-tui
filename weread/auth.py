"""
auth.py — 登录与 Cookie 管理

Cookie 仅通过 keyring 写入系统钥匙串（macOS Keychain），绝不明文写文件。

登录流程（基于逆向 JS bundle 确认的真实端点）：
    1. GET /api/auth/getLoginUid     → 获取本次登录的 uid
    2. 生成 QR 内容：https://weread.qq.com/web/confirm?uid=UID
    3. 终端渲染 ASCII 二维码，提示用户用微信读书 App 扫码
    4. 长轮询 GET /api/auth/getLoginInfo?uid=UID（服务端持连最多 60 秒）
       - 若返回 logicCode=="NEED_OTP"，提示输入短信验证码后带 otp 参数重新轮询
       - 若返回 logicCode=="EXPIRED"，二维码失效，抛 LoginError
       - 若 succeed==True，返回 accessToken / webLoginVid
    5. 将 webLoginVid → wr_vid、accessToken → wr_skey 写入 keyring

注意：
    - 轮询单次超时约 62 秒（服务端 60s + 2s 缓冲），最多重试 2 轮（~120s）
    - 若 3 轮内未完成扫码则抛 LoginTimeoutError
"""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from typing import Any

import httpx
import keyring
import keyring.errors
import qrcode

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

KEYRING_SERVICE = "weread-tui"
_VID_KEY = "wr_vid"
_SKEY_KEY = "wr_skey"

BASE_URL = "https://weread.qq.com"

_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weread.qq.com/",
}

# getLoginInfo 服务端最长持连 60 秒，客户端多留 5 秒缓冲
_LONG_POLL_TIMEOUT: float = 65.0
# 最多扫码等待轮次（每轮约 60 秒）
POLL_MAX_ROUNDS: int = 2
# 短轮询间隔（仅测试覆盖用，正常流程无需 sleep）
POLL_INTERVAL: float = 0.0


# --------------------------------------------------------------------------- #
# 自定义异常
# --------------------------------------------------------------------------- #

class LoginTimeoutError(Exception):
    """扫码等待超时，超过最大轮询轮数仍未完成。"""


class LoginError(Exception):
    """登录过程中发生不可恢复的错误（二维码失效、接口异常等）。"""


# --------------------------------------------------------------------------- #
# Keychain 存取（macOS 用 security CLI；其他平台用 keyring）
# --------------------------------------------------------------------------- #

def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _mac_set(account: str, value: str) -> None:
    """通过 macOS security CLI 写入 Keychain（-U 表示已存在则更新）。"""
    subprocess.run(
        [
            "security", "add-generic-password",
            "-s", KEYRING_SERVICE,
            "-a", account,
            "-w", value,
            "-U",
        ],
        check=True,
        capture_output=True,
    )


def _mac_get(account: str) -> str | None:
    """通过 macOS security CLI 读取 Keychain，条目不存在时返回 None。"""
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-s", KEYRING_SERVICE,
            "-a", account,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


def _mac_delete(account: str) -> None:
    """通过 macOS security CLI 删除 Keychain 条目，不存在时静默忽略。"""
    subprocess.run(
        [
            "security", "delete-generic-password",
            "-s", KEYRING_SERVICE,
            "-a", account,
        ],
        capture_output=True,
    )


def load_cookie() -> tuple[str, str] | None:
    """
    从系统钥匙串读取 (wr_vid, wr_skey)。
    任一字段缺失时返回 None。
    """
    if _is_macos():
        vid = _mac_get(_VID_KEY)
        skey = _mac_get(_SKEY_KEY)
    else:
        vid = keyring.get_password(KEYRING_SERVICE, _VID_KEY)
        skey = keyring.get_password(KEYRING_SERVICE, _SKEY_KEY)
    if vid and skey:
        return vid, skey
    return None


def save_cookie(vid: str, skey: str) -> None:
    """将 wr_vid 和 wr_skey 写入系统钥匙串。"""
    if _is_macos():
        _mac_set(_VID_KEY, vid)
        _mac_set(_SKEY_KEY, skey)
    else:
        keyring.set_password(KEYRING_SERVICE, _VID_KEY, vid)
        keyring.set_password(KEYRING_SERVICE, _SKEY_KEY, skey)


def clear_cookie() -> None:
    """从系统钥匙串删除 Cookie，条目不存在时静默忽略。"""
    if _is_macos():
        _mac_delete(_VID_KEY)
        _mac_delete(_SKEY_KEY)
    else:
        for key in (_VID_KEY, _SKEY_KEY):
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass


# --------------------------------------------------------------------------- #
# Cookie 过期检测
# --------------------------------------------------------------------------- #

def is_cookie_expired(response_body: dict[str, Any]) -> bool:
    """
    检测 API 响应是否表示 Cookie 已过期。
    条件：响应 JSON 中 errCode == -2012。
    """
    return response_body.get("errCode") == -2012


# --------------------------------------------------------------------------- #
# 登录辅助函数（独立提取，便于单元测试 mock）
# --------------------------------------------------------------------------- #

def _fetch_login_uid(client: httpx.Client) -> str:
    """
    调用 GET /api/auth/getLoginUid，返回本次登录会话的 uid 字符串。
    uid 将作为 QR 内容的一部分，并用于后续轮询。
    """
    resp = client.get(f"{BASE_URL}/api/auth/getLoginUid")
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()

    uid: str | None = data.get("uid")
    if not uid:
        raise LoginError(f"无法获取登录 uid，接口返回：{data}")

    return uid


def _render_qr(url: str) -> None:
    """在终端渲染 ASCII 二维码（qrcode 库）。"""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True, out=sys.stdout)


def _poll_login_info(
    client: httpx.Client,
    uid: str,
    otp: str = "",
    *,
    long_poll_timeout: float = _LONG_POLL_TIMEOUT,
) -> dict[str, Any]:
    """
    向 GET /api/auth/getLoginInfo?uid=UID[&otp=OTP] 发送一次长轮询请求。
    服务端最多持连 60 秒后返回。

    返回值为原始 JSON dict，调用方负责解读 succeed / logicCode 字段。
    网络超时时抛 httpx.ReadTimeout；HTTP 错误时抛 httpx.HTTPStatusError。
    """
    params: dict[str, str] = {"uid": uid}
    if otp:
        params["otp"] = otp

    resp = client.get(
        f"{BASE_URL}/api/auth/getLoginInfo",
        params=params,
        timeout=long_poll_timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_cookies(response_or_data: httpx.Response | dict[str, Any]) -> tuple[str, str]:
    """
    从 httpx.Response 或已解析的 JSON dict 中提取 wr_vid 和 wr_skey。

    优先级：
      1. Set-Cookie header（仅 httpx.Response 有效）
      2. JSON body 字段 webLoginVid / accessToken（新端点返回格式）
      3. JSON body 字段 cookies.wr_vid / cookies.wr_skey（旧格式兼容）
    """
    vid: str | None = None
    skey: str | None = None

    # --- 1) Set-Cookie header ---
    if isinstance(response_or_data, httpx.Response):
        vid = response_or_data.cookies.get("wr_vid")
        skey = response_or_data.cookies.get("wr_skey")
        body: dict[str, Any] = {}
        try:
            body = response_or_data.json()
        except Exception:
            pass
    else:
        body = response_or_data

    # --- 2) webLoginVid / accessToken（getLoginInfo 成功响应）---
    # webLoginVid 在 API 响应中可能是整数，统一转为字符串
    if not vid:
        raw = body.get("webLoginVid")
        if raw is not None:
            vid = str(raw)
    if not skey:
        raw = body.get("accessToken")
        if raw is not None:
            skey = str(raw)

    # --- 3) cookies.wr_vid / cookies.wr_skey（旧端点兼容）---
    if not (vid and skey):
        body_cookies: dict = body.get("cookies") or {}
        if not vid and body_cookies.get("wr_vid") is not None:
            vid = str(body_cookies["wr_vid"])
        if not skey and body_cookies.get("wr_skey") is not None:
            skey = str(body_cookies["wr_skey"])

    if not vid or not skey:
        raise LoginError(
            "无法从登录响应中提取 wr_vid / wr_skey，请确认 API 是否发生变更。"
        )

    return vid, skey


# --------------------------------------------------------------------------- #
# 公开登录入口
# --------------------------------------------------------------------------- #

def login(
    *,
    poll_max_rounds: int = POLL_MAX_ROUNDS,
    long_poll_timeout: float = _LONG_POLL_TIMEOUT,
) -> tuple[str, str]:
    """
    完整扫码登录流程，返回 (wr_vid, wr_skey) 并写入 keyring。

    poll_max_rounds / long_poll_timeout 参数仅用于测试覆盖，
    生产环境使用默认值。
    """
    print("✦ 欢迎使用 weread-tui")
    print("  请使用「微信读书 App」扫描以下二维码登录：\n")

    with httpx.Client(headers=_DEFAULT_HEADERS, timeout=10.0) as client:
        uid = _fetch_login_uid(client)

        qr_url = f"{BASE_URL}/web/confirm?uid={uid}"
        _render_qr(qr_url)
        print()

        otp = ""
        for round_num in range(poll_max_rounds):
            try:
                data = _poll_login_info(
                    client, uid, otp=otp, long_poll_timeout=long_poll_timeout
                )
            except httpx.ReadTimeout:
                # 服务端正常超时（60s 无人扫码），继续下一轮
                print(f"\r  等待扫码中...（第 {round_num + 1} 轮）", end="", flush=True)
                continue

            logic_code = data.get("logicCode", "")

            if data.get("succeed"):
                print()
                vid, skey = _extract_cookies(data)
                save_cookie(vid, skey)
                print("  ✓ 登录成功！\n")
                return vid, skey

            if logic_code == "NEED_OTP":
                print("\n  需要短信验证码验证。")
                otp = input("  请输入验证码：").strip()
                # 重置轮次计数，带 otp 再试一轮
                round_num = -1  # 下次循环变为 0
                continue

            if logic_code in ("EXPIRED", "GET_UID_FAIL"):
                raise LoginError("二维码已失效，请重新运行 weread login。")

            if logic_code == "LOGIN_TIMEOUT":
                # 本轮服务端超时，继续下一轮
                continue

        # 超出最大轮数
        print()
        raise LoginTimeoutError(
            f"扫码超时（等待 {poll_max_rounds} 轮仍未完成），请重新运行 weread login。"
        )
