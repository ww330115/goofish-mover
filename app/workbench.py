# -*- coding: utf-8 -*-
"""闲鱼搬运工作台 —— 本地可视化后端（127.0.0.1，纯标准库 + cv2/numpy）

四步：① 环境准备（自检+装依赖）② 导出包 ③ 清洗（二维码/水印/面单 + 文案改写）④ 发布（演练优先）
所有数据只在这台电脑上，不上传任何内容。
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- 可移植路径层：一切从工具包根目录推导，不依赖用户名 / 工作区目录 / 盘符 ----
ROOT = Path(__file__).resolve().parent          # .../闲鱼搬家用工具包/app
BASE = ROOT.parent                             # 工具包根目录（整个文件夹可随意复制/改名）
# 无论是双击 .cmd、python app/workbench.py 还是被别的脚本 import，都能找到同目录的引擎
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    import rewrite_engine                       # 离线规则改写，无外部依赖
except Exception as _e:                         # 缺文件也不影响工作台其它功能
    rewrite_engine = None
    print(f'自动文案改写引擎不可用：{_e}')
CONFIG_FILE = BASE / 'config.json'


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
    except Exception:
        return {}


def data_from(cfg: dict) -> Path:
    raw = str(cfg.get('data_dir') or '数据').strip()
    p = Path(raw)
    return (p if p.is_absolute() else BASE / p).resolve()


DATA = data_from(load_config())
REWRITE = ROOT / 'build_import_zip.py'          # 仅 A 号项目的改写文案字典
DECISION = ROOT / '参考' / '闲鱼A号迁移决策表_20261104.xlsx'


def rebind_paths() -> None:
    """数据目录可配置；切换时所有路径一起重算。"""
    global DATA, INBOX, MAT, PACKAGES, PROFILE, INDEX_FILE, LOGFILE
    global LEGACY_STATE, LEGACY_SRC, LEGACY_OUT
    DATA = data_from(load_config())
    INBOX = DATA / '收件箱'
    MAT = DATA / '项目'
    PACKAGES = DATA / '发布包'
    PROFILE = DATA / '浏览器资料'
    INDEX_FILE = DATA / '工作台项目.json'
    LOGFILE = DATA / '工作台日志.txt'
    LEGACY_STATE = MAT / '工作台状态.json'
    LEGACY_SRC = MAT / '20261104-闲鱼A号导出'
    LEGACY_OUT = PACKAGES / '发布包_20261105'
    for d in (DATA, INBOX, MAT, PACKAGES, PROFILE):
        d.mkdir(parents=True, exist_ok=True)


rebind_paths()

PORT = 8765
LOCK = threading.Lock()
BUILD_LOCK = threading.Lock()
IMPORT_LOCK = threading.Lock()
LOGS: list[str] = []
PUB = {'running': False, 'stop': False, 'mode': 'dry', 'done': 0,
       'total': 0, 'ok': 0, 'fail': 0, 'current': '', 'started': None, 'count': 0}
IMPORT = {'running': False, 'stage': '空闲', 'done': 0, 'total': 0,
          'project': '', 'error': '', 'finishedAt': ''}

# ---- 当前项目：切换项目时整体重算，下面这些路径不再是常量 ----
PROJECTS: dict = {'active': '', 'list': []}
ACTIVE: dict = {}
SRC = MEDIA = MANIFEST = SCAN = STATE_FILE = OUTROOT = None
CATALOG: list[dict] = []
BYID: dict[str, dict] = {}


def log(msg: str) -> None:
    line = f'[{dt.datetime.now():%H:%M:%S}] {msg}'
    with LOCK:
        LOGS.append(line)
        if len(LOGS) > 800:
            del LOGS[:200]
    try:
        with open(LOGFILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


def jload(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return default if default is not None else {}


def jwrite(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


# ------------------------------------------------------- 多账号 / 多导出包
def is_legacy() -> bool:
    """首批 A 号项目：有人工决策表和改写文案，别的项目走自动扫描"""
    return bool(ACTIVE.get('legacy'))


def norm_project(p: dict) -> dict:
    """台账只存身份信息，绝不存绝对路径 —— 换电脑 / 换盘符 / 改目录名都不会失效。"""
    out = {k: p.get(k) for k in ('id', 'name', 'zip', 'importedAt',
                                 'itemCount', 'legacy', 'scanned') if k in p}
    out.setdefault('id', '')
    out.setdefault('name', out['id'] or '未命名')
    out.setdefault('zip', '')
    out.setdefault('importedAt', '')
    out.setdefault('itemCount', 0)
    out.setdefault('legacy', False)
    out.setdefault('scanned', False)
    return out


def project_paths(p: dict) -> dict:
    """按 id 推导三套路径，与台账里存过什么无关。"""
    pid = p['id']
    if p.get('legacy'):
        return {'dir': LEGACY_SRC, 'state': LEGACY_STATE, 'outroot': LEGACY_OUT}
    d = MAT / pid
    return {'dir': d, 'state': d / '工作台状态.json',
            'outroot': PACKAGES / ('发布包_' + pid)}


def init_projects() -> None:
    """扫描数据目录里带 manifest.json 的导出包；台账独立保存。"""
    global PROJECTS
    if INDEX_FILE.is_file():
        raw = jload(INDEX_FILE, {'active': '', 'list': []})
        PROJECTS = {'active': raw.get('active', ''),
                    'list': [norm_project(x) for x in raw.get('list', []) if x.get('id')]}
    else:
        plist = []
        for d in sorted(MAT.iterdir()):
            if not d.is_dir() or not (d / 'manifest.json').is_file():
                continue
            legacy = d.name == LEGACY_SRC.name
            mf = jload(d / 'manifest.json', {})
            plist.append({
                'id': d.name,
                'name': '闲鱼 A 号（首批）' if legacy else d.name,
                'zip': 'busyfish-backup-77b9a212.zip' if legacy else '',
                'importedAt': (mf.get('createdAt') or '')[:19],
                'itemCount': len(mf.get('snapshots', [])),
                'legacy': legacy,
                'scanned': (d / 'image_scan2.json').is_file(),
            })
        PROJECTS = {'active': plist[0]['id'] if plist else '', 'list': plist}
    # 目录被手工挪走的项目不显示；active 失效就落回第一个
    PROJECTS['list'] = [p for p in PROJECTS.get('list', [])
                        if project_paths(p)['dir'].is_dir()]
    ids = [p['id'] for p in PROJECTS['list']]
    if PROJECTS.get('active') not in ids:
        PROJECTS['active'] = ids[0] if ids else ''
    jwrite(INDEX_FILE, PROJECTS)


def activate(pid: str) -> dict:
    """切换当前导出包：素材、扫描、状态、发布包四套路径一起换。"""
    global SRC, MEDIA, MANIFEST, SCAN, STATE_FILE, OUTROOT, ACTIVE, CATALOG, BYID
    p = next((x for x in PROJECTS['list'] if x['id'] == pid), None)
    if p is None:
        raise ValueError(f'项目不存在：{pid}')
    pp = project_paths(p)
    d = pp['dir']
    if not (d / 'manifest.json').is_file():
        raise ValueError(f'项目缺 manifest.json：{d}')
    SRC = d
    MEDIA = d / 'media'
    MANIFEST = d / 'manifest.json'
    SCAN = d / 'image_scan2.json'
    STATE_FILE = pp['state']
    OUTROOT = pp['outroot']
    ACTIVE = {**p, 'dir': str(d), 'state': str(STATE_FILE), 'outroot': str(OUTROOT)}
    if not STATE_FILE.exists() and LEGACY_STATE.exists() and p.get('legacy'):
        shutil.copy2(LEGACY_STATE, STATE_FILE)
    OUTROOT.mkdir(parents=True, exist_ok=True)
    PROJECTS['active'] = pid
    jwrite(INDEX_FILE, PROJECTS)
    CATALOG = build_catalog()
    BYID = {it['id']: it for it in CATALOG}
    get_state()
    log(f'切换到「{p["name"]}」：{len(CATALOG)} 件，素材 {d}')
    return ACTIVE


def blank_active() -> None:
    """全新电脑、数据目录为空时也要能启动，好让用户从第一步导入 ZIP。"""
    global SRC, MEDIA, MANIFEST, SCAN, STATE_FILE, OUTROOT, ACTIVE, CATALOG, BYID
    pp = project_paths({'id': '_未导入', 'legacy': False})
    SRC = pp['dir']
    MEDIA = SRC / 'media'
    MANIFEST = SRC / 'manifest.json'
    SCAN = SRC / 'image_scan2.json'
    STATE_FILE = pp['state']
    OUTROOT = pp['outroot']
    OUTROOT.mkdir(parents=True, exist_ok=True)
    ACTIVE = {'id': '', 'name': '（还没有项目）', 'dir': str(SRC),
              'state': str(STATE_FILE), 'outroot': str(OUTROOT), 'zip': '',
              'importedAt': '', 'itemCount': 0, 'legacy': False, 'scanned': False}
    CATALOG = []
    BYID = {}


def apply_config(raw: str) -> dict:
    """改数据目录并热切换（界面「数据放在哪」用）。"""
    cfg = load_config()
    cfg['data_dir'] = raw
    jwrite(CONFIG_FILE, cfg)
    rebind_paths()
    init_projects()
    if PROJECTS['list']:
        activate(PROJECTS['active'])
    else:
        blank_active()
    log(f'数据目录已切换：{DATA}')
    return {'dataDir': raw, 'data': str(DATA), 'inbox': str(INBOX),
            'packages': str(PACKAGES)}


def refresh_catalog() -> None:
    """图片扫描跑完后重建目录（状态文件里的手工改动会被保留）"""
    global CATALOG, BYID
    if not ACTIVE:
        return
    CATALOG = build_catalog()
    BYID = {it['id']: it for it in CATALOG}
    get_state()


# ------------------------------------------------------- 新包自动风险扫描
SCAN_PATS = {
    '平台水印': r'(京东|JD\.?COM|JDL|京尊达|天猫|淘宝|拼多多|抖音|快手|小红书|唯品会|苏宁|自营|京豆|京造|PLUS会员|旗舰店|官方旗舰)',
    '联系方式': r'(微信|威信|vx|V信|加我|QQ号|\b1[3-9]\d{9}\b|400-\d{3}-\d{4}|服务电话|联系电话)',
    '网址': r'(www\.|https?://|\.com|\.cn|\.asia|\.net)',
    '扫码引流': r'(扫码|扫一扫|二维码|关注|公众号)',
    '面单隐私': r'(订单号|运单号|收件人|收货人|京东快递|顺丰|中通|圆通|韵达|申通)',
}
TEXT_RISKS = [
    ('放弃', '医疗器械 / 药品功效', r'(医用|医疗器械|外科口罩|祛疤|疤痕凝胶|治疗|疗效|药用|抑菌|杀菌|消毒|痔疮|造口|敷料|矫正|保健液|苍耳子)'),
    ('放弃', '成人 / 低俗类目', r'(成人用品|润滑液|私密修护|壮阳|情趣|催情|私处)'),
    ('改后搬', '极限词 / 售假暗示', r'(最好|最低价|第一品牌|国家级|顶级|包治|根治|100%有效|正品保证|专柜|官方旗舰|原单|尾单|高仿|A货)'),
    ('改后搬', '站外引流', r'(加我|微信|威信|vx|V信|QQ|扫码|公众号|https?://|www\.)'),
    ('改后搬', '品牌全新品 / IP 投诉', r'(全新正品|专柜|旗舰店|官方授权)'),
]


def auto_risk(s: dict) -> dict:
    """新导入的包没有人工决策表，用正则先分档；结论只作排查线索，不是最终判据。"""
    text = f"{s.get('displayTitle', '')} {s.get('description', '')}"
    hits = []
    for lvl, why, pat in TEXT_RISKS:
        m = sorted(set(x.group(0) for x in re.finditer(pat, text, re.I)))
        if m:
            hits.append((lvl, why, m[:5]))
    lvl = ('放弃' if any(h[0] == '放弃' for h in hits)
           else '改后搬' if hits else '可搬')
    why = '；'.join(f'{h[1]}：{"、".join(h[2])}' for h in hits) or '未命中已知违规词，仍需人工复核'
    return {
        '文案判定': '自动扫描' if hits else '未改写',
        '文案说明': why + '；⚠ 文案仍是原文，跨号发布前必须重写标题与描述',
        '图片问题': '', '最终结论': lvl,
        '实拍图': 0, '平台截图': 0, '带二维码': 0,
    }


def scan_project_images(pdir: Path) -> list[dict]:
    """OCR + 二维码全扫，产出与 A 号同格式的 image_scan2.json"""
    import cv2
    import numpy as np
    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()
    except Exception as e:
        ocr = None
        log(f'OCR 引擎不可用（{e}），本次只做二维码检测')
    det = cv2.QRCodeDetector()
    try:
        bd = cv2.barcode.BarcodeDetector()   # 1D 条形码（商品包装/快递单），与二维码一并抹除
    except Exception:
        bd = None
    files = []
    md = pdir / 'media'
    if md.is_dir():
        for d in sorted(x for x in md.iterdir() if x.is_dir()):
            files += [(d.name, f) for f in sorted(d.iterdir()) if f.is_file()]
    IMPORT['total'] = len(files)
    IMPORT['done'] = 0
    res = []
    for item, f in files:
        name = f.name
        if f.suffix.lower() in ('.mp4', '.mov'):
            res.append({'item': item, 'file': name, 'media': 'video'})
        else:
            img = cv2.imdecode(np.fromfile(str(f), dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                res.append({'item': item, 'file': name, 'err': 'read_fail'})
            else:
                rec = {'item': item, 'file': name}
                try:
                    ok, pts = det.detect(img)
                    if pts is not None and len(pts) > 0:
                        rec['qr'] = True
                except Exception:
                    pass
                # 条形码：统一并入「机器码」标识，走与二维码相同的局部抹除
                if bd is not None:
                    try:
                        rb = bd.detect(img)
                        corners = rb[1] if (isinstance(rb, tuple) and len(rb) >= 2 and rb[0]) else None
                        if corners is not None and getattr(corners, 'size', 0):
                            rec['barcode'] = True
                            rec.setdefault('qr', True)
                    except Exception:
                        pass
                if ocr is not None:
                    try:
                        out, _ = ocr(str(f))
                        txt = ' '.join(x[1] for x in out) if out else ''
                        rec['len'] = len(txt)
                        hits = {}
                        for k, pat in SCAN_PATS.items():
                            m = sorted(set(x.group(0) for x in re.finditer(pat, txt, re.I)))
                            if m:
                                hits[k] = m[:6]
                        if hits:
                            rec['hits'] = hits
                            rec['head'] = txt[:160]
                    except Exception as e:
                        rec['ocr_err'] = str(e)[:80]
                res.append(rec)
        IMPORT['done'] += 1
    jwrite(pdir / 'image_scan2.json', res)
    return res


def _scan_worker(pdir: Path, pid: str) -> None:
    try:
        IMPORT.update(running=True, stage='正在扫描图片（OCR + 二维码）',
                      done=0, total=0, project=pid, error='')
        recs = scan_project_images(pdir)
        for p in PROJECTS['list']:
            if p['id'] == pid:
                p['scanned'] = True
                p['itemCount'] = len(jload(pdir / 'manifest.json', {}).get('snapshots', []))
        jwrite(INDEX_FILE, PROJECTS)
        if ACTIVE.get('id') == pid:
            refresh_catalog()
        IMPORT.update(running=False,
                      stage=f'完成：扫描 {len(recs)} 个媒体文件',
                      done=len(recs), total=len(recs),
                      finishedAt=dt.datetime.now().isoformat(timespec='seconds'))
        log(f'「{ACTIVE.get("name", pid)}」图片扫描完成，共 {len(recs)} 个媒体')
    except Exception as e:
        IMPORT.update(running=False, stage='扫描失败', error=str(e))
        log(f'图片扫描失败：{e}\n{traceback.format_exc()}')


def import_zip(zip_path: Path, name: str) -> dict:
    """从忙鱼导出的 ZIP 新建一个项目；只放行 manifest.json 和 media/ 下的文件。"""
    import zipfile
    if not zip_path.is_file():
        raise ValueError(f'找不到 ZIP：{zip_path}')
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith('/')]
        mf_name = next((n for n in names
                        if n.replace('\\', '/').split('/')[-1] == 'manifest.json'), None)
        if not mf_name:
            raise ValueError('ZIP 里没有 manifest.json，确认选的是忙鱼导出的包')
        mf = json.loads(z.read(mf_name).decode('utf-8-sig'))
        snaps = mf.get('snapshots', [])
        if not snaps:
            raise ValueError('manifest.json 里没有商品快照')
        base = mf_name.replace('\\', '/').rsplit('/', 1)[0] if '/' in mf_name.replace('\\', '/') else ''
        stamp = dt.datetime.now().strftime('%Y%m%d')
        clean = safe_name(name or '新号')
        target = MAT / f'导出包_{clean}_{stamp}'
        i = 2
        while target.exists():
            target = MAT / f'导出包_{clean}_{stamp}-{i}'
            i += 1
        target.mkdir(parents=True, exist_ok=True)
        (target / 'manifest.json').write_bytes(z.read(mf_name))
        for n in names:
            nn = n.replace('\\', '/')
            rel = nn[len(base) + 1:] if base and nn.startswith(base + '/') else nn
            rel = os.path.normpath(rel).replace('\\', '/')
            if rel != 'manifest.json' and not rel.startswith('media/'):
                continue
            if rel.startswith('..') or ':' in rel:
                continue
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(z.read(n))
    pid = target.name
    rec = {
        'id': pid, 'name': clean, 'zip': zip_path.name,
        'importedAt': dt.datetime.now().isoformat(timespec='seconds'),
        'itemCount': len(snaps), 'legacy': False, 'scanned': False,
    }
    PROJECTS['list'].append(rec)
    jwrite(INDEX_FILE, PROJECTS)
    activate(pid)
    log(f'导入新导出包「{clean}」：{len(snaps)} 件 ← {zip_path.name}')
    threading.Thread(target=_scan_worker, args=(target, pid), daemon=True).start()
    return {'ok': True, 'id': pid, 'name': clean, 'itemCount': len(snaps),
            'scanning': True, 'isLegacy': False}


def load_rewrite() -> dict:
    """A 号专用改写字典；新导入的包没有，必须人工重写文案"""
    if not is_legacy():
        return {}
    try:
        tree = ast.parse(REWRITE.read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'NEW' for t in node.targets
            ):
                return ast.literal_eval(node.value)
    except Exception as e:
        log(f'改写文案读取失败：{e}')
    return {}


def load_decision() -> dict:
    """A 号有人工决策表；其它号交给 auto_risk 自动分档"""
    out = {}
    if not is_legacy() or not DECISION.is_file():
        return out
    try:
        from openpyxl import load_workbook
        ws = load_workbook(DECISION, read_only=True, data_only=True).active
        heads = [c.value for c in ws[1]]
        idx = {h: i for i, h in enumerate(heads)}
        for row in ws.iter_rows(min_row=2, values_only=True):
            iid = str(row[idx['商品ID']] or '').strip()
            if not iid:
                continue
            out[iid] = {
                '文案判定': row[idx['文案判定']] or '',
                '文案说明': row[idx['文案说明']] or '',
                '图片问题': row[idx['图片问题']] or '',
                '最终结论': row[idx['最终结论']] or '',
                '实拍图': row[idx['实拍图']] or 0,
                '平台截图': row[idx['平台截图']] or 0,
                '带二维码': row[idx['带二维码']] or 0,
            }
    except Exception as e:
        log(f'决策表读取失败：{e}')
    return out


def load_scan() -> dict:
    out = {}
    for r in jload(SCAN, []):
        key = f"{r.get('item','')}/{r.get('file','')}"
        tags = list((r.get('hits') or {}).keys())
        if r.get('qr'):
            tags.append('二维码')
        if r.get('barcode'):
            tags.append('条形码')
        if tags:
            out[key] = tags
    return out


def media_files(iid: str) -> list[str]:
    d = MEDIA / iid
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.iterdir()
                  if f.is_file() and f.suffix.lower() in ('.webp', '.jpg', '.jpeg', '.png', '.mp4'))


def parse_price(text: str) -> tuple[float, str]:
    """返回 (价格, 提示)。闲鱼区间价必须定死一个数才能传给 CLI"""
    t = str(text or '').strip()
    m = re.findall(r'\d+(?:\.\d+)?', t)
    if not m:
        return 0.0, '价格为 0，必须手动填写'
    v = float(m[0])
    note = ''
    if len(m) > 1 or re.search(r'[-~～至]', t):
        note = f'原价是区间「{t}」，发布用 {v:g}，请确认'
    return v, note


def build_catalog() -> list[dict]:
    mf = jload(MANIFEST)
    rewrite = load_rewrite()
    decision = load_decision()
    scan = load_scan()
    items = []
    for s in mf.get('snapshots', []):
        iid = str(s.get('itemId', ''))
        files = media_files(iid)
        dec = decision.get(iid) or auto_risk(s)
        conclusion = str(dec.get('最终结论') or '未分类')
        price, pnote = parse_price((s.get('price') or {}).get('amountText', ''))
        pics = []
        for f in files:
            pics.append({'file': f, 'tags': scan.get(f'{iid}/{f}', []),
                         'kind': 'video' if f.lower().endswith('.mp4') else 'image'})
        items.append({
            'id': iid,
            'title': s.get('displayTitle', ''),
            'desc': s.get('description', ''),
            'price': price,
            'priceText': (s.get('price') or {}).get('amountText', ''),
            'priceNote': pnote,
            'category': (s.get('category') or {}).get('name', ''),
            'shipping': s.get('shippingText', '包邮'),
            'attrs': {a.get('name'): a.get('value') for a in s.get('attributes', [])},
            'url': s.get('sourceUrl', ''),
            'pics': pics,
            'conclusion': conclusion,
            'copyRisk': str(dec.get('文案判定') or ''),
            'copyNote': str(dec.get('文案说明') or ''),
            'picNote': str(dec.get('图片问题') or ''),
            'newTitle': (rewrite.get(iid) or ('', ''))[0],
            'newDesc': (rewrite.get(iid) or ('', ''))[1],
            'problemCount': sum(1 for p in pics if p['tags']),
        })
    return items


def action_for(tags: list[str], kind: str) -> str:
    """图片问题 → 处理动作，唯一规则来源（初始状态与一键清洗共用）"""
    if '面单隐私' in tags:
        return 'delete'
    if '二维码' in tags or '条形码' in tags:
        return 'erase_qr'
    if '平台水印' in tags and kind == 'image':
        return 'crop_bottom'
    return 'keep'


def default_state(items: list[dict]) -> dict:
    st = {'version': 1, 'updatedAt': dt.datetime.now().isoformat(timespec='seconds'), 'items': {}}
    fresh = not is_legacy()      # 新包默认一件都不纳入，避免原文案被一键发到另一个号
    for it in items:
        actions = {}
        for p in it['pics']:
            actions[p['file']] = action_for(p['tags'], p['kind'])
        st['items'][it['id']] = {
            'include': False if fresh else it['conclusion'] in ('可搬', '改后搬'),
            'title': it['newTitle'] or it['title'],
            'desc': it['newDesc'] or it['desc'],
            'price': it['price'],
            'delivery': (it['shipping'] or '无需邮寄').strip() or '无需邮寄',
            'actions': actions,
        }
    return st


def get_state() -> dict:
    saved = jload(STATE_FILE, None)
    if not saved or not saved.get('items'):
        st = default_state(CATALOG)
        jwrite(STATE_FILE, st)
        return st
    # 补齐新增商品 / 新图片
    changed = False
    for it in CATALOG:
        if it['id'] not in saved['items']:
            saved['items'][it['id']] = default_state([it])['items'][it['id']]
            changed = True
        row = saved['items'][it['id']]
        for p in it['pics']:
            if p['file'] not in row.get('actions', {}):
                row.setdefault('actions', {})[p['file']] = 'keep'
                changed = True
    if changed:
        jwrite(STATE_FILE, saved)
    return saved


# ------------------------------------------------------- 批量操作 / 自动改写
REWRITE_SKIP = ('放弃', '需重拍')
ACTION_LABEL = {'keep': '原样保留', 'erase_qr': '抹除二维码',
                'crop_bottom': '裁掉底部', 'crop_top': '裁掉顶部',
                'crop_right': '裁掉右侧', 'delete': '整张删除'}


def _tiny_png() -> bytes:
    """2×2 空白 PNG：预览结果被判定「整张弃用」时用，避免浏览器控制台报 404。"""
    import struct
    import zlib
    raw = (b'\x00' + b'\xff\xff\xff\xff' * 2) * 2

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


PLACEHOLDER_PNG = _tiny_png()


def auto_rewrite(body: dict) -> dict:
    """按当前范围批量重写标题与描述。

    护栏：默认跳过「放弃 / 需重拍」档与已改写过的商品；
    引擎自身还会拒写医用 / 成人 / 功效类高危品，不靠删词洗白。
    只改状态文件里要发布的文案，原标题描述永远保留在 manifest 里，可一键还原。
    """
    if rewrite_engine is None:
        raise ValueError('自动改写引擎不可用：app/rewrite_engine.py 不在工具包里')
    if not CATALOG:
        raise ValueError('当前项目没有商品，先在第一步导入导出包')
    ids = body.get('ids')
    idset = {str(x) for x in ids} if isinstance(ids, list) and ids else None
    overwrite = bool(body.get('overwrite'))
    skip_risk = bool(body.get('skipRisk', True))
    st = get_state()
    targets, skip_risk_n, skip_done_n = [], 0, 0
    for it in CATALOG:
        if idset is not None and it['id'] not in idset:
            continue
        if skip_risk and it['conclusion'] in REWRITE_SKIP:
            skip_risk_n += 1
            continue
        row = st['items'].get(it['id'], {})
        cur = str(row.get('title') or '').strip()
        if cur and cur != str(it['title']).strip() and not overwrite:
            skip_done_n += 1
            continue
        targets.append(it)
    if not targets:
        return {'ok': True, 'changed': 0, 'skipped': skip_done_n,
                'skippedRisk': skip_risk_n, 'blocked': 0, 'blockedIds': [],
                'samples': [], 'message': '没有需要重写的商品（已改写过，或都属高风险档）'}

    rows = [{'id': it['id'], 'title': it['title'], 'desc': it['desc'],
             'category': it['category'], 'price': it['priceText']} for it in targets]
    res = rewrite_engine.rewrite_many(rows, seed=str(ACTIVE.get('id') or ''))
    changed, blocked, blocked_ids, samples = 0, 0, [], []
    now = dt.datetime.now().isoformat(timespec='seconds')
    for r in res:
        it = BYID.get(r['id'])
        if it is None:
            continue
        meta = r['meta']
        if not r['title']:
            blocked += 1
            blocked_ids.append({'id': r['id'], 'title': it['title'],
                                'why': (meta.get('warnings') or ['高危商品'])[0]})
            continue
        row = st['items'][r['id']]
        row['title'] = r['title']
        row['desc'] = r['desc']
        row['rewrite'] = {
            'at': now, 'engine': '本地规则改写 v1',
            'structure': meta['structure'],
            'titleSim': meta['titleSim'], 'descSim': meta['descSim'],
            'origTitle': it['title'], 'origDesc': it['desc'],
            'warnings': meta['warnings'], 'removed': meta['removed'][:8],
        }
        changed += 1
        if len(samples) < 3:
            samples.append({'id': r['id'], 'old': it['title'],
                            'new': r['title'], 'structure': meta['structure'],
                            'titleSim': meta['titleSim'], 'descSim': meta['descSim']})
    st['updatedAt'] = now
    jwrite(STATE_FILE, st)
    log(f'自动重写文案：改写 {changed} 件，拒绝高危 {blocked} 件，'
        f'跳过已改写 {skip_done_n} 件 / 风险档 {skip_risk_n} 件')
    return {'ok': True, 'changed': changed, 'skipped': skip_done_n,
            'skippedRisk': skip_risk_n, 'blocked': blocked,
            'blockedIds': blocked_ids, 'samples': samples}


# ---- 闲鱼平台规则体检：按平台社区公约/商品发布规范逐件扫描 ----
RULE_DANGER, RULE_WARN = '高危', '提醒'

# (规则名, 级别, 提醒文案, 命中正则)
_RULE_TESTS = [
    ('站外引流', RULE_DANGER,
     '闲鱼禁止留联系方式或引导站外交易，图片里留码也违规（清洗步骤会抹码）',
     re.compile(r'微信|威信|微❤|加\s*v|vx|vx号|wx号|扣扣|qq|qq\.com|1[3-9]\d{9}'
                r'|抖音|快手号|看主页|私聊我|闲鱼以外', re.I)),
    ('绝对化用语', RULE_DANGER,
     '广告法禁用的绝对化用语，会被判违规宣传甚至下架扣分',
     re.compile(r'最好|最强|最佳|最优|最低价|全网最低|全网最|第一品牌|销量第一|行业第一'
                r'|顶级|极品|绝世|绝对|国家级|世界级|史上|万能|包治|根治|100%|百分百')),
    ('售假风险', RULE_DANGER,
     '高仿/复刻/原单类是售假红线词，品牌方投诉会直接下架、扣分封号',
     re.compile(r'高仿|精仿|a\s*货|复刻|原单|原厂尾单|尾单|跟单|外贸单|1\s*[:：]\s*1'
                r'|海关罚没|代工厂|拆包|份料', re.I)),
    ('违禁限售', RULE_DANGER,
     '这些属禁售/限售类目，发了就删，多次会封号',
     re.compile(r'发票|代写|代考|代刷|刷单|刷信誉|刷粉|涨粉|香烟|烟草|电子烟'
                r'|处方|药品|兽药|减肥药|保健品|种子|幼苗|野生动物|象牙|玳瑁|砗磲'
                r'|管制刀具|弩|电击器|监控屏蔽|身份证|银行卡|学历证|毕业证'
                r'|牌照|车牌|账号出租|账号出售|游戏外挂|外挂|代练|扩列|一夜情', re.I)),
    ('功效宣称', RULE_WARN,
     '医疗/功效宣称是重点治理对象，普通闲置别碰这类词，确实有资质再如实描述',
     re.compile(r'医用|医疗器械|灭菌|消炎|杀菌|抑菌|美白|祛斑|祛疤|脱毛|减肥|瘦身'
                r'|丰胸|壮阳|生发|防脱|助眠|降血压|降血糖')),
]

# 常见品牌词：不拦，只提醒（自用闲置正品可以带，但要防品牌投诉）
_BRAND_RE = re.compile(
    r'耐克|阿迪|nike|adidas|puma|new\s*balance|新百伦|匡威|converse|vans|北面'
    r'|the\s*north\s*face|始祖鸟|arc.teryx|优衣库|uniqlo|gucci|古驰|路易威登|lv'
    r'|香奈儿|chanel|迪奥|dior|普拉达|prada|爱马仕|hermes|苹果|iphone|ipad|macbook'
    r'|华为|小米|oppo|vivo|荣耀|戴森|dyson|大疆|dji|索尼|sony|佳能|尼康|乐高|lego'
    r'|费雪|好孩子|babycare|花王|帮宝适|好奇|sk-?ii|雅诗兰黛|兰蔻|欧莱雅|科颜氏'
    r'|无印|muji|三只松鼠|百草味|良品铺子|南极人|恒源祥|北极绒|富光|苏泊尔|美的'
    r'|格力|海尔|九阳|小熊|乐扣|膳魔师|象印|虎牌', re.I)


def rule_check_item(it: dict, row: dict, published: set) -> list[dict]:
    """对单件商品做平台规则体检，返回问题列表（无问题返回空）。"""
    issues: list[dict] = []
    # 行值即使是空串也要采纳（用户可能清空了描述），不能回退到原文
    title = str(row.get('title', it.get('newTitle') or it.get('title')) or '').strip()
    desc = str(row.get('desc', it.get('newDesc') or it.get('desc')) or '').strip()
    text = title + '\n' + desc
    for name, level, advice, pat in _RULE_TESTS:
        m = pat.search(text)
        if m:
            issues.append({'rule': name, 'level': level,
                           'msg': f'命中「{m.group(0)[:14]}」——{advice}'})
    if _BRAND_RE.search(title):
        issues.append({'rule': '品牌词', 'level': RULE_WARN,
                       'msg': '标题含品牌词：未授权发布有被品牌投诉风险；'
                              '自用闲置正品可如实写，别借品牌夸宣传'})
    if not desc:
        issues.append({'rule': '描述为空', 'level': RULE_DANGER,
                       'msg': '描述是空的——平台要求描述真实完整，空描述影响搜索收录、易被判违规'})
    n = len(title)
    if n > 30:
        issues.append({'rule': '标题超长', 'level': RULE_WARN,
                       'msg': f'标题 {n} 字（平台标题约 30 字上限），超出部分会被截断，堆砌关键词易判营销号'})
    elif 0 < n < 6:
        issues.append({'rule': '标题太短', 'level': RULE_WARN,
                       'msg': '标题不到 6 个字，搜不到也说不清是什么，建议补品类+成色+规格'})
    try:
        price = round(float(row.get('price', it.get('price'))), 2)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        issues.append({'rule': '价格异常', 'level': RULE_WARN,
                       'msg': '价格为 0 或没填——0 元/超低价会被判低价引流，按实际二手价填'})
    elif price < 3:
        issues.append({'rule': '价格异常', 'level': RULE_WARN,
                       'msg': '价格低于 3 元，明显低价可能被判引流，确认没少打一个零'})
    if title == str(it.get('title') or '').strip() and desc == str(it.get('desc') or '').strip():
        issues.append({'rule': '重复铺货', 'level': RULE_WARN,
                       'msg': '标题描述还是原号原文——同图同文多号/多次发布会被判重复铺货限流，请先改写'})
    actions = row.get('actions') or {}
    unhandled = sum(1 for p in (it.get('pics') or [])
                    if p.get('kind') == 'image' and p.get('tags')
                    and (actions.get(p.get('file')) or 'keep') == 'keep')
    if unhandled:
        issues.append({'rule': '图片未清洗', 'level': RULE_WARN,
                       'msg': f'{unhandled} 张图扫出过二维码/水印/联系方式但仍原样保留——'
                              '图里留码属站外引流违规，生成发布包前先安排处理'})
    if str(it.get('id')) in published:
        issues.append({'rule': '已发布过', 'level': RULE_WARN,
                       'msg': '这件已发布成功过；同内容再发一次就是重复铺货，确要重发先删旧帖'})
    return issues


def rule_check() -> dict:
    """全量体检：返回统计 + 有问题商品的明细（含未纳入发布包的，供参考）。"""
    if not CATALOG:
        raise ValueError('当前项目没有商品，先在第一步导入导出包')
    st = get_state()
    published = set()
    if OUTROOT:
        published = {str(r.get('id')) for r in (jload(OUTROOT / '发布记录.json', []) or [])}
    items, danger_n, warn_n = [], 0, 0
    for i, it in enumerate(CATALOG, 1):
        row = st['items'].get(it['id'], {})
        iss = rule_check_item(it, row, published)
        if not iss:
            continue
        has_danger = any(x['level'] == RULE_DANGER for x in iss)
        if has_danger:
            danger_n += 1
        else:
            warn_n += 1
        items.append({'id': it['id'], 'row': i,
                      'title': str(row.get('title', it.get('newTitle')) or it.get('title') or ''),
                      'include': bool(row.get('include')),
                      'danger': has_danger, 'issues': iss})
    clean = len(CATALOG) - len(items)
    log(f'平台规则体检：高危 {danger_n} 件 / 提醒 {warn_n} 件 / 无问题 {clean} 件')
    return {'ok': True, 'total': len(CATALOG), 'danger': danger_n,
            'warn': warn_n, 'clean': clean, 'items': items}


def smart_clean() -> dict:
    """重新排布全部图片的处理动作，并返回覆盖统计。

    初始状态已经按同一套规则排好了动作，所以「变更数」常常是 0；
    这里返回的是本次处理的图片总数与动作分布，避免界面误报「什么都没做」。
    真正落地图是在「生成发布包」时，原图永远不动。
    """
    st = get_state()
    stat = {'total': 0, 'keep': 0, 'erase_qr': 0, 'crop_bottom': 0,
            'delete': 0, 'changed': 0, 'touched': 0, 'items': 0}
    for it in CATALOG:
        row = st['items'].setdefault(it['id'], {})
        acts = row.setdefault('actions', {})
        hit = False
        for pic in it['pics']:
            act = action_for(pic['tags'], pic['kind'])
            if acts.get(pic['file']) != act:
                stat['changed'] += 1
            acts[pic['file']] = act
            stat[act] = stat.get(act, 0) + 1
            stat['total'] += 1
            if act != 'keep':
                stat['touched'] += 1
                hit = True
        if hit:
            stat['items'] += 1
    st['updatedAt'] = dt.datetime.now().isoformat(timespec='seconds')
    jwrite(STATE_FILE, st)
    log('智能清洗：覆盖 %d 张图（抹码 %d / 删除 %d / 裁边 %d / 保留 %d），'
        '涉及 %d 件商品；动作在生成发布包时生效'
        % (stat['total'], stat['erase_qr'], stat['delete'],
           stat['crop_bottom'], stat['keep'], stat['items']))
    return {'ok': True, **stat, 'labels': ACTION_LABEL}


# ------------------------------------------------------------------ 图片处理

def _imread(p: Path):
    import cv2
    import numpy as np
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def _imwrite(p: Path, img, ext: str = '.png') -> bool:
    import cv2
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    p.write_bytes(buf.tobytes())
    return True


def _copy(src: Path, target: Path) -> None:
    """copy2 在中文长路径/目录刚被重建时会报 WinError 3，降级为字节写入"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, target)
    except OSError:
        target.write_bytes(src.read_bytes())


