"""
tests/test_cli.py — CLI 命令单元测试

测试范围：
- weread --help / --version
- weread login（mock auth.login）
- weread logout（mock clear_cookie + clear_shelf_cache）
- weread（无 cookie 时自动 login + 启动 TUI；有 cookie 时直接启动 TUI）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from weread.cli import main


# ---------------------------------------------------------------------------
# 基础命令
# ---------------------------------------------------------------------------

class TestHelpVersion:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "微信读书" in result.output

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "weread" in result.output


# ---------------------------------------------------------------------------
# weread login
# ---------------------------------------------------------------------------

class TestLoginCommand:
    def _make_mock_app(self):
        mock_app = MagicMock()
        mock_app.run = MagicMock()
        return mock_app

    def test_login_success_then_starts_tui(self):
        """登录成功后应自动启动 TUI（方案 B）。"""
        runner = CliRunner()
        mock_app = self._make_mock_app()
        with (
            patch("weread.auth.login", return_value=("vid123", "skey456")) as mock_login,
            patch("weread.auth.load_cookie", return_value=("vid123", "skey456")),
            patch("weread.tui.app.WeReadApp", return_value=mock_app),
        ):
            result = runner.invoke(main, ["login"])
        assert result.exit_code == 0
        mock_login.assert_called_once()
        mock_app.run.assert_called_once()

    def test_login_timeout(self):
        from weread.auth import LoginTimeoutError

        runner = CliRunner()
        with patch("weread.auth.login", side_effect=LoginTimeoutError("超时")):
            result = runner.invoke(main, ["login"])
        assert result.exit_code == 1
        assert "超时" in result.output

    def test_login_error(self):
        from weread.auth import LoginError

        runner = CliRunner()
        with patch("weread.auth.login", side_effect=LoginError("接口异常")):
            result = runner.invoke(main, ["login"])
        assert result.exit_code == 1

    def test_login_unknown_error(self):
        runner = CliRunner()
        with patch("weread.auth.login", side_effect=RuntimeError("未知")):
            result = runner.invoke(main, ["login"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# weread logout
# ---------------------------------------------------------------------------

class TestLogoutCommand:
    def test_logout_clears_cookie_and_cache(self):
        runner = CliRunner()
        with (
            patch("weread.auth.clear_cookie") as mock_clear_cookie,
            patch("weread.state.clear_shelf_cache") as mock_clear_cache,
        ):
            result = runner.invoke(main, ["logout"])
        assert result.exit_code == 0
        mock_clear_cookie.assert_called_once()
        mock_clear_cache.assert_called_once()
        assert "已退出" in result.output


# ---------------------------------------------------------------------------
# weread（默认命令，启动 TUI）
# ---------------------------------------------------------------------------

class TestDefaultCommand:
    def _make_mock_app(self):
        """返回一个 mock App，run() 什么都不做。"""
        mock_app = MagicMock()
        mock_app.run = MagicMock()
        return mock_app

    def test_with_existing_cookie_starts_tui(self):
        runner = CliRunner()
        mock_app = self._make_mock_app()
        with (
            patch("weread.auth.load_cookie", return_value=("vid", "skey")),
            patch("weread.tui.app.WeReadApp", return_value=mock_app),
        ):
            result = runner.invoke(main, [])
        assert result.exit_code == 0
        mock_app.run.assert_called_once()

    def test_without_cookie_triggers_login_then_tui(self):
        runner = CliRunner()
        mock_app = self._make_mock_app()
        with (
            patch("weread.auth.load_cookie", return_value=None),
            patch("weread.auth.login", return_value=("vid", "skey")) as mock_login,
            patch("weread.tui.app.WeReadApp", return_value=mock_app),
        ):
            result = runner.invoke(main, [])
        assert result.exit_code == 0
        mock_login.assert_called_once()
        mock_app.run.assert_called_once()

    def test_without_cookie_login_fails_exits(self):
        from weread.auth import LoginError

        runner = CliRunner()
        with (
            patch("weread.auth.load_cookie", return_value=None),
            patch("weread.auth.login", side_effect=LoginError("fail")),
        ):
            result = runner.invoke(main, [])
        assert result.exit_code == 1

    def test_without_cookie_login_timeout_exits(self):
        from weread.auth import LoginTimeoutError

        runner = CliRunner()
        with (
            patch("weread.auth.load_cookie", return_value=None),
            patch("weread.auth.login", side_effect=LoginTimeoutError("timeout")),
        ):
            result = runner.invoke(main, [])
        assert result.exit_code == 1
