"""tests/test_auth.py — auth.py 单元测试

真实网络调用与系统 keyring / security CLI 均通过 monkeypatch / mock 隔离，
不依赖微信读书账号即可运行。
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

import weread.auth as auth_mod
from weread.auth import (
    LoginError,
    LoginTimeoutError,
    _extract_cookies,
    _fetch_login_uid,
    _poll_login_info,
    _render_qr,
    clear_cookie,
    is_cookie_expired,
    load_cookie,
    save_cookie,
)


# --------------------------------------------------------------------------- #
# 工具：构造假的 httpx.Response
# --------------------------------------------------------------------------- #

def _make_response(
    json_body: dict[str, Any] | None = None,
    cookies: dict[str, str] | None = None,
    status_code: int = 200,
) -> httpx.Response:
    """
    构造一个绑定了 dummy request 的 httpx.Response，使 raise_for_status()
    和 .cookies 均可正常使用。Cookies 通过 Set-Cookie header 传入。
    """
    dummy_request = httpx.Request("GET", "https://weread.qq.com/")

    headers: list[tuple[str, str]] = [("content-type", "application/json")]
    if cookies:
        for k, v in cookies.items():
            headers.append(("set-cookie", f"{k}={v}"))

    return httpx.Response(
        status_code=status_code,
        json=json_body or {},
        headers=headers,
        request=dummy_request,
    )


# --------------------------------------------------------------------------- #
# Keyring 存取（非 macOS 路径）
# --------------------------------------------------------------------------- #

class TestKeyring:
    """测试 keyring 路径（强制 _is_macos=False）。"""

    def test_load_cookie_returns_none_when_empty(self):
        with (
            patch("weread.auth._is_macos", return_value=False),
            patch.object(auth_mod.keyring, "get_password", return_value=None),
        ):
            assert load_cookie() is None

    def test_load_cookie_returns_none_when_only_vid(self):
        def _get(service, key):
            return "vid123" if key == "wr_vid" else None

        with (
            patch("weread.auth._is_macos", return_value=False),
            patch.object(auth_mod.keyring, "get_password", side_effect=_get),
        ):
            assert load_cookie() is None

    def test_load_cookie_success(self):
        def _get(service, key):
            return {"wr_vid": "vid123", "wr_skey": "skey456"}.get(key)

        with (
            patch("weread.auth._is_macos", return_value=False),
            patch.object(auth_mod.keyring, "get_password", side_effect=_get),
        ):
            assert load_cookie() == ("vid123", "skey456")

    def test_save_cookie_calls_set_password(self):
        with (
            patch("weread.auth._is_macos", return_value=False),
            patch.object(auth_mod.keyring, "set_password") as mock_set,
        ):
            save_cookie("vid123", "skey456")
            assert mock_set.call_count == 2
            mock_set.assert_any_call(auth_mod.KEYRING_SERVICE, "wr_vid", "vid123")
            mock_set.assert_any_call(auth_mod.KEYRING_SERVICE, "wr_skey", "skey456")

    def test_clear_cookie_deletes_both_keys(self):
        with (
            patch("weread.auth._is_macos", return_value=False),
            patch.object(auth_mod.keyring, "delete_password") as mock_del,
        ):
            clear_cookie()
            assert mock_del.call_count == 2

    def test_clear_cookie_ignores_missing_entry(self):
        """PasswordDeleteError 应被静默吞掉，不向上抛出。"""
        import keyring.errors

        with (
            patch("weread.auth._is_macos", return_value=False),
            patch.object(
                auth_mod.keyring,
                "delete_password",
                side_effect=keyring.errors.PasswordDeleteError("not found"),
            ),
        ):
            clear_cookie()  # 不抛异常即通过


# --------------------------------------------------------------------------- #
# macOS security CLI 路径
# --------------------------------------------------------------------------- #

class TestMacOSKeychain:
    """测试 macOS security CLI 路径（强制 _is_macos=True）。"""

    def _make_proc(self, returncode: int = 0, stdout: str = "") -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_save_cookie_calls_security_add(self):
        with (
            patch("weread.auth._is_macos", return_value=True),
            patch("weread.auth.subprocess.run") as mock_run,
        ):
            mock_run.return_value = self._make_proc()
            save_cookie("vid123", "skey456")

        assert mock_run.call_count == 2
        # 两次调用均使用 add-generic-password -U
        for c in mock_run.call_args_list:
            args = c[0][0]
            assert "add-generic-password" in args
            assert "-U" in args
        # 第一次存 wr_vid，第二次存 wr_skey
        args0 = mock_run.call_args_list[0][0][0]
        assert "wr_vid" in args0 and "vid123" in args0
        args1 = mock_run.call_args_list[1][0][0]
        assert "wr_skey" in args1 and "skey456" in args1

    def test_load_cookie_returns_values(self):
        def _run(args, **kwargs):
            if "wr_vid" in args:
                return self._make_proc(stdout="vid123\n")
            return self._make_proc(stdout="skey456\n")

        with (
            patch("weread.auth._is_macos", return_value=True),
            patch("weread.auth.subprocess.run", side_effect=_run),
        ):
            result = load_cookie()

        assert result == ("vid123", "skey456")

    def test_load_cookie_returns_none_when_missing(self):
        with (
            patch("weread.auth._is_macos", return_value=True),
            patch("weread.auth.subprocess.run", return_value=self._make_proc(returncode=44)),
        ):
            assert load_cookie() is None

    def test_clear_cookie_calls_security_delete(self):
        with (
            patch("weread.auth._is_macos", return_value=True),
            patch("weread.auth.subprocess.run") as mock_run,
        ):
            mock_run.return_value = self._make_proc()
            clear_cookie()

        assert mock_run.call_count == 2
        for c in mock_run.call_args_list:
            assert "delete-generic-password" in c[0][0]


# --------------------------------------------------------------------------- #
# Cookie 过期检测
# --------------------------------------------------------------------------- #

class TestIsCookieExpired:
    @pytest.mark.parametrize(
        "body, expected",
        [
            ({"errCode": -2012}, True),
            ({"errCode": 0}, False),
            ({"errCode": -2012, "extra": "ignored"}, True),
            ({}, False),
            ({"data": "ok"}, False),
        ],
    )
    def test_various_bodies(self, body, expected):
        assert is_cookie_expired(body) is expected


# --------------------------------------------------------------------------- #
# _fetch_login_uid
# --------------------------------------------------------------------------- #

class TestFetchLoginUid:
    def test_returns_uid_on_success(self):
        resp = _make_response({"uid": "abc-123-def"})
        client = MagicMock()
        client.get.return_value = resp

        uid = _fetch_login_uid(client)
        assert uid == "abc-123-def"
        client.get.assert_called_once_with(
            "https://weread.qq.com/api/auth/getLoginUid"
        )

    def test_raises_login_error_when_uid_missing(self):
        resp = _make_response({"status": "ok"})  # 无 uid 字段
        client = MagicMock()
        client.get.return_value = resp

        with pytest.raises(LoginError, match="uid"):
            _fetch_login_uid(client)

    def test_raises_on_http_error(self):
        client = MagicMock()
        client.get.return_value = _make_response(status_code=500)

        with pytest.raises(httpx.HTTPStatusError):
            _fetch_login_uid(client)


# --------------------------------------------------------------------------- #
# _poll_login_info
# --------------------------------------------------------------------------- #

class TestPollLoginInfo:
    def test_returns_data_on_succeed(self):
        resp = _make_response({"succeed": True, "webLoginVid": "v", "accessToken": "s"})
        client = MagicMock()
        client.get.return_value = resp

        data = _poll_login_info(client, "uid-123")
        assert data["succeed"] is True
        assert data["webLoginVid"] == "v"
        assert data["accessToken"] == "s"
        client.get.assert_called_once_with(
            "https://weread.qq.com/api/auth/getLoginInfo",
            params={"uid": "uid-123"},
            timeout=auth_mod._LONG_POLL_TIMEOUT,
        )

    def test_passes_otp_when_provided(self):
        resp = _make_response({"succeed": True, "webLoginVid": "v", "accessToken": "s"})
        client = MagicMock()
        client.get.return_value = resp

        _poll_login_info(client, "uid-123", otp="8888")
        client.get.assert_called_once_with(
            "https://weread.qq.com/api/auth/getLoginInfo",
            params={"uid": "uid-123", "otp": "8888"},
            timeout=auth_mod._LONG_POLL_TIMEOUT,
        )

    def test_otp_empty_string_not_passed(self):
        """otp 为空串时不应出现在 params 中。"""
        resp = _make_response({"succeed": False, "logicCode": "LOGIN_TIMEOUT"})
        client = MagicMock()
        client.get.return_value = resp

        _poll_login_info(client, "uid-xyz", otp="")
        call_kwargs = client.get.call_args
        assert "otp" not in call_kwargs.kwargs["params"]

    def test_returns_need_otp_logic_code(self):
        resp = _make_response({"succeed": False, "logicCode": "NEED_OTP"})
        client = MagicMock()
        client.get.return_value = resp

        data = _poll_login_info(client, "uid-123")
        assert data["logicCode"] == "NEED_OTP"

    def test_returns_login_timeout_logic_code(self):
        resp = _make_response({"succeed": False, "logicCode": "LOGIN_TIMEOUT"})
        client = MagicMock()
        client.get.return_value = resp

        data = _poll_login_info(client, "uid-123")
        assert data["logicCode"] == "LOGIN_TIMEOUT"

    def test_raises_read_timeout_on_network_timeout(self):
        """服务端 60s 超时 → httpx.ReadTimeout 向上透传，由 login() 处理。"""
        client = MagicMock()
        client.get.side_effect = httpx.ReadTimeout("timed out", request=None)

        with pytest.raises(httpx.ReadTimeout):
            _poll_login_info(client, "uid-123")

    def test_raises_on_http_error(self):
        client = MagicMock()
        client.get.return_value = _make_response(status_code=500)

        with pytest.raises(httpx.HTTPStatusError):
            _poll_login_info(client, "uid-123")


# --------------------------------------------------------------------------- #
# _extract_cookies
# --------------------------------------------------------------------------- #

class TestExtractCookies:
    def test_extracts_webloginvid_and_accesstoken_from_dict(self):
        """新格式：直接从 dict 提取 webLoginVid / accessToken。"""
        data = {"succeed": True, "webLoginVid": "vid99", "accessToken": "sk99"}
        assert _extract_cookies(data) == ("vid99", "sk99")

    def test_extracts_integer_webloginvid_as_string(self):
        """webLoginVid 为整数时应转换为字符串（API 实际返回数字类型）。"""
        data = {"succeed": True, "webLoginVid": 123456789, "accessToken": "sk99"}
        vid, skey = _extract_cookies(data)
        assert vid == "123456789"
        assert isinstance(vid, str)

    def test_extracts_from_set_cookie_header(self):
        """Set-Cookie header 优先。"""
        resp = _make_response(
            json_body={"webLoginVid": "body_vid", "accessToken": "body_sk"},
            cookies={"wr_vid": "header_vid", "wr_skey": "header_sk"},
        )
        assert _extract_cookies(resp) == ("header_vid", "header_sk")

    def test_extracts_from_json_body_webloginvid_when_no_cookie_header(self):
        """无 Set-Cookie 时，从 JSON body webLoginVid/accessToken 提取。"""
        resp = _make_response(
            json_body={"webLoginVid": "vid2", "accessToken": "sk2"},
        )
        assert _extract_cookies(resp) == ("vid2", "sk2")

    def test_extracts_from_nested_cookies_field_fallback(self):
        """旧端点兼容：cookies.wr_vid / cookies.wr_skey。"""
        resp = _make_response(
            json_body={"cookies": {"wr_vid": "vid3", "wr_skey": "sk3"}},
        )
        assert _extract_cookies(resp) == ("vid3", "sk3")

    def test_raises_login_error_when_missing(self):
        resp = _make_response(json_body={"status": 1})
        with pytest.raises(LoginError, match="wr_vid / wr_skey"):
            _extract_cookies(resp)

    def test_raises_login_error_on_empty_dict(self):
        with pytest.raises(LoginError, match="wr_vid / wr_skey"):
            _extract_cookies({})

    def test_header_takes_precedence_over_webloginvid(self):
        """Set-Cookie header 优先级高于 webLoginVid。"""
        resp = _make_response(
            json_body={"webLoginVid": "body_vid", "accessToken": "body_sk"},
            cookies={"wr_vid": "header_vid", "wr_skey": "header_sk"},
        )
        vid, skey = _extract_cookies(resp)
        assert vid == "header_vid"
        assert skey == "header_sk"


# --------------------------------------------------------------------------- #
# login()：完整流程（全量 mock）
# --------------------------------------------------------------------------- #

class TestLogin:
    def _make_client_ctx(self) -> tuple[MagicMock, MagicMock]:
        """返回 (client_mock, ctx_mock)，ctx 模拟 httpx.Client 上下文管理器。"""
        client = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=client)
        ctx.__exit__ = MagicMock(return_value=False)
        return client, ctx

    def test_login_success_saves_cookie(self):
        client, ctx = self._make_client_ctx()

        uid_resp = _make_response({"uid": "test-uid-001"})
        ok_resp = _make_response({"succeed": True, "webLoginVid": "v", "accessToken": "s"})
        client.get.side_effect = [uid_resp, ok_resp]

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            patch("weread.auth.save_cookie") as mock_save,
        ):
            vid, skey = auth_mod.login(poll_max_rounds=2, long_poll_timeout=0.1)

        assert vid == "v"
        assert skey == "s"
        mock_save.assert_called_once_with("v", "s")

    def test_login_success_on_second_round_after_login_timeout(self):
        """第一轮服务端返回 LOGIN_TIMEOUT，第二轮成功。"""
        client, ctx = self._make_client_ctx()

        uid_resp = _make_response({"uid": "test-uid-002"})
        timeout_resp = _make_response({"succeed": False, "logicCode": "LOGIN_TIMEOUT"})
        ok_resp = _make_response({"succeed": True, "webLoginVid": "hv", "accessToken": "hs"})
        client.get.side_effect = [uid_resp, timeout_resp, ok_resp]

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            patch("weread.auth.save_cookie"),
        ):
            vid, skey = auth_mod.login(poll_max_rounds=2, long_poll_timeout=0.1)

        assert vid == "hv"
        assert skey == "hs"

    def test_login_timeout_after_max_rounds(self):
        """所有轮次均超时 → 抛 LoginTimeoutError。"""
        client, ctx = self._make_client_ctx()

        uid_resp = _make_response({"uid": "test-uid-003"})
        timeout_resp = _make_response({"succeed": False, "logicCode": "LOGIN_TIMEOUT"})
        # 1 次 getLoginUid + 2 次轮询全部返回 LOGIN_TIMEOUT
        client.get.side_effect = [uid_resp, timeout_resp, timeout_resp]

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            pytest.raises(LoginTimeoutError),
        ):
            auth_mod.login(poll_max_rounds=2, long_poll_timeout=0.1)

    def test_login_timeout_via_read_timeout_exception(self):
        """服务端长连接超时（ReadTimeout）→ 继续轮询，超出轮次后抛 LoginTimeoutError。"""
        client, ctx = self._make_client_ctx()

        uid_resp = _make_response({"uid": "test-uid-004"})
        client.get.side_effect = [
            uid_resp,
            httpx.ReadTimeout("timeout", request=None),
            httpx.ReadTimeout("timeout", request=None),
        ]

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            pytest.raises(LoginTimeoutError),
        ):
            auth_mod.login(poll_max_rounds=2, long_poll_timeout=0.1)

    def test_login_qr_expired_raises_login_error(self):
        """二维码失效（EXPIRED）→ 抛 LoginError。"""
        client, ctx = self._make_client_ctx()

        uid_resp = _make_response({"uid": "test-uid-005"})
        expired_resp = _make_response({"succeed": False, "logicCode": "EXPIRED"})
        client.get.side_effect = [uid_resp, expired_resp]

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            pytest.raises(LoginError, match="二维码已失效"),
        ):
            auth_mod.login(poll_max_rounds=2, long_poll_timeout=0.1)

    def test_login_with_otp(self):
        """NEED_OTP 场景：提示输入验证码，带 otp 重新轮询后成功。"""
        client, ctx = self._make_client_ctx()

        uid_resp = _make_response({"uid": "test-uid-006"})
        otp_needed = _make_response({"succeed": False, "logicCode": "NEED_OTP"})
        ok_resp = _make_response({"succeed": True, "webLoginVid": "vv", "accessToken": "ss"})
        client.get.side_effect = [uid_resp, otp_needed, ok_resp]

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            patch("builtins.input", return_value="123456"),
            patch("weread.auth.save_cookie"),
        ):
            vid, skey = auth_mod.login(poll_max_rounds=3, long_poll_timeout=0.1)

        assert vid == "vv"
        assert skey == "ss"

    def test_fetch_uid_failure_propagates(self):
        """`_fetch_login_uid` 失败时（接口无 uid 字段）→ 抛 LoginError。"""
        client, ctx = self._make_client_ctx()

        bad_uid_resp = _make_response({"error": "server error"})
        client.get.return_value = bad_uid_resp

        with (
            patch("weread.auth.httpx.Client", return_value=ctx),
            patch("weread.auth._render_qr"),
            pytest.raises(LoginError),
        ):
            auth_mod.login(poll_max_rounds=2, long_poll_timeout=0.1)