def process_image(src: Path, action: str, dst: Path) -> str | None:
    """按动作产出一张图到 dst 目录，返回输出文件名；delete 返回 None"""
    name = src.name
    if action == 'delete':
        return None
    if not src.is_file():
        log(f'源文件不存在，已跳过：{src}')
        return None
    dst.mkdir(parents=True, exist_ok=True)
    if action == 'keep':
        _copy(src, dst / name)
        return name
    if src.suffix.lower() == '.mp4':
        return None                       # 个人店发布链路不支持视频
    img = _imread(src)
    if img is None:
        log(f'图片读不出来，已跳过：{src}')
        return None
    out = src.stem + '.png'
    try:
        import cv2
        import numpy as np
        if action == 'erase_qr':
            box = _code_box(img)            # 同时覆盖二维码 + 条形码
            if box is not None:
                x0, y0, x1, y1 = box
                mask = np.zeros(img.shape[:2], np.uint8)
                mask[y0:y1, x0:x1] = 255
                img = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)
                # 再用机器码上方条带的中位色覆盖一遍：光 inpaint 会留下定位角轮廓，
                # 平台的识图仍能认出来，纯色覆盖才能彻底抹掉。
                sy0, sy1 = max(0, y0 - 28), max(1, y0 - 4)
                strip = img[sy0:sy1, x0:x1]
                color = (np.median(strip.reshape(-1, 3), axis=0)
                         if strip.size else np.array([255, 255, 255]))
                img[y0:y1, x0:x1] = color
                if _code_box(img) is not None:
                    log(f'修补后仍能识别出机器码，整张删除：{src.parent.name}/{src.name}')
                    return None
                log(f'抹除机器码：{src.parent.name}/{src.name} 区域 {x0},{y0}-{x1},{y1}')
            else:
                log(f'未定位到机器码，改用底部裁剪：{src.parent.name}/{src.name}')
                img = img[:int(img.shape[0] * 0.88), :]
        elif action == 'crop_bottom':
            img = img[:int(img.shape[0] * 0.88), :]
        elif action == 'crop_top':
            img = img[int(img.shape[0] * 0.12):, :]
        elif action == 'crop_right':
            img = img[:, :int(img.shape[1] * 0.88)]
        else:
            _copy(src, dst / name)
            return name
    except Exception as e:
        log(f'处理失败，退回原图：{src.name} {e}')
        _copy(src, dst / name)
        return name
    if _imwrite(dst / out, img):
        return out
    _copy(src, dst / name)
    return name


