# -*- coding: utf-8 -*-
"""自导出闲鱼号商品 —— 不依赖忙鱼，无导出额度限制。

做法：用本机 Edge 的独立资料目录打开 goofish.com，人工登录自己的号，
浏览器自己会调 mtop 接口；本脚本只「读」这两个响应：
  · mtop.idle.web.xyh.item.list        个人页商品列表（分页）
  · mtop.idle.pc.idleitem.editDetail   商品编辑详情（标题/描述/价格/类目/图片）
再下载图片，拼成与忙鱼导出包一致的 manifest.json + media/<itemId>/，
打包到工具包内的「数据/收件箱/」（可在 config.json 改位置），工作台第一步可直接导入。

合规边界：只导当前登录的自有账号；不搜索他人商品、不批量注册、不伪造设备、
不绕过滑块或风控。遇到验证请人工在浏览器里处理，脚本会停下并报错。
所有数据只落本机，不上传。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

# 控制台代码页可能不是 UTF-8；不让个别生僻字把整个导出中断
try:
    sys.stdout.reconfigure(errors='replace')
except Exception:
    pass

# ---- 可移植路径层：与工作台共用工具包根的 config.json ----
ROOT = Path(__file__).resolve().parent          # .../闲鱼搬家用工具包/app
BASE = ROOT.parent                             # 工具包根目录
CONFIG_FILE = BASE / 'config.json'
try:
    CFG = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
except Exception:
    CFG = {}
_raw = str(CFG.get('data_dir') or '数据').strip()
_p = Path(_raw)
DATA = (_p if _p.is_absolute() else BASE / _p).resolve()
INBOX = DATA / '收件箱'
PROFILE = DATA / '浏览器资料'
INBOX.mkdir(parents=True, exist_ok=True)
PROFILE.mkdir(parents=True, exist_ok=True)
HOME = 'https://www.goofish.com/'

S = {
    'cards': {},        # itemId -> cardData
    'detail': {},       # itemId -> editDetail data
    'total': 0,
    'nextPage': True,
    'groups': [],
    'errors': [],
    'seen': [],             # 最近拦截到的接口，排错用
    'blocked': False,
}


def say(msg: str) -> None:
    print(f'[{dt.datetime.now():%H:%M:%S}] {msg}', flush=True)


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '', s or '')
    s = re.sub(r'\s+', ' ', s).strip()[:24].strip().rstrip('. ')
    return s or '未命名'


def parse_response(resp) -> dict:
    """mtop 一般返回纯 JSON；偶发 JSONP，兜底抠出大括号。"""
    try:
        return resp.json()
    except Exception:
        try:
            txt = resp.text()
            m = re.search(r'\{.*\}', txt, re.S)
            return json.loads(m.group(0)) if m else {}
        except Exception:
            return {}


def ret_of(j: dict) -> str:
    ret = j.get('ret') or []
    return str(ret[0]) if ret else ''


def is_blocked(ret: str) -> bool:
    return any(k in ret for k in ('RGV587', 'SESSION_EXPIRED', 'FAIL_SYS',
                                  'TOKEN_EXPIRED', '滑块', '风控'))


def on_response(resp) -> None:
    """只记录，不改请求、不重放。"""
    u = resp.url
    S['seen'].append(u.split('/h5/')[-1].split('/')[0] if '/h5/' in u else u[-60:])
    if len(S['seen']) > 60:
        S['seen'].pop(0)
    if 'mtop.idle.web.xyh.item.list' in u:
        j = parse_response(resp)
        r = ret_of(j)
        if r and 'SUCCESS' not in r:
            S['errors'].append(f'商品列表：{r}')
            if is_blocked(r):
                S['blocked'] = True
            return
        d = j.get('data') or {}
        for c in d.get('cardList') or []:
            cd = c.get('cardData') or {}
            dp = cd.get('detailParams') or {}
            iid = str(cd.get('id') or dp.get('itemId') or '')
            if iid:
                S['cards'][iid] = cd
        if d.get('totalCount') is not None:
            S['total'] = int(d['totalCount'])
        if 'nextPage' in d:
            S['nextPage'] = bool(d['nextPage'])
        if d.get('itemGroupList'):
            S['groups'] = d['itemGroupList']
    elif 'mtop.idle.pc.idleitem.editDetail' in u:
        j = parse_response(resp)
        r = ret_of(j)
        if r and 'SUCCESS' not in r:
            S['errors'].append(f'编辑详情：{r}')
            if is_blocked(r):
                S['blocked'] = True
            return
        d = j.get('data') or j
        iid = str(d.get('itemId') or '')
        if iid:
            S['detail'][iid] = d
    elif 'user.page.nav' in u or 'userPageNav' in u:
        j = parse_response(resp)
        d = j.get('data') or {}
        for k in ('userId', 'user_id', 'unb'):
            if d.get(k):
                S.setdefault('navUserId', str(d[k]))
                break


def harvest_urls(node) -> list[str]:
    """字段名随版本变，递归找图片直链，不写死 imageInfoDOList 的元素键名。"""
    out = []

    def walk(x):
        if isinstance(x, str):
            v = x.strip()
            if v.startswith('//'):
                v = 'https:' + v
            if (v.startswith('http') and
                    any(h in v for h in ('img.alicdn', 'tbcdn', '/bao/uploaded/', 'goofish.com/img')) and
                    any(x in v.lower() for x in ('.webp', '.jpg', '.jpeg', '.png', '_q90', '!')) and
                    not any(k in v for k in ('logo', 'icon', 'avatar', 'placeholder', '.js', '.css'))):
                out.append(v)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(node)
    return out


def extract_images(detail: dict, card: dict) -> list[str]:
    urls: list[str] = []
    for key in ('imageInfoDOList', 'imageList', 'images', 'picList', 'imageInfoList'):
        v = detail.get(key)
        if isinstance(v, list) and v:
            urls += harvest_urls(v)
    if not urls:
        urls += harvest_urls(detail)
    pic = (card.get('picInfo') or {}).get('picUrl')
    if pic:
        p = str(pic)
        if p.startswith('//'):
            p = 'https:' + p
        elif p.startswith('http://'):
            p = 'https://' + p[7:]
        urls.append(p)
    seen, uniq = set(), []
    for u in urls:
        key = u.split('?')[0]
        if key in seen or not u.startswith('http'):
            continue
        seen.add(key)
        uniq.append(u)
    return uniq[:9]                       # 工作台/闲鱼单品最多 9 张


def ext_of(url: str, ctype: str) -> str:
    low = (url + ' ' + ctype).lower()
    if '.webp' in low or 'webp' in ctype.lower():
        return '.webp'
    if '.png' in low or 'png' in ctype.lower():
        return '.png'
    return '.jpg'


def download(ctx, url: str, folder: Path, order: int, ua: str) -> dict | None:
    try:
        r = ctx.request.get(url, headers={
            'Referer': 'https://www.goofish.com/',
            'User-Agent': ua,
        }, timeout=30000)
        if not r.ok:
            return None
        body = r.body()
        if len(body) < 1024:
            return None
        ctype = (r.headers.get('content-type') or '').split(';')[0].strip().lower()
        if not ctype.startswith('image/'):
            return None                      # 拦到的是 HTML / 验证页，不能当图片存
        name = f'{order:02d}{ext_of(url, ctype)}'
        p = folder / name
        p.write_bytes(body)
        return {'name': name, 'url': url, 'bytes': len(body),
                'mime': ctype, 'sha': hashlib.sha256(body).hexdigest()}
    except Exception:
        return None


def price_text(detail: dict, card: dict) -> str:
    p = detail.get('itemPriceDTO') or {}
    cent = p.get('priceInCent')
    try:
        if cent is not None and str(cent).isdigit():
            v = int(cent) / 100
            return f'{v:g}'
    except Exception:
        pass
    pi = card.get('priceInfo') or {}
    return str(pi.get('price') or (card.get('detailParams') or {}).get('soldPrice') or '0')


def build_snapshot(iid: str, card: dict, detail: dict, pics: list[dict]) -> dict:
    text = detail.get('itemTextDTO') or {}
    cat = detail.get('itemCatDTO') or {}
    dp = card.get('detailParams') or {}
    cid = str(cat.get('catId') or dp.get('categoryId') or '')
    url = f'https://www.goofish.com/item?id={iid}' + (f'&categoryId={cid}' if cid else '')
    media = []
    for i, p in enumerate(pics):
        media.append({'order': i, 'type': 'image', 'url': p['url'], 'sha256': p['sha']})
    return {
        'schemaVersion': 1,
        'itemId': iid,
        'sourceUrl': url,
        'categoryId': cid,
        'category': {'channelCatId': str(cat.get('channelCatId') or ''),
                     'name': str(cat.get('catName') or '')},
        'capturedAt': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        'displayTitle': str(text.get('title') or card.get('title') or ''),
        'description': str(text.get('desc') or text.get('description') or ''),
        'price': {'currencySymbol': '¥', 'amountText': price_text(detail, card)},
        # 工作台发布链路固定写「包邮」；这里不猜 postPriceInCent，避免把运费写错
        'shippingText': '包邮',
        'attributes': [],
        'media': media,
        'itemStatus': card.get('itemStatus'),
        'source': '本机自导出（mtop 拦截）',
        'rawDetailKeys': sorted(detail.keys())[:40],
    }


def wait_for_login(ctx, seconds: int = 300) -> str:
    say('浏览器已打开。请在窗口里登录要导出的那个闲鱼号（扫码即可）。')
    say(f'检测到登录态会自动继续，最长等 {seconds // 60} 分钟；遇到滑块请人工过一下。')
    end = time.time() + seconds
    while time.time() < end:
        try:
            for c in ctx.cookies():
                if c.get('name') == 'unb' and c.get('value'):
                    unb = str(c['value'])
                    say(f'已登录，unb = {unb[:6]}****  （个人页 userId 通常就是它）')
                    return unb
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('等不到登录态。确认已在打开的 Edge 里登录 goofish.com 后重跑。')


def click_onsale(page) -> bool:
    """有「在售」分组就切过去；若前端不重新发请求，把原卡片还回来，别丢数据。"""
    if not any('在售' in str(g.get('groupName', '')) for g in S['groups']):
        return False
    for sel in ('text=在售', '//span[normalize-space()="在售"]',
                '//div[normalize-space()="在售"]'):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                backup = dict(S['cards'])
                S['cards'].clear()
                S['total'] = 0
                S['nextPage'] = True
                loc.click(timeout=4000)
                page.wait_for_timeout(1800)
                if not S['cards']:
                    S['cards'].update(backup)
                    S['total'] = len(S['cards'])
                    S['nextPage'] = False
                    say('「在售」标签没触发新列表，保留综合分组的全部卡片（含已售出）。')
                else:
                    say('已切到「在售」分组，重新收集。')
                return True
        except Exception:
            continue
    say('没点到「在售」标签，改为收集全部卡片（含已售出），稍后请在工作台里筛。')
    return False


def collect_cards(page, url: str, include_sold: bool = False, max_scrolls: int = 40) -> None:
    say('打开个人页，读取商品列表…')
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    for _ in range(30):
        if S['cards'] or S['blocked']:
            break
        page.wait_for_timeout(1000)
    if S['blocked']:
        raise RuntimeError('列表接口被风控拦下（RGV587 / 会话过期），请人工处理后重跑。')
    if not include_sold:
        click_onsale(page)
    # 个人页可能是内部容器滚动，只滚 window 不触发下一页
    scroll_js = """() => {
        window.scrollTo(0, document.body.scrollHeight);
        for (const el of document.querySelectorAll('div,main,section,ul')) {
            if (el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 200) {
                try { el.scrollTop = el.scrollHeight; } catch (e) {}
            }
        }
    }"""
    last, stagnant = -1, 0
    for i in range(max_scrolls):
        if S['blocked']:
            break
        page.evaluate(scroll_js)
        page.wait_for_timeout(1100)
        n = len(S['cards'])
        stagnant = stagnant + 1 if n == last else 0
        last = n
        if stagnant >= 6:
            say('连续 6 次滚动没有新卡片，停止翻页。')
            break
        if not S['nextPage']:
            break
        if S['total'] and n >= S['total']:
            break
    say(f'列表收集完成：{len(S["cards"])} 件（接口报 totalCount={S["total"]}）')
    for g in S['groups']:
        say(f'  分组：{g.get("groupName")} {g.get("itemNumber")} 件')


def main() -> int:
    ap = argparse.ArgumentParser(description='自导出闲鱼号商品（本机浏览器拦截，只读）')
    ap.add_argument('--name', default='', help='导出包名，默认用日期')
    ap.add_argument('--user-id', default='', help='个人页 userId，默认取登录 cookie 的 unb')
    ap.add_argument('--limit', type=int, default=0, help='最多导几件，0 = 全部（先试几件用 --limit 3）')
    ap.add_argument('--start', type=int, default=0, help='从第几件开始（0 起），配合 --limit 分批导出')
    ap.add_argument('--slow', type=float, default=2.5, help='每件之间等待秒数，默认 2.5')
    ap.add_argument('--include-sold', action='store_true', help='不切「在售」分组，保留已售出')
    ap.add_argument('--profile', default=str(PROFILE), help='Edge 独立资料目录')
    ap.add_argument('--out', default=str(INBOX), help='ZIP 输出目录')
    a = ap.parse_args()

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    Path(a.profile).mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M')
    zipname = f'自导出_{safe_name(a.name) if a.name else stamp}.zip'
    zpath = outdir / zipname

    say('=' * 66)
    say('自导出器：只导当前登录的自有账号，数据只落本机，不上传。')
    say('风控提醒：读接口也会慢跑（默认 2.5 秒/件）；遇滑块请人工过，脚本不绕。')
    say(f'本次导出：{str(a.limit) + " 件" if a.limit else "全部在售商品"}。'
        '件数多时别关窗口，全部跑完才生成 ZIP。')
    say(f'导出 ZIP 会放到：{zpath}')
    say('浏览器打开后，请在那个独立 Edge 窗口里登录「要导出的号」，登录后自动继续。')
    say('=' * 66)

    with sync_playwright() as pw:
        # 不伪造 UA、不伪装设备：用本机 Edge 自己的指纹，风控面最干净
        ctx = pw.chromium.launch_persistent_context(
            a.profile, channel='msedge', headless=False,
            viewport={'width': 1440, 'height': 900},
            args=['--no-first-run', '--no-default-browser-check'],
        )
        try:
            ctx.on('response', on_response)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until='domcontentloaded', timeout=60000)
            unb = wait_for_login(ctx)
            uid = a.user_id or unb
            try:
                ua = str(page.evaluate('navigator.userAgent'))
            except Exception:
                ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'
            say(f'个人页 userId = {uid}')
            collect_cards(page, f'https://www.goofish.com/personal?userId={uid}', a.include_sold)
            if not S['cards']:
                raise RuntimeError('没抓到商品列表。可能 userId 不对或页面没加载出来；'
                                   '可加 --user-id 指定个人页 URL 里的数字。')

            items = list(S['cards'].items())
            if a.start > 0:
                items = items[a.start:]
                say(f'从第 {a.start + 1} 件开始（--start {a.start}）')
            if a.limit > 0:
                items = items[:a.limit]
                say(f'本次导 {len(items)} 件（--limit {a.limit}）')

            with tempfile.TemporaryDirectory(prefix='xy_export_') as td:
                tmp = Path(td)
                media_files, snaps, warns = [], [], []
                for n, (iid, card) in enumerate(items, 1):
                    if S['blocked']:
                        raise RuntimeError('接口被风控拦下，已停止。请人工处理验证后重跑。')
                    S['detail'].pop(iid, None)
                    try:
                        page.goto(f'https://www.goofish.com/publish?itemId={iid}',
                                  wait_until='domcontentloaded', timeout=45000)
                        for _ in range(14):
                            if iid in S['detail']:
                                break
                            page.wait_for_timeout(800)
                    except Exception as e:
                        warns.append(f'{iid} 编辑页打开失败：{str(e)[:80]}')
                    detail = S['detail'].get(iid, {})
                    if not detail:
                        warns.append(f'{iid} 没拿到编辑详情，只用列表卡片字段（描述可能为空）')
                    urls = extract_images(detail, card)
                    folder = tmp / 'media' / iid
                    folder.mkdir(parents=True, exist_ok=True)
                    pics = []
                    for u in urls:
                        got = download(ctx, u, folder, len(pics), ua)
                        if got:
                            pics.append(got)
                        page.wait_for_timeout(250)
                    if not pics:
                        warns.append(f'{iid} 一张图都没下下来，该件跳过')
                        continue
                    snap = build_snapshot(iid, card, detail, pics)
                    if not snap['displayTitle'] or not snap['description']:
                        warns.append(f'{iid} 标题或描述为空，工作台会拦下，请人工补')
                    snaps.append(snap)
                    for k, p in enumerate(pics):
                        media_files.append({
                            'path': f'media/{iid}/{p["name"]}', 'blobKey': f'{iid}:{k}',
                            'itemId': iid, 'order': k, 'type': 'image',
                            'sourceUrl': p['url'], 'mimeType': p['mime'],
                            'byteLength': p['bytes'], 'sha256': p['sha'],
                        })
                    say(f'[{n}/{len(items)}] {iid}  {snap["displayTitle"][:26]}  '
                        f'¥{snap["price"]["amountText"]}  {len(pics)} 图')
                    page.wait_for_timeout(int(a.slow * 1000))

                if not snaps:
                    raise RuntimeError('一件都没导出来，未生成 ZIP。')

                mf = {
                    'schemaVersion': 1,
                    'createdAt': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                    'snapshots': snaps,
                    'mediaFiles': media_files,
                    'source': '本机自导出 / 浏览器 mtop 响应拦截',
                }
                report = [
                    '闲鱼账号商品自导出包',
                    f'导出时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}',
                    f'账号 userId：{uid}',
                    f'商品：{len(snaps)} 件 | 图片：{len(media_files)} 张',
                    f'分组：{json.dumps(S["groups"], ensure_ascii=False)[:400]}',
                    '',
                    '字段来源：',
                    '  列表  → mtop.idle.web.xyh.item.list（cardList / itemGroupList）',
                    '  详情  → mtop.idle.pc.idleitem.editDetail（itemTextDTO / itemPriceDTO /',
                    '          itemCatDTO / imageInfoDOList / itemPostFeeDTO）',
                    '  图片  → 图片直链下载到 media/<商品ID>/，格式对齐忙鱼导出包',
                    '',
                    '注意：',
                    '  · 文案是原号原文，工作台导入后默认全部不纳入，必须逐件重写。',
                    '  · 自动分档只是正则初筛，最终判据是人工核对。',
                    '  · 已按「在售」分组尽力过滤，仍可能含已售出，导入后请抽查。',
                    '  · 本包只含本机抓到的字段，没有人工决策表和改写文案。',
                    '',
                    f'使用：把本 ZIP 留在 {INBOX}，打开工作台 → 第一步选它 → 导入并切换。',
                    '',
                    '警告：',
                ] + [f'  · 接口：{e}' for e in S['errors']] \
                  + [f'  · {w}' for w in warns] \
                  + (['  （无）'] if not (warns or S['errors']) else [])
                with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
                    z.writestr('manifest.json', json.dumps(mf, ensure_ascii=False))
                    z.writestr('导出说明.txt', '\n'.join(report))
                    for m in media_files:
                        z.write(tmp / m['path'], m['path'])

            say('=' * 66)
            say(f'导出完成：{len(snaps)} 件 / {len(media_files)} 张图 / '
                f'{zpath.stat().st_size / 1024 / 1024:.1f} MB')
            say(f'ZIP：{zpath}')
            say(f'提醒 {len(warns)} 条' + ('：' + warns[0][:60] if warns else ''))
            say('下一步：双击「启动工作台.cmd」→ 第一步下拉选这个 ZIP → 导入并切换。')
            say('=' * 66)
            return 0
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say('已手动中断，未生成 ZIP。')
        sys.exit(130)
    except Exception as e:
        say(f'失败：{e}')
        if S['seen']:
            print('最近拦截到的接口： ' + ' | '.join(list(dict.fromkeys(S['seen']))[-12:]),
                  flush=True)
        if S['errors']:
            print('接口报错： ' + ' | '.join(S['errors'][-5:]), flush=True)
        print('把上面几行发我，我按你账号的真实响应调字段。', flush=True)
        sys.exit(1)
