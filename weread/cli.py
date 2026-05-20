"""
weread/cli.py — Click 命令行入口

命令：
    weread          默认启动 TUI（未登录时自动引导扫码）
    weread login    强制重新扫码登录（覆盖旧 Cookie）
    weread logout   清除本地凭证及书架缓存
"""

from __future__ import annotations

import sys

import click

from weread import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="weread")
@click.pass_context
def main(ctx: click.Context) -> None:
    """macOS 终端微信读书阅读器。"""
    if ctx.invoked_subcommand is None:
        _run_tui()


def _run_tui() -> None:
    """读取 Cookie → 必要时登录 → 启动 TUI。"""
    # 懒导入重依赖，加快命令行响应
    from weread.auth import LoginError, LoginTimeoutError, load_cookie, login
    from weread.api import CookieExpiredError

    cookie = load_cookie()
    if cookie is None:
        click.echo("未检测到登录信息，正在引导登录...\n")
        try:
            cookie = login()
        except LoginTimeoutError as exc:
            click.echo(f"错误：{exc}", err=True)
            sys.exit(1)
        except LoginError as exc:
            click.echo(f"登录失败：{exc}", err=True)
            sys.exit(1)
        except Exception as exc:
            click.echo(f"未知错误：{exc}", err=True)
            sys.exit(1)

    vid, skey = cookie

    # 启动 TUI
    from weread.tui.app import WeReadApp

    app = WeReadApp(vid=vid, skey=skey)
    app.run()


@main.command()
def login() -> None:
    """扫码登录微信读书（覆盖现有 Cookie），登录成功后直接启动 TUI。"""
    from weread.auth import LoginError, LoginTimeoutError, login as do_login

    try:
        do_login()
    except LoginTimeoutError as exc:
        click.echo(f"错误：{exc}", err=True)
        sys.exit(1)
    except LoginError as exc:
        click.echo(f"登录失败：{exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"未知错误：{exc}", err=True)
        sys.exit(1)

    # 登录成功后直接进入 TUI，不再需要用户手动执行 weread
    _run_tui()


@main.command()
def logout() -> None:
    """退出登录，清除本地凭证及书架缓存。"""
    from weread.auth import clear_cookie
    from weread.state import clear_shelf_cache

    clear_cookie()
    clear_shelf_cache()
    click.echo("已退出登录，本地凭证及书架缓存已清除。")