def _code_box(img):
    """返回图中第一个机器码（二维码或条形码）的矩形；占整图过大视为商品/包装误检，返回 None。
    只定位、不擦除——品牌商标（logo）不在识别范围内，永不触碰。"""
    import cv2
    import numpy as np
    # 二维码
    try:
        det = cv2.QRCodeDetector()
        ok, pts = det.detect(img)
        if ok and pts is not None and len(pts):
            q = pts.astype(np.int32).reshape(-1, 2)
            x0, y0 = max(0, int(q[:, 0].min()) - 6), max(0, int(q[:, 1].min()) - 6)
            x1, y1 = min(img.shape[1], int(q[:, 0].max()) + 6), min(img.shape[0], int(q[:, 1].max()) + 6)
            ratio = (x1 - x0) * (y1 - y0) / max(1, img.shape[0] * img.shape[1])
            if 0 < ratio <= 0.25:
                return (x0, y0, x1, y1)
    except Exception:
        pass
    # 条形码（1D）：商品包装/快递单上的机器码，与二维码一并抹除
    try:
        bd = cv2.barcode.BarcodeDetector()
        rb = bd.detect(img)
        corners = rb[1] if (isinstance(rb, tuple) and len(rb) >= 2 and rb[0]) else None
        if corners is not None and getattr(corners, 'size', 0):
            c = corners.reshape(-1, 2).astype(np.int32)
            x0, y0 = max(0, int(c[:, 0].min()) - 6), max(0, int(c[:, 1].min()) - 6)
            x1, y1 = min(img.shape[1], int(c[:, 0].max()) + 6), min(img.shape[0], int(c[:, 1].max()) + 6)
            ratio = (x1 - x0) * (y1 - y0) / max(1, img.shape[0] * img.shape[1])
            if 0 < ratio <= 0.30:
                return (x0, y0, x1, y1)
    except Exception:
        pass
    return None


