"""用本机 Edge 跑 goofish 的扫码登录（绕过 goofish 写死的 channel='chrome'）。

背景：
  goofish-cli 的 `auth login --qr` 在底层 `goofish_cli.core.browser.goofish_page` 里
  硬编码了 `channel="chrome"`，即只认系统 Chrome。很多电脑（比如这台）只装了 Edge，
  于是 `--qr` 直接报 "Chromium distribution 'chrome' is not found"。

  但实际上 Edge 也是 Chromium，Playwright 完全能驱动。本模块：
    1. 找到本机 msedge.exe；
    2. 把 `goofish_cli.core.browser.goofish_page` 替换成一个等价实现，
       只把启动参数从 `channel="chrome"` 改成 `executable_path=<Edge>`；
    3. 其余全部复用 goofish 原版扫码逻辑（等待 .qrcode-login 渲染、
       轮询 _m_h5_tk/unb/cookie2、写回 cookies.json）。

这样：
  - 不需要安装 Chrome（省 ~150MB 下载，也避开管理员权限问题）；
  - 不需要 Playwright 自带 chromium；
  - 弹出的就是真实 Edge 窗口，闲鱼 App 扫码即可，风控角度等同正常浏览。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import goofish_cli.core.browser as _b
from loguru import logger


def find_edge() -> str:
    """返回本机 msedge.exe 路径；找不到返回空串。"""
    env_pf = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    env_pf2 = os.environ.get('ProgramFiles', r'C:\Program Files')
    candidates = [
        os.path.join(env_pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(env_pf2, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # 兜底：系统 PATH 里有 msedge
    try:
        import shutil as _s
        p = _s.which('msedge')
        if p:
            return p
    except Exception:
        pass
    return ''


@asynccontextmanager
async def _edge_page(*, headless=None, viewport=(1440, 900), cookies=None,
                     persist_session=True):
    """goofish_page 的等价实现，仅浏览器换成 Edge。复用 goofish 的 cookie 工具函数。"""
    from playwright.async_api import async_playwright

    edge = find_edge()
    if not edge:
        raise RuntimeError('本机未找到 Microsoft Edge，无法用扫码登录')

    if headless is None:
        headless = os.environ.get('GOOFISH_HEADLESS') == '1'

    profiles_parent = _b.PROFILES_PARENT
    if cookies is None:
        cookies = _b._load_cookies_from_session()
    pw_cookies = _b._cookies_to_playwright(cookies)

    profiles_parent.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix='edge-', dir=str(profiles_parent)))
    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path=edge,          # ← 关键：用 Edge 而不是 channel='chrome'
                headless=headless,
                viewport={'width': viewport[0], 'height': viewport[1]},
                locale='zh-CN',
                timezone_id='Asia/Shanghai',
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-default-browser-check',
                    '--no-first-run',
                ],
            )
            try:
                await context.add_cookies(pw_cookies)
            except Exception as e:  # noqa: BLE001
                logger.warning(f'注入 cookie 失败：{e}')
            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            try:
                yield page
            finally:
                await context.close()
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def login_via_qr(timeout: int = 180, log_sink=None) -> dict:
    """驱动 goofish 原版扫码登录，但用 Edge 弹窗。

    返回 {'ok': bool, 'reason': str, 'msg': str}。
    log_sink 可选，传一个 callable(str) 用于把 goofish 日志实时转发到 UI。
    """
    edge = find_edge()
    if not edge:
        return {'ok': False, 'reason': 'no_edge',
                'msg': '本机未找到 Microsoft Edge，无法弹窗扫码。可改用「导入浏览器登录态」。'}

    import goofish_cli.core.qr_login as _ql

    # 让 qr_login 内部 `from goofish_cli.core.browser import goofish_page` 拿到我们的 Edge 版本
    _b.goofish_page = _edge_page

    sinks = []
    if log_sink:
        try:
            h = logger.add(lambda m: log_sink(m.strip()), level='INFO',
                           format='{message}')
            sinks.append(h)
        except Exception:
            pass

    try:
        records = _ql.login_via_qr(timeout=timeout, persist=True)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'reason': 'exception', 'msg': f'扫码登录异常：{e}'}
    finally:
        for h in sinks:
            try:
                logger.remove(h)
            except Exception:
                pass

    if records:
        return {'ok': True, 'reason': 'done',
                'msg': f'登录成功，已写入 {len(records)} 条 cookie'}
    return {'ok': False, 'reason': 'timeout',
            'msg': '超时未登录（或浏览器未弹窗）。请在弹出的 Edge 窗口里用闲鱼 App 扫码。'}