def scrub_small_code(path: Path) -> str:
    """发布前最后一道：反复涂抹输出图里残留的小二维码 / 条形码。
    返回 clean（可发布）/ dirty（涂不净，别发）/ skip（读不了，保守保留）。"""
    try:
        import cv2
        import numpy as np
        img = _imread(path)
        if img is None:
            return 'skip'
        changed = False
        for _ in range(4):
            box = _code_box(img)
            if box is None:
                break
            x0, y0, x1, y1 = box
            strip = img[max(0, y0 - 20):max(1, y0 - 3), x0:x1]
            color = (np.median(strip.reshape(-1, 3), axis=0)
                     if strip.size else np.array([255, 255, 255]))
            mask = np.zeros(img.shape[:2], np.uint8)
            mask[y0:y1, x0:x1] = 255
            img = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
            img[y0:y1, x0:x1] = color
            changed = True
        if changed:
            ext = path.suffix if path.suffix.lower() in ('.png', '.webp', '.jpg', '.jpeg') else '.png'
            ok2, buf = cv2.imencode(ext, img)
            if not ok2:
                return 'dirty'
            path.write_bytes(buf.tobytes())
            log(f'补抹残留机器码：{path.parent.name[:16]}/{path.name}')
        check = _imread(path)
        return 'dirty' if (check is not None and _code_box(check) is not None) else 'clean'
    except Exception as e:
        log(f'机器码复核失败（保留原图）：{path.name} {e}')
        return 'skip'


def safe_name(title: str) -> str:
    # Windows 目录名不能以空格或点结尾，截断后必须再 strip 一次
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '', title or '')
    s = re.sub(r'\s+', ' ', s).strip()
    s = s[:24].strip().rstrip('. ')
    return s or '未命名'


def build_package() -> dict:
    """互斥：防止连点两次时第二次把第一次正在写的目录删掉"""
    with BUILD_LOCK:
        return _build_package_impl()


def _build_package_impl() -> dict:
    st = get_state()
    OUTROOT.mkdir(parents=True, exist_ok=True)
    # 不做任何删除：同名文件直接覆盖，旧文件留在目录里但不进 images 列表，
    # 发布脚本只认 商品信息.json，所以残留不会影响上架。
    # （批量删除会被系统的安全钩子拦截，也会让正在预览的界面断连）
    made, warnings = [], []
    for it in CATALOG:
        row = st['items'].get(it['id'], {})
        if not row.get('include'):
            continue
        title = (row.get('title') or '').strip()
        desc = (row.get('desc') or '').strip()
        if not title or not desc:
            warnings.append(f"{it['id']} 标题或描述为空，已跳过")
            continue
        price = row.get('price')
        try:
            price = float(price)
        except Exception:
            warnings.append(f"{it['id']} 价格不是数字，已跳过")
            continue
        folder = OUTROOT / f"{it['id']}_{safe_name(title)}"
        folder.mkdir(parents=True, exist_ok=True)
        images = []
        for p in it['pics']:
            if p['kind'] == 'video':
                continue
            act = row.get('actions', {}).get(p['file'], 'keep')
            out = process_image(MEDIA / it['id'] / p['file'], act, folder)
            if out:
                images.append(out)
        if not images:
            # 兜底：至少留一张，优先挑不带二维码、不带面单的
            pics = [p for p in it['pics'] if p['kind'] == 'image']
            clean = [p for p in pics if not ({'二维码', '面单隐私'} & set(p['tags']))]
            first = (clean or pics or [None])[0]
            if first:
                out = process_image(MEDIA / it['id'] / first['file'], 'keep', folder)
                images = [out] if out else []
                warnings.append(f"{it['id']} 图片被全部删掉，已强制保留一张"
                                + ('' if clean else '（该图仍带二维码，请手动重拍）'))
        if not images:
            warnings.append(f"{it['id']} 没有可用图片，已跳过")
            continue
        images = images[:9]
        cleaned = []
        for fn in images:
            r = scrub_small_code(folder / fn)
            if r == 'dirty':
                log(f'二维码反复涂不净，该图不发布：{it["id"]}/{fn}')
                warnings.append(f"{it['id']} 的 {fn} 仍带二维码，已排除，请手动重拍")
            else:
                cleaned.append(fn)
        images = cleaned
        if not images:
            warnings.append(f"{it['id']} 所有图都被二维码复核拦下，已跳过")
            continue
        info = {
            'item_id': it['id'], 'title': title, 'desc': desc,
            'price': price, 'delivery': row.get('delivery') or '无需邮寄',
            'post_price': 0, 'category': it['category'],
            'images': images, 'source_url': it['url'],
            'note': '；'.join(x for x in [it['priceNote'], it['copyNote']] if x),
        }
        info['hint'] = '本文件夹以上面的 images 列表为准；其它文件是历次生成的残留，不要用于上架。'
        jwrite(folder / '商品信息.json', info)
        # 把本次不用的旧文件挪到归档区（移动，不是删除）
        keep = set(images) | {'商品信息.json'}
        archive = OUTROOT / '_未使用' / folder.name
        for left in folder.iterdir():
            if left.is_file() and left.name not in keep:
                try:
                    archive.mkdir(parents=True, exist_ok=True)
                    os.replace(left, archive / left.name)
                except OSError:
                    pass
        made.append({'id': it['id'], 'title': title, 'price': price,
                     'images': len(images), 'folder': folder.name})

    # 清单 CSV
    csv_path = OUTROOT / '发布清单.csv'
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['序号', '商品ID', '标题', '价格', '图数', '文件夹'])
        for i, m in enumerate(made, 1):
            w.writerow([i, m['id'], m['title'], m['price'], m['images'], m['folder']])

    # 命令清单（可读）
    lines = ['# goofish-cli 发布命令清单（每行一件，务必先核对 --help）',
             f'# 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}  共 {len(made)} 件',
             '# 件数不限，发多少自己定（建议间隔 ≥60 秒，先演练再真发）；清单见发布清单.csv', '']
    for m in made:
        folder = OUTROOT / m['folder']
        info = jload(folder / '商品信息.json')
        imgs = ' '.join(f'"{x}"' for x in info['images'])
        desc1 = info['desc'].replace('\n', '\\n').replace('"', "'")
        lines.append(f'cd "{folder}"')
        lines.append(f'goofish item publish "{info["title"]}" "{desc1}" {imgs} '
                     f'{info["price"]:g} --delivery {info["delivery"]} --format json')
        lines.append('')
    (OUTROOT / '发布命令清单.txt').write_text('\n'.join(lines), encoding='utf-8')

    # 独立批量脚本
    # 脚本放在发布包目录里，路径用 __file__ 推导，整个文件夹搬到别的电脑也能跑
    (OUTROOT / 'publish_all.py').write_text(PUBLISH_SCRIPT, encoding='utf-8')

    jwrite(OUTROOT / '_manifest.json',
           {'createdAt': dt.datetime.now().isoformat(timespec='seconds'),
            'count': len(made), 'items': made, 'warnings': warnings,
            'outroot': str(OUTROOT)})
    log(f'发布包生成完成：{len(made)} 件 → {OUTROOT}')
    return {'count': len(made), 'outroot': str(OUTROOT), 'warnings': warnings,
            'items': made}


PUBLISH_SCRIPT = r'''# -*- coding: utf-8 -*-
"""按限速逐件发布（默认演练，加 --live 才真发）。

用法：
  python publish_all.py                 # 演练，只打印命令，1 秒一件
  python publish_all.py --live           # 真实发布，间隔 60 秒；件数由你自己定
  python publish_all.py --live --limit 15   # 也可以显式限定本次最多 15 件
"""
import argparse, datetime as dt, json, os, pathlib, shutil, subprocess, sys, time

OUTROOT = pathlib.Path(__file__).resolve().parent


def goofish_path():
    """找到 goofish 可执行文件（本脚本独立运行也能定位，不依赖 PATH）。"""
    on_path = shutil.which('goofish')
    if on_path:
        return on_path
    base = pathlib.Path(sys.executable).parent
    cand = (base / 'goofish.exe') if os.name == 'nt' else (base / 'goofish')
    return str(cand) if cand.exists() else 'goofish'


def _ensure_node():
    """goofish 签名依赖 Node。PATH 里没有时 execjs 回退到 JScript 会报
    「SyntaxError: 语法错误」，所以先找到 Node 并注入 PATH。"""
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if shutil.which('node') or os.environ.get('EXECJS_RUNTIME') == 'Node':
        return
    home = pathlib.Path.home()
    cands = [OUTROOT.parent / 'runtime' / 'node', OUTROOT.parent / 'runtime',
             home / '.workbuddy/binaries/node/versions']
    cands += [pathlib.Path(x) for x in ('C:/Program Files/nodejs', 'C:/Program Files (x86)/nodejs')]
    for d in cands:
        for nd in ([d] if d.name == 'nodejs' or d.name == 'runtime' or d.name == 'node'
                   else sorted(d.glob('*/'))):
            if nd.is_dir() and (nd / ('node.exe' if os.name == 'nt' else 'node')).is_file():
                os.environ['PATH'] = str(nd) + os.pathsep + os.environ.get('PATH', '')
                os.environ['EXECJS_RUNTIME'] = 'Node'
                print(f'Node 已就绪：{nd}')
                return
    print('警告：没找到 Node.js，发布会因「签名语法错误」失败；请安装 Node 或把 node.exe 放进 runtime\\')


_ensure_node()

ap = argparse.ArgumentParser()
ap.add_argument('--live', action='store_true', help='真实发布（默认只演练）')
ap.add_argument('--limit', type=int, default=0, help='本次最多发几件，0 = 全部（默认 0）')
ap.add_argument('--interval', type=float, default=60, help='真实发布间隔秒数，默认 60')
ap.add_argument('--only', default='', help='只发指定商品ID，逗号分隔')
a = ap.parse_args()

# 只按本次生成的清单发布，不遍历目录（避免历史残留目录被重复发）
mf = json.loads((OUTROOT / '_manifest.json').read_text(encoding='utf-8'))
entries = [(OUTROOT / it['folder'], it) for it in mf['items']]
entries = [(f, m) for f, m in entries if (f / '商品信息.json').exists()]
if a.only:
    want = set(a.only.split(','))
    entries = [(f, m) for f, m in entries if m['item_id'] in want]
if a.limit and a.limit > 0:
    entries = entries[:a.limit]
print(f'{"真实发布" if a.live else "演练"}：{len(entries)} 件，间隔 '
      f'{a.interval if a.live else 1} 秒，目录 {OUTROOT}')
if a.live:
    print('5 秒后开始，Ctrl+C 可中断'); time.sleep(5)

ok = fail = 0
RECPATH = OUTROOT / '发布记录.json'
done_ids = set()
if RECPATH.exists() and a.live:
    try:
        done_ids = {r.get('id') for r in json.loads(RECPATH.read_text(encoding='utf-8'))}
    except Exception:
        done_ids = set()
    if done_ids:
        before = len(entries)
        entries = [(f, m) for f, m in entries if m.get('id') not in done_ids]
        if len(entries) < before:
            print(f'跳过 {before - len(entries)} 件已发布过的商品（发布记录.json），本次发剩余 {len(entries)} 件')
if not entries:
    print('清单里的商品都已发布过，无事可做。要整套重发就删掉 发布记录.json')
    raise SystemExit(0)

def _record(meta):
    try:
        recs = json.loads(RECPATH.read_text(encoding='utf-8')) if RECPATH.exists() else []
    except Exception:
        recs = []
    recs.append({'id': meta.get('id'), 'title': (meta.get('title') or '')[:40],
                 'time': dt.datetime.now().isoformat(timespec='seconds')})
    RECPATH.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding='utf-8')

for i, (folder, meta) in enumerate(entries, 1):
    info = json.loads((folder / '商品信息.json').read_text(encoding='utf-8'))
    cmd = [goofish_path(), 'item', 'publish', info['title'], info['desc'],
           *info['images'], f"{float(info['price']):g}",
           '--delivery', info.get('delivery') or '无需邮寄', '--format', 'json']
    print(f'[{i}/{len(entries)}] {info["title"][:34]}  ¥{info["price"]:g}')
    if not a.live:
        print('   演练：' + ' '.join(f'"{x}"' if (' ' in x or chr(10) in x) else x
                                   for x in cmd[:3]) + ' ...')
        time.sleep(1); ok += 1; continue
    try:
        r = subprocess.run(cmd, cwd=folder, capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=180)
        if r.returncode == 0:
            print('   OK ' + r.stdout.strip()[:180]); ok += 1; _record(meta)
        else:
            print('   FAIL ' + (r.stderr or r.stdout).strip()[:240]); fail += 1
            print('   已停下，先查原因再继续（submission_unknown 不要自动重发）')
            break
    except Exception as e:
        print('   ERROR ' + str(e)[:200]); fail += 1; break
    if i < len(entries):
        time.sleep(a.interval)
print(f'完成：成功 {ok}，失败 {fail}。发布后请用 goofish item list 回读核对。')
'''


# ------------------------------------------------------------------ 发布队列

def _find_node_dir() -> str:
    """找 node.exe 所在目录。goofish 签名依赖 Node——PATH 里没有时 execjs 会回退到
    Windows JScript，ES6 语法的签名 JS 直接报「SyntaxError: 语法错误」（真实踩过的坑）。"""
    if shutil.which('node'):
        return os.path.dirname(shutil.which('node'))
    cands = [
        BASE / 'runtime' / 'node',
        BASE / 'runtime',
        Path(sys.executable).parent / 'node',
    ]
    cands += sorted(Path.home().glob('.workbuddy/binaries/node/versions/*/'))
    cands += [Path(x) for x in ('C:/Program Files/nodejs', 'C:/Program Files (x86)/nodejs')]
    for d in cands:
        try:
            exe = d / ('node.exe' if os.name == 'nt' else 'node')
            if exe.is_file():
                return str(d)
        except Exception:
            pass
    return ''


def _ensure_node() -> str:
    """把 Node 目录注入本进程 PATH（子进程会继承），并强制 execjs 走 Node。"""
    # 强制 goofish/pip 等子进程的 stdout/stderr 用 UTF-8：
    # 中文 Windows 管道下默认 GBK，工作台按 UTF-8 读会得到一串乱码（真实踩过）。
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if os.environ.get('EXECJS_RUNTIME') == 'Node':
        return 'already'
    d = _find_node_dir()
    if d:
        os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
        os.environ['EXECJS_RUNTIME'] = 'Node'
        log(f'Node 已就绪：{d}')
    else:
        log('警告：本机没找到 Node.js，goofish 发布签名会失败；'
            '请安装 Node 或把 node.exe 放进工具包 runtime\\ 目录')
    return d


_ensure_node()


def goofish_path() -> str:
    """goofish 可执行文件完整路径（跨平台）。优先 PATH，否则查本服务 python 同目录的 Scripts/bin。"""
    on_path = shutil.which('goofish')
    if on_path:
        return on_path
    base = Path(sys.executable).parent
    cand = (base / 'goofish.exe') if os.name == 'nt' else (base / 'goofish')
    return str(cand) if cand.exists() else ''


def goofish_status() -> dict:
    exe = goofish_path()
    node_dir = _find_node_dir()
    hint = '' if exe else '本机未找到 goofish 命令；点下面的「一键安装」即可，无需手敲命令'
    if exe and not node_dir:
        hint = '⚠ 未找到 Node.js：发布会因「签名语法错误」失败。请安装 Node.js，或把 node.exe 放进工具包 runtime\\ 文件夹'
    return {'installed': bool(exe), 'exe': exe, 'node': bool(node_dir), 'hint': hint}


# ---- goofish-cli 一键安装 / 登录（页面内完成，无需手敲命令）----
CLI = {
    'install': {'running': False, 'stage': 'idle', 'log': [], 'ok': False},
    'auth': {'running': False, 'log': [], 'qr': '', 'proc': None},
}


def _cli_public() -> dict:
    inst = {k: v for k, v in CLI['install'].items() if k != 'thread'}
    auth = {k: v for k, v in CLI['auth'].items() if k not in ('thread', 'proc')}
    return {'install': inst, 'auth': auth, 'cli': goofish_status()}


def _cli_install_thread() -> None:
    st = CLI['install']
    st['running'] = True
    st['stage'] = 'installing'
    st['log'] = ['▶ 开始安装 goofish-cli（使用本服务所用的 Python）…']
    st['ok'] = False
    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', 'goofish-cli'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            st['log'].append(line.rstrip('\n'))
            if len(st['log']) > 500:
                st['log'].pop(0)
        proc.wait()
        if proc.returncode != 0:
            st['stage'] = 'failed'
            st['log'].append(f'✗ pip 退出码 {proc.returncode}')
        else:
            exe = goofish_path()
            if exe:
                st['ok'] = True
                st['stage'] = 'done'
                st['log'].append(f'✓ 安装完成：{exe}')
            else:
                st['stage'] = 'notfound'
                st['log'].append('✗ 安装报告成功，但找不到 goofish 可执行文件')
    except Exception as e:
        st['stage'] = 'failed'
        st['log'].append('异常：' + traceback.format_exc()[-500:])
    finally:
        st['running'] = False


def cli_install_start() -> dict:
    st = CLI['install']
    if st['running']:
        return {'ok': False, 'reason': 'running'}
    if goofish_path():
        return {'ok': False, 'reason': 'installed'}
    threading.Thread(target=_cli_install_thread, daemon=True).start()
    return {'ok': True}


def _cli_auth_thread(mode: str = 'qr') -> None:
    st = CLI['auth']
    st['running'] = True
    st['log'] = []
    st['qr'] = ''
    st['result'] = None
    st['mode'] = mode
    exe = goofish_path()
    if not exe:
        st['log'].append('✗ 还没安装 goofish-cli，先点「一键安装」')
        st['running'] = False
        return

    if mode == 'qr':
        st['log'].append('▶ 启动 Edge 弹窗扫码登录（用本机 Edge，无需安装 Chrome）…')
        st['log'].append('   请在弹出的 Edge 窗口里用「闲鱼 App」扫码；扫完在手机上点「确认登录」。')
        st['log'].append('   关闭窗口或超时（约 3 分钟）即结束。')
        try:
            import qr_login_shim
            def sink(m):
                st['log'].append(m)
                if len(st['log']) > 600:
                    st['log'].pop(0)
            res = qr_login_shim.login_via_qr(timeout=180, log_sink=sink)
            st['result'] = res
            st['log'].append(('✓ ' if res['ok'] else '✗ ') + res.get('msg', ''))
        except Exception as e:
            st['result'] = {'ok': False, 'reason': 'exception', 'msg': str(e)}
            st['log'].append('异常：' + str(e))
        finally:
            st['running'] = False
        return

    # mode == 'auto'：从本机已登录的浏览器读取 cookie
    st['log'].append('▶ 运行 `goofish auth login`（自动读取本机浏览器里已登录的闲鱼会话）…')
    try:
        st['proc'] = subprocess.Popen(
            [exe, 'auth', 'login'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding='utf-8', errors='replace', bufsize=1,
            cwd=str(BASE))
        for line in st['proc'].stdout:
            line = line.rstrip('\n')
            st['qr'] = (st['qr'] + '\n' + line) if st['qr'] else line
            st['log'].append(line)
            if len(st['log']) > 600:
                st['log'].pop(0)
        st['proc'].wait()
        rc = st['proc'].returncode
        st['log'].append(f'进程结束，退出码 {rc}')
        st['result'] = {'ok': rc == 0, 'reason': 'done' if rc == 0 else 'exit',
                        'msg': '导入完成' if rc == 0 else '导入失败，看上面对话'}
    except Exception as e:
        st['result'] = {'ok': False, 'reason': 'exception', 'msg': str(e)}
        st['log'].append('异常：' + str(e))
    finally:
        st['running'] = False
        st['proc'] = None


def cli_auth_start(mode: str = 'qr') -> dict:
    if CLI['auth']['running']:
        return {'ok': False, 'reason': 'running'}
    threading.Thread(target=_cli_auth_thread, args=(mode,), daemon=True).start()
    return {'ok': True}


def _kill_edge_qr_profiles() -> None:
    """结束扫码登录弹出的 Edge 进程（仅限 .goofish-cli/profiles/ 下的临时 profile）。"""
    ps = [
        'powershell', '-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*.goofish-cli*profiles*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
    ]
    try:
        subprocess.run(ps, capture_output=True, timeout=20)
    except Exception as e:
        log('关闭扫码 Edge 窗口失败（可手动关闭）：' + str(e))


def cli_auth_cancel() -> None:
    st = CLI['auth']
    proc = st.get('proc')
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    else:
        # qr 模式：关掉我们弹出的 Edge 窗口
        _kill_edge_qr_profiles()
    st['running'] = False
    st['log'].append('■ 已取消登录')


# ---- 第一步：一键自导出（弹浏览器扫码，工作台第二步也有入口）----
EXP = {'running': False, 'log': [], 'proc': None, 'result': None, 'zip': ''}


def _export_thread(name: str, limit: int) -> None:
    st = EXP
    st['running'] = True
    st['log'] = []
    st['result'] = None
    st['zip'] = ''
    script = ROOT / 'export_items.py'
    try:
        if not script.exists():
            st['log'].append('✗ 找不到 export_items.py，工具包可能没拷全')
            st['result'] = {'ok': False, 'msg': '缺少 export_items.py'}
            return
        cmd = [sys.executable, str(script)]
        if name:
            cmd += ['--name', name]
        if limit and limit > 0:
            cmd += ['--limit', str(limit)]
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        st['log'].append('▶ 正在弹出独立 Edge 窗口…请在里面登录「要导出的那个号」（扫码即可）。')
        st['log'].append('   首次要扫码；之后登录态留在「数据/浏览器资料」，再导就不用扫了。')
        st['log'].append('   登录后全自动：读列表 → 下图片 → 打包 ZIP 到收件箱。件数多时别关窗口。')
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            cwd=str(BASE), env=env)
        st['proc'] = proc
        for line in proc.stdout:
            line = line.rstrip('\n')
            if line:
                st['log'].append(line)
                if len(st['log']) > 600:
                    st['log'].pop(0)
        proc.wait()
        rc = proc.returncode
        try:
            zips = sorted(INBOX.glob('自导出_*.zip'), key=lambda x: x.stat().st_mtime)
            if zips:
                st['zip'] = zips[-1].name
        except Exception:
            pass
        ok = rc == 0
        msg = (f'导出完成：{st["zip"]}，在上方下拉框选它导入即可'
               if ok and st['zip'] else
               ('导出完成' if ok else f'导出失败（退出码 {rc}），看上面对话'))
        st['result'] = {'ok': ok, 'msg': msg}
        st['log'].append(('✓ ' if ok else '✗ ') + msg)
        log(f'自导出结束，退出码 {rc}' + (f'，产物 {st["zip"]}' if st['zip'] else ''))
    except Exception as e:
        st['result'] = {'ok': False, 'msg': str(e)}
        st['log'].append('异常：' + traceback.format_exc()[-500:])
    finally:
        st['running'] = False
        st['proc'] = None


def export_start(name: str, limit) -> dict:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    if EXP['running']:
        return {'ok': False, 'reason': 'running'}
    if IMPORT['running'] or PUB['running']:
        return {'ok': False, 'reason': 'busy'}
    threading.Thread(target=_export_thread, args=(name, limit), daemon=True).start()
    return {'ok': True}


def export_stop() -> None:
    st = EXP
    proc = st.get('proc')
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    # 弹出的 Edge 窗口也要关；按「浏览器资料」目录匹配，不会误伤日常在用的 Edge
    ps = [
        'powershell', '-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*浏览器资料*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
    ]
    try:
        subprocess.run(ps, capture_output=True, timeout=20)
    except Exception:
        pass
    st['log'].append('■ 已停止导出（已抓到的数据不保留 ZIP）')


# ---- 第 1 步：环境自检 + 一键装依赖（工作台网页内完成）----
ENVCHK = {'installing': False, 'log': [], 'checked': None}


def env_check() -> dict:
    """只读自检，逻辑对齐 app/check_env.py，返回结构化结果。"""
    import importlib.util as iu
    items: list[dict] = []

    def add(name: str, flag: str, detail: str = '') -> None:
        items.append({'name': name, 'flag': flag, 'detail': detail})  # ok/warn/bad

    v = sys.version_info
    add('Python', 'ok' if v >= (3, 9) else 'bad', f'{v.major}.{v.minor}.{v.micro}（需要 3.9+）')
    cfg = load_config()
    add('config.json', 'ok' if CONFIG_FILE.exists() else 'bad',
        f'data_dir = {cfg.get("data_dir")}' if CONFIG_FILE.exists() else '读取失败')
    add('数据目录', 'ok' if DATA.is_dir() else 'bad', str(DATA))
    for sub in ('收件箱', '项目', '发布包', '浏览器资料'):
        d = DATA / sub
        add(f'　{sub}/', 'ok' if d.is_dir() and os.access(d, os.W_OK) else 'bad',
            '可写' if d.is_dir() else '不存在')
    for mod, label, required in (
        ('playwright', 'playwright（浏览器拦截导出）', True),
        ('cv2', 'opencv（二维码 / 图片处理）', True),
        ('numpy', 'numpy', True),
        ('PIL', 'pillow', False),
        ('rapidocr_onnxruntime', 'RapidOCR（图片文字识别）', False),
        ('openpyxl', 'openpyxl（A 号人工决策表）', False),
    ):
        found = iu.find_spec(mod) is not None
        add(label, 'ok' if found else ('bad' if required else 'warn'),
            '' if found else ('缺，点「一键安装依赖」' if required else '可选，缺了只影响该项功能'))
    if iu.find_spec('playwright') is not None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch(channel='msedge', headless=True,
                                       args=['--no-first-run'])
                b.close()
            add('本机 Edge', 'ok', '可由 Playwright 驱动，导出器可用')
        except Exception as e:
            add('本机 Edge', 'bad', str(e)[:110])
    else:
        add('本机 Edge', 'bad', 'playwright 没装，跳过检测')
    exe = goofish_path()
    add('goofish-cli（真实发布）', 'ok' if exe else 'warn',
        exe or '未安装；只影响发布页真实发布，可在发布页点「一键安装」')
    zips = list(INBOX.glob('*.zip')) if INBOX.is_dir() else []
    add('收件箱 ZIP', 'ok', f'{len(zips)} 个')
    problems = [x['name'] for x in items if x['flag'] == 'bad']
    return {'ok': not problems, 'items': items, 'problems': problems}


def _env_install_thread() -> None:
    st = ENVCHK
    st['installing'] = True
    st['log'] = ['▶ 正在安装依赖（pip install -r requirements.txt）…需要联网，每台电脑装一次']
    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
             '-r', str(ROOT / 'requirements.txt')],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding='utf-8', errors='replace', bufsize=1, cwd=str(BASE))
        for line in proc.stdout:
            st['log'].append(line.rstrip('\n'))
            if len(st['log']) > 500:
                st['log'].pop(0)
        proc.wait()
        rc = proc.returncode
        st['log'].append((f'✓ pip 完成，退出码 {rc}，自动重跑自检…' if rc == 0
                          else f'✗ pip 退出码 {rc}，看上面的报错（多为网络问题，重试即可）'))
        st['checked'] = env_check()
    except Exception:
        st['log'].append('异常：' + traceback.format_exc()[-500:])
    finally:
        st['installing'] = False


def _record_published(rec_path: Path, m: dict) -> None:
    """发布成功后追加记录（按清单 id 去重），下次发布自动跳过这些件。"""
    try:
        recs = jload(rec_path, []) or []
        recs.append({'id': m.get('id'), 'title': (m.get('title') or '')[:40],
                     'time': dt.datetime.now().isoformat(timespec='seconds')})
        rec_path.write_text(json.dumps(recs, ensure_ascii=False, indent=1),
                            encoding='utf-8')
    except Exception as e:
        log(f'写入发布记录失败（不影响已发出去的商品）：{e}')


def _run_publish_cmd(cmd: list, folder: Path) -> tuple:
    """跑一次 goofish publish 子进程；期间每 0.4s 检查停止指令，收到立刻杀掉。
    返回 (returncode, 输出, 是否被手动停止)。"""
    proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace')
    stopped_mid = False
    # 最多等 180 秒
    for _ in range(450):
        rc = proc.poll()
        if rc is not None:
            break
        with LOCK:
            if PUB['stop']:
                stopped_mid = True
                break
        time.sleep(0.4)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    out = (proc.stdout.read() or '').strip() if proc.stdout else ''
    if proc.stdout:
        try:
            proc.stdout.close()
        except Exception:
            pass
    return proc.returncode, out, stopped_mid


def _fallback_category(title: str) -> tuple:
    """类目推荐失败时的兜底：先试简化标题，再试通用词，拿到第一个合法类目即返回。
    返回 (类目dict 或 None, 命中的查询词)。"""
    try:
        from goofish_cli.commands.category.recommend import recommend
    except Exception:
        return None, ''
    import re as _re
    clean = _re.sub(r'[A-Za-z]{1,6}\d{3,}', ' ', title)          # 型号：XX250110
    clean = _re.sub(r'\d+(?:\.\d+)?\s*[A-Za-z]{1,4}\b', ' ', clean)  # 规格：10A 2500W
    clean = _re.sub(r'[·•・\-—_，,。]', ' ', clean)
    cands: list[str] = []
    c = clean.strip()
    if c and c != title:
        cands.append(c)
    cands += ['温控开关', '宠物用品', '收纳用品', '家居日用', '日用百货']
    need = ('cat_id', 'cat_name', 'channel_cat_id', 'tb_cat_id')
    for q in cands:
        try:
            cat = recommend(q, '[]')
            if all(cat.get(k) for k in need):
                return {k: cat[k] for k in need}, q
        except Exception:
            continue
    return None, ''


def _sleep_check(seconds: float) -> bool:
    """可中断睡眠：期间每 0.4s 检查停止指令。返回 True=收到了停止。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        with LOCK:
            if PUB['stop']:
                return True
        time.sleep(0.4)
    return False


def _rate_retry(cmd: list, folder: Path, title: str) -> tuple:
    """跑发布命令；遇上传限速自动等 goofish 提示的秒数后重试（最多 3 次）。
    返回 (rc, out, stopped_mid)。"""
    attempts = 0
    while True:
        rc, out, stopped_mid = _run_publish_cmd(cmd, folder)
        if stopped_mid or rc == 0:
            return rc, out, stopped_mid
        if ('RateLimited' in out or '上限' in out) and attempts < 3:
            wm = re.search(r'再等\s*([\d.]+)\s*s', out)
            wait = min(120.0, max(15.0, float(wm.group(1)) + 6)) if wm else 66.0
            attempts += 1
            log(f'[限速等待] 上传限速（每 60 秒 9 张），自动等 {wait:.0f} 秒后重试'
                f'（第 {attempts}/3 次）：{title[:26]}')
            if _sleep_check(wait):
                return rc, out, True
            continue
        return rc, out, stopped_mid


def run_publish(mode: str) -> None:
    manifest = jload(OUTROOT / '_manifest.json', None)
    if not manifest or not manifest.get('items'):
        log('没有发布包，请先在第二步点「生成发布包」')
        return
    st = goofish_status()
    if mode == 'live' and not st['installed']:
        log('真实发布需要本机已安装 goofish-cli，当前未安装，已拒绝')
        return
    ids = PUB.get('ids') or []
    if ids:
        idset = set(ids)
        queue = [m for m in manifest['items'] if m.get('id') in idset]
        log(f'按勾选发布 {len(queue)} 件（勾了 {len(idset)} 件）')
    else:
        count = PUB.get('count') or 0
        queue = manifest['items'] if count <= 0 else manifest['items'][:count]
    rec_path = OUTROOT / '发布记录.json'
    if mode == 'live':
        done_ids = {r.get('id') for r in (jload(rec_path, []) or [])}
        if done_ids:
            before = len(queue)
            queue = [m for m in queue if m.get('id') not in done_ids]
            if len(queue) < before:
                log(f'跳过 {before - len(queue)} 件已发布过的商品（记录在发布包的 发布记录.json），'
                    f'本次只发剩余 {len(queue)} 件')
        if not queue:
            log('勾选/清单里的商品都已发布过，无事可做。要整套重发就删掉发布包里的 发布记录.json')
            return
    with LOCK:
        if PUB['running']:
            return
        PUB.update(running=True, stop=False, mode=mode, done=0, ok=0, fail=0,
                   total=len(queue), current='',
                   started=dt.datetime.now().isoformat(timespec='seconds'))
    log(f'发布任务开始：{mode}，本次 {len(queue)} 件'
        + (f'（清单共 {len(manifest["items"])} 件）' if len(manifest['items']) > len(queue) else ''))
    n = 0
    consecutive_fails = 0
    for m in queue:
        with LOCK:
            if PUB['stop']:
                break
            PUB['current'] = m['title'][:30]
        folder = OUTROOT / m['folder']
        info = jload(folder / '商品信息.json')
        cmd = [goofish_path(), 'item', 'publish', info['title'], info['desc'],
               *info['images'], f"{float(info['price']):g}",
               '--delivery', info.get('delivery') or '无需邮寄', '--format', 'json']
        if mode == 'dry':
            log(f'[演练 {n+1}/{PUB["total"]}] {m["title"][:34]} ¥{info["price"]:g} '
                f'{len(info["images"])} 张图 → 未发送')
            time.sleep(0.8)
            with LOCK:
                PUB['ok'] += 1
        else:
            rc, out, stopped_mid = _rate_retry(cmd, folder, m['title'])
            # 类目推荐失败 → 自动兜底：简化标题/通用词拿到类目后用 --category-json 强制重发
            # 兜底重发同样会被限速挡（图片会重传一遍），所以也走限速重试
            if not stopped_mid and rc != 0 and ('类目推荐' in out or '分类数据' in out):
                log(f'[类目兜底] 闲鱼认不出「{m["title"][:24]}」的类目，正在用通用说法重新识别…')
                cat, q = _fallback_category(info['title'])
                if cat:
                    log(f'[类目兜底] 以「{q}」匹配到类目「{cat["cat_name"]}」，用该类目重新发布')
                    cmd2 = cmd[:-2] + ['--category-json', json.dumps(cat, ensure_ascii=False)] + cmd[-2:]
                    rc, out, stopped_mid = _rate_retry(cmd2, folder, m['title'])
                else:
                    out = out + '\n[类目兜底] 所有通用说法也识别失败，这件需要人工处理'
            if stopped_mid:
                log(f'[已手动停止] {m["title"][:30]} 当前这件被中止，未计入成功')
                with LOCK:
                    PUB['fail'] += 1
                break
            if rc == 0:
                log(f'[发布成功] {m["title"][:30]} {out[:150]}')
                _record_published(rec_path, m)
                with LOCK:
                    PUB['ok'] += 1
                consecutive_fails = 0
            else:
                log(f'[发布失败] {m["title"][:30]} {out[:300]} — 跳过这件，继续发下一件')
                with LOCK:
                    PUB['fail'] += 1
                consecutive_fails += 1
                if consecutive_fails >= 5:
                    log('连续 5 件失败，多半是登录态过期或网络异常，先停下；'
                        '排查后在发布页重新勾选没发的件即可继续')
                    break
        if _sleep_check(60):
                break
        n += 1
        with LOCK:
            PUB['done'] = n
    with LOCK:
        PUB['running'] = False
        PUB['current'] = ''
    log(f'发布任务结束：成功 {PUB["ok"]}，失败 {PUB["fail"]}')


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        try:
            body = path.read_bytes()
        except Exception:
            self.send_error(404); return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        try:
            if p in ('/', '/index.html'):
                return self._file(ROOT / 'index.html', 'text/html; charset=utf-8')
            if p == '/api/state':
                pkg = jload(OUTROOT / '_manifest.json', None)
                zips = [f.name for f in INBOX.glob('*.zip')] if INBOX.is_dir() else []
                return self._json({
                    'aj9': str(BASE), 'base': str(BASE), 'data': str(DATA),
                    'configFile': str(CONFIG_FILE),
                    'dataDir': load_config().get('data_dir', '数据'),
                    'inbox': str(INBOX), 'packages': str(PACKAGES),
                    'src': str(SRC), 'outroot': str(OUTROOT),
                    'manifest': str(MANIFEST), 'log': str(LOGFILE),
                    'zips': zips, 'itemCount': len(CATALOG),
                    'project': {k: ACTIVE.get(k) for k in
                                ('id', 'name', 'dir', 'zip', 'importedAt',
                                 'itemCount', 'legacy', 'scanned', 'state', 'outroot')},
                    'projects': PROJECTS['list'],
                    'isLegacy': is_legacy(),
                    'import': dict(IMPORT),
                    'package': {'count': pkg['count'], 'createdAt': pkg['createdAt']} if pkg else None,
                    'cli': goofish_status(),
                    'cliOp': _cli_public(),
                    'logLines': LOGS[-40:],
                })
            if p == '/api/import/status':
                return self._json({'import': dict(IMPORT), 'active': ACTIVE.get('id', '')})
            if p == '/api/items':
                st = get_state()
                out = []
                for it in CATALOG:
                    row = st['items'].get(it['id'], {})
                    d = dict(it)
                    d['include'] = bool(row.get('include'))
                    d['newTitle'] = row.get('title', it['newTitle'])
                    d['newDesc'] = row.get('desc', it['newDesc'])
                    d['editPrice'] = row.get('price', it['price'])
                    d['delivery'] = row.get('delivery', '包邮')
                    d['actions'] = row.get('actions', {})
                    d['rewrite'] = row.get('rewrite')
                    out.append(d)
                return self._json({'items': out})
            if p.startswith('/media/'):
                parts = p[len('/media/'):].split('/')
                if len(parts) != 2:
                    self.send_error(404); return
                iid, fn = parts
                f = (MEDIA / iid / fn).resolve()
                if not str(f).startswith(str(MEDIA.resolve())) or not f.is_file():
                    self.send_error(404); return
                ext = f.suffix.lower()
                ctype = {'.webp': 'image/webp', '.jpg': 'image/jpeg',
                         '.jpeg': 'image/jpeg', '.png': 'image/png',
                         '.mp4': 'video/mp4'}.get(ext, 'application/octet-stream')
                return self._file(f, ctype)
            if p.startswith('/preview/'):
                # 实时生成「按当前动作处理后」的样子，只写缓存目录，原图不动
                rest = p[len('/preview/'):].split('/')
                if len(rest) != 2 or MEDIA is None:
                    self.send_error(404); return
                iid, fn = rest
                src = (MEDIA / iid / fn).resolve()
                if not str(src).startswith(str(MEDIA.resolve())) or not src.is_file():
                    self.send_error(404); return
                act = urllib.parse.parse_qs(u.query).get('action', ['keep'])[0]
                if act not in ACTION_LABEL:
                    act = 'keep'
                if act == 'delete':
                    self.send_error(404); return        # 前端直接显示「整张删除」
                key = hashlib.md5(f'{iid}|{fn}|{act}|{src.stat().st_mtime_ns}'
                                  .encode('utf-8')).hexdigest()[:16]
                cache = DATA / '_预览缓存' / key
                files = [x for x in cache.iterdir() if x.is_file()] if cache.is_dir() else []
                if not files:
                    cache.mkdir(parents=True, exist_ok=True)
                    out = process_image(src, act, cache)
                    files = [x for x in cache.iterdir() if x.is_file()] if out else []
                if not files:
                    # 二维码涂不净 / 检测器误判时，处理结果是「弃用这张」。
                    # 返回占位图而不是 404，前端靠尺寸识别后显示文字说明。
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Content-Length', str(len(PLACEHOLDER_PNG)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(PLACEHOLDER_PNG)
                    return
                f = files[0]
                ctype = {'.webp': 'image/webp', '.jpg': 'image/jpeg',
                         '.jpeg': 'image/jpeg', '.png': 'image/png'}.get(
                             f.suffix.lower(), 'image/png')
                return self._file(f, ctype)
            if p == '/api/pkg/items':
                manifest = jload(OUTROOT / '_manifest.json', None) or {}
                done = {r.get('id') for r in (jload(OUTROOT / '发布记录.json', []) or [])}
                items = []
                for m in manifest.get('items', []):
                    info = jload(OUTROOT / m['folder'] / '商品信息.json', {}) or {}
                    imgs = [f'/api/pkg/img/{urllib.parse.quote(m["folder"])}/{urllib.parse.quote(f)}'
                            for f in (info.get('images') or [])]
                    items.append({'id': m['id'], 'title': m['title'], 'price': m.get('price'),
                                  'folder': m['folder'], 'desc': info.get('desc', ''),
                                  'delivery': info.get('delivery', ''),
                                  'images': imgs, 'published': m['id'] in done})
                return self._json({'items': items, 'outroot': str(OUTROOT)})
            if p.startswith('/api/pkg/img/'):
                rel = urllib.parse.unquote(p[len('/api/pkg/img/'):].lstrip('/'))
                fp = (OUTROOT / rel).resolve()
                try:
                    fp.relative_to(OUTROOT.resolve())
                except ValueError:
                    self.send_error(403); return
                if not fp.is_file():
                    self.send_error(404); return
                ctype = {'.webp': 'image/webp', '.jpg': 'image/jpeg',
                         '.jpeg': 'image/jpeg', '.png': 'image/png'}.get(
                             fp.suffix.lower(), 'image/png')
                return self._file(fp, ctype)
            if p == '/api/publish/status':
                return self._json({'pub': dict(PUB), 'logs': LOGS[-80:],
                                   'cli': goofish_status(), 'cliOp': _cli_public(),
                                   'package': jload(OUTROOT / '_manifest.json', None)})
            if p == '/api/env/status':
                return self._json({'env': {'installing': ENVCHK['installing'],
                                           'log': ENVCHK['log'],
                                           'checked': ENVCHK['checked']}})
            if p == '/api/export/status':
                return self._json({'exp': {k: v for k, v in EXP.items() if k != 'proc'}})
            if p == '/api/cli/status':
                return self._json(_cli_public())
            if p == '/api/health':
                return self._json({'ok': True})
            self.send_error(404)
        except Exception:
            log('GET 异常：\n' + traceback.format_exc())
            self._json({'error': traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            if p == '/api/config':
                if IMPORT['running'] or PUB['running']:
                    return self._json({'error': '有导入/发布任务在跑，等它结束再换数据目录'}, 400)
                raw = str(body.get('dataDir') or '').strip().strip('"')
                if not raw:
                    return self._json({'error': '数据目录不能为空'}, 400)
                return self._json({'ok': True, **apply_config(raw)})
            if p == '/api/import':
                with IMPORT_LOCK:
                    if IMPORT['running']:
                        return self._json({'error': '上一个包还在扫描，等它跑完再导入'}, 400)
                    zp = str(body.get('path') or '').strip().strip('"').strip("'")
                    if zp:
                        zpath = Path(zp)
                    else:
                        fn = str(body.get('zip') or '').strip()
                        if not fn:
                            return self._json({'error': '请选择收件箱里的 ZIP，或填一个绝对路径'}, 400)
                        zpath = INBOX / Path(fn).name
                    return self._json(import_zip(zpath, str(body.get('name') or '').strip()))
            if p == '/api/projects/switch':
                return self._json({'ok': True,
                                   'project': activate(str(body.get('id') or ''))})
            if p == '/api/bulk-include':
                mode = body.get('mode') or 'risk'
                ids = body.get('ids')
                idset = {str(x) for x in ids} if isinstance(ids, list) else None
                st = get_state()
                n = 0
                for it in CATALOG:
                    if idset is not None and it['id'] not in idset:
                        continue
                    row = st['items'][it['id']]
                    if mode == 'invert':
                        v = not bool(row.get('include'))
                    elif mode == 'all':
                        v = True
                    elif mode == 'none':
                        v = False
                    else:
                        v = it['conclusion'] in ('可搬', '改后搬')
                    row['include'] = v
                    n += 1
                st['updatedAt'] = dt.datetime.now().isoformat(timespec='seconds')
                jwrite(STATE_FILE, st)
                log(f'批量纳入：{mode}，作用 {n} 件')
                return self._json({'ok': True, 'mode': mode, 'count': n})
            if p == '/api/save':
                st = get_state()
                iid = str(body.get('id', ''))
                row = st['items'].setdefault(iid, {})
                for k in ('include', 'title', 'desc', 'price', 'delivery', 'actions'):
                    if k in body:
                        row[k] = body[k]
                if body.get('restore'):
                    row.pop('rewrite', None)
                st['updatedAt'] = dt.datetime.now().isoformat(timespec='seconds')
                jwrite(STATE_FILE, st)
                return self._json({'ok': True})
            if p == '/api/auto-rewrite':
                try:
                    return self._json(auto_rewrite(body))
                except Exception as e:
                    log('auto-rewrite 失败：' + traceback.format_exc())
                    return self._json({'error': str(e)}, 400)
            if p == '/api/env/check':
                try:
                    res = env_check()
                    ENVCHK['checked'] = res
                    return self._json({'env': res})
                except Exception:
                    log('环境自检异常：\n' + traceback.format_exc())
                    return self._json({'error': '自检出错，看工作台日志'}, 500)
            if p == '/api/env/install':
                if ENVCHK['installing']:
                    return self._json({'ok': False, 'reason': 'running'})
                threading.Thread(target=_env_install_thread, daemon=True).start()
                return self._json({'ok': True})
            if p == '/api/rules-check':
                try:
                    return self._json(rule_check())
                except Exception as e:
                    return self._json({'error': str(e)}, 400)
            if p == '/api/smart-clean':
                return self._json(smart_clean())
            if p == '/api/build':
                return self._json(build_package())
            if p == '/api/publish/start':
                mode = 'live' if body.get('mode') == 'live' and body.get('confirm') else 'dry'
                if body.get('mode') == 'live' and not body.get('confirm'):
                    return self._json({'error': '真实发布需要显式确认'}, 400)
                if PUB['running']:
                    return self._json({'error': '任务正在运行'}, 400)
                try:
                    cnt = int(body.get('count') or 0)
                except (TypeError, ValueError):
                    cnt = 0
                if cnt < 0:
                    cnt = 0
                PUB['count'] = cnt
                ids = body.get('ids')
                PUB['ids'] = [str(x) for x in ids] if isinstance(ids, list) else []
                t = threading.Thread(target=run_publish, args=(mode,), daemon=True)
                t.start()
                return self._json({'ok': True, 'mode': mode, 'count': cnt})
            if p == '/api/pkg/edit':
                # 修改发布包里的单件商品（标题/描述/价格/发货方式），直接写回
                # 商品信息.json 与 _manifest.json；对已发出去的线上商品无影响。
                if PUB['running']:
                    return self._json({'error': '发布进行中，等结束再改'}, 400)
                iid = str(body.get('id') or '')
                manifest = jload(OUTROOT / '_manifest.json', None)
                if not manifest:
                    return self._json({'error': '还没有发布包'}, 400)
                m = next((x for x in manifest.get('items', [])
                          if str(x.get('id')) == iid), None)
                if not m:
                    return self._json({'error': '清单里没有这件'}, 400)
                folder = OUTROOT / m['folder']
                info_path = folder / '商品信息.json'
                info = jload(info_path, None)
                if info is None:
                    return self._json({'error': '商品信息.json 缺失'}, 400)
                changed = []
                if 'title' in body:
                    t = str(body['title']).strip()
                    if not t:
                        return self._json({'error': '标题不能为空'}, 400)
                    info['title'], m['title'] = t, t
                    changed.append('标题')
                if 'desc' in body:
                    info['desc'] = str(body['desc'])
                    changed.append('描述')
                if 'price' in body:
                    try:
                        p2 = round(float(body['price']), 2)
                    except (TypeError, ValueError):
                        return self._json({'error': '价格必须是数字'}, 400)
                    if p2 < 0 or p2 > 999999:
                        return self._json({'error': '价格超出范围'}, 400)
                    info['price'], m['price'] = p2, p2
                    changed.append('价格')
                if 'delivery' in body:
                    dv = str(body['delivery'])
                    if dv not in ('包邮', '按距离计费', '一口价', '无需邮寄'):
                        return self._json({'error': '发货方式不合法'}, 400)
                    info['delivery'] = dv
                    changed.append('发货方式')
                info_path.write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                     encoding='utf-8')
                jwrite(OUTROOT / '_manifest.json', manifest)
                log(f'已修改发布包商品（{"、".join(changed) or "无改动"}）：{info["title"][:30]}')
                return self._json({'ok': True, 'changed': changed})
            if p == '/api/publish/stop':
                with LOCK:
                    PUB['stop'] = True
                log('收到停止指令，当前这件完成后停下')
                return self._json({'ok': True})
            if p == '/api/export/start':
                return self._json(export_start(str(body.get('name') or '').strip(),
                                               body.get('limit')))
            if p == '/api/export/stop':
                export_stop()
                return self._json({'ok': True})
            if p == '/api/cli/install':
                return self._json(cli_install_start())
            if p == '/api/cli/auth':
                return self._json(cli_auth_start(str(body.get('mode') or 'qr')))
            if p == '/api/cli/auth/cancel':
                cli_auth_cancel()
                return self._json({'ok': True})
            self.send_error(404)
        except Exception:
            log('POST 异常：\n' + traceback.format_exc())
            self._json({'error': traceback.format_exc()[-800:]}, 500)


def main():
    global PORT
    init_projects()
    if PROJECTS['list']:
        activate(PROJECTS['active'])
        log(f'工作台载入 {len(PROJECTS["list"])} 个项目，当前 {len(CATALOG)} 件商品，素材目录 {SRC}')
    else:
        blank_active()
        log(f'还没有项目。把导出 ZIP 放进 {INBOX}，在网页第一步点「导入」。')
    log(f'工具包 {BASE}　数据目录 {DATA}')
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(load_config().get('port') or 8765))
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    for port in range(a.port, a.port + 10):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            PORT = port
            break
        except OSError:
            continue
    else:
        print('端口全被占用'); sys.exit(1)
    url = f'http://127.0.0.1:{PORT}/'
    log(f'工作台已启动：{url}')
    if not a.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('工作台已关闭')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('启动失败：\n' + traceback.format_exc())
        sys.exit(1)
