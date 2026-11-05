# -*- coding: utf-8 -*-
"""本地自动文案改写引擎（纯标准库 / 离线 / 不调用任何大模型）

做法：不替换原句里的几个字，而是把原标题、描述拆成结构化字段
（品牌 / 型号 / 规格 / 尺寸 / 成色 / 包装 / 有效期 / 类目），
再用轮换的骨架重新组织成全新文案 —— 句式、段落顺序、呈现形式都换。

三条硬约束：
1. 只使用能从原文确认的事实 + 通用交易说明；不虚构转手故事、不编使用场景。
2. 不声称「实拍」（首图可能是平台截图）、不写功效 / 医用 / 引流 / 极限词。
3. 生成后做相似度与撞车检查，保证与原号文案、同批次其它商品都拉开距离。
"""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------- 安全词表
# 命中即整段删掉；这些词不能靠「改写」洗白，高危商品应直接跳过重写。
BAN_RULES = [
    ('联系方式/引流', r'(微信|威信|weixin|vx|V信|QQ|qq号|加我|私聊|扫码|扫一扫|二维码|公众号|关注我|站外)'),
    ('网址电话', r'(https?://|www\.|\.com|\.cn|\.net|1[3-9]\d{9}|400-?\d{3}-?\d{4}|联系电话)'),
    ('极限词', r'(最好|最低价|全网最低|第一品牌|国家级|顶级|包治|根治|100%有效|绝对正品|全网第一|史上最)'),
    ('售假暗示', r'(正品保证|正品渠道|专柜|官方旗舰|官方授权|原单|尾单|高仿|A货|全新正品|质量放心)'),
    ('医疗功效', r'(医用|医疗器械|灭菌级|外科口罩|治疗|疗效|药用|抑菌|杀菌|消毒|祛疤|疤痕凝胶|保健液|矫正|防感染|缓解|适用于.{0,8}(症|病)|医生指导)'),
    ('平台水印', r'(京东JD|JD\.?COM|JDL|天猫|淘宝特价|拼多多|抖音商城|旗舰店)'),
]
BAN_RE = re.compile('|'.join(f'(?:{p})' for _, p in BAN_RULES), re.I)

# 原文里的营销套话，提取事实前先剥掉，避免把原号句式带过来
BOILER = re.compile(
    r'(非偏远(地区)?包邮|包邮|支持自提|可自提|售出不退不换|售出不退|不退不换|'
    r'喜欢直接拍|看好下单|看好再拍|介意勿拍|有问题随时私聊|需要细节图可私聊|'
    r'细节图|拍前问清楚|可小刀|不议价|二手闲置)', re.I)

BRANDS = [
    '京东京造', '无印良品', '东方甄选', ' SYNTHWARE ', 'WATERFLY', 'Benefant',
    '欣维尔', '海锐得', '弗列加特', '万宝龙', '博世', 'BOSCH', '闪迪', 'SanDisk',
    '京造', 'MUJI', '联想', 'Lenovo', '世达', 'SATA', '史丹利', '得力', '绿联',
    '超能', '热风', 'hotwind', '惠象', '仙片', 'MASTO', '3M', '小米', '华为',
    '罗技', '戴尔', 'Apple', '索尼', '飞利浦', '松下', '公牛', '南孚', '双立人',
]
BRANDS = sorted({b.strip() for b in BRANDS if b.strip()}, key=len, reverse=True)

COND_WORDS = [
    '全新未拆封', '全新未开封', '全新未使用', '全新未用', '整袋未拆', '未拆封',
    '未开封', '几乎全新', '接近全新', '成色接近全新', '九成新', '八成新',
    '轻微划痕', '正常使用痕迹', '使用痕迹', '无拆无修', '没使用过', '闲置',
]
PACK_WORDS = ['原装手提袋', '原装蓝色包装盒', '原装盒', '原包装盒', '原包装',
              '原盒在', '原盒', '包装盒', '吊牌', '质保标签', '带盒', '裸机']
# 标题里不照抄原成色词，换成另一种说法，避免看起来「只改了几个字」
COND_ALT = {
    '全新未拆封': '没拆过', '全新未开封': '未开封', '全新未使用': '没上过手',
    '全新未用': '一次没用', '整袋未拆': '整袋没动', '未拆封': '封条还在',
    '未开封': '还没拆', '几乎全新': '成色很新', '接近全新': '接近新',
    '成色接近全新': '成色很新', '九成新': '九成左右', '八成新': '八成左右',
    '轻微划痕': '有细划痕', '正常使用痕迹': '有使用痕迹', '使用痕迹': '用过一阵',
    '无拆无修': '原装没动', '没使用过': '没用过', '闲置': '闲置',
}

OPENERS = [
    '整理闲置，把这件{name}转给用得上的人。',
    '一件{name}挂着也是占地方，出给需要的人。',
    '{name}一直收着没怎么用，这次一并清出来。',
    '闲置清单里还有一件{name}，一并挂出来。',
    '{name}用不上了，转给合适的人。',
    '这次清几件闲置，先说这件{name}。',
]
def _detect_brand(raw: str) -> str:
    """品牌只从白名单识别——稳健，绝不为未知品牌编造或切错字。
    白名单外的品牌会原样保留在标题正文（核心词）里，不会被丢掉。"""
    low = raw.lower()
    for b in BRANDS:
        if b.lower() in low:
            return b
    return ''
# 命中即判定为高危商品：不允许自动重写，只能人工判断是放弃还是重写
UNSAFE_RE = re.compile(
    r'(医用|医疗器械|外科口罩|成人用品|润滑液|私处|催情|壮阳|痔疮|造口|'
    r'祛疤|疤痕凝胶|苍耳子|保健液|抑菌|液体敷料|治疗|疗效|根治|包治|'
    r'私密修护|痔|癣|皮炎湿疹|干眼症|鼻炎|鼻塞)', re.I)
TRADES = [
    '非偏远地区包邮，同城可自提。二手闲置，售出不退不换，介意勿拍。',
    '默认包邮（偏远地区另议），也可以同城当面看货。二手物品不退不换，拍前确认清楚。',
    '邮寄默认包邮，能自提更好。闲置转出不支持无理由退换，有疑问先问。',
    '包邮出，急用可自提。二手闲置，到手后不退不换。',
    '默认邮寄包邮，验货当面更省事。闲置商品售出不退，确认好再拍。',
]
CLOSERS = [
    '需要补充细节或角度图，直接留言，看到会回。',
    '细节拿不准的地方，留言问一句再决定。',
    '还有想确认的地方可以留言，我按实物答复。',
    '其余信息看图，有需要再留言。',
]
STRUCTS = ['清单式', '叙述式', '问答式', '分栏式']

TITLE_TPL = [
    '{brand}{core} {spec}｜{condAlt} 闲置转出',
    '{condAlt}｜{brand}{core} {model} {spec}',
    '整理出一件{brand}{core}，{spec}，{condAlt}',
    '{brand} {model} {core} · {spec} · {condAlt}',
    '{spec} {brand}{core}，{condAlt}，可自提',
    '{core}转让｜{brand} {model} {spec} {condAlt}',
    '{brand}{core} 闲置出｜{spec}｜{condAlt}',
    '{condAlt} {brand}{core} {spec} 现货',
    '{brand} {core} {spec}，{condAlt}，包邮',
    '{model} {brand}{core}｜{condAlt} {spec}',
    '闲置{brand}{core} {spec}，{condAlt}',
    '{brand}{core} {spec}｜{condAlt} 实物看图',
]


def audit(text: str) -> list[str]:
    """返回命中的风险类别（供界面提示，不用于放行高危品）。"""
    return [name for name, pat in BAN_RULES if re.search(pat, text or '', re.I)]


def sanitize(text: str) -> tuple[str, list[str]]:
    """兜底净化：删掉命中词所在的小分句，并记录删了什么。"""
    removed = []
    s = text or ''
    for name, pat in BAN_RULES:
        rx = re.compile(pat, re.I)
        found = sorted(set(m.group(0) for m in rx.finditer(s)))
        if found:
            removed.extend(f'{name}:{x}' for x in found)
            # 删掉含命中词的那一小句，保持句子完整
            parts = re.split(r'([。；\n])', s)
            s = ''.join(p for p in parts if not rx.search(p))
    s = re.sub(r'[ \t]{2,}', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip(), removed


def _clean_orig(text: str) -> str:
    t = BOILER.sub('', text or '')
    t = re.sub(r'\([^)]*\)', ' ', t)        # 删半角括号及其中内容（尺码/备注）
    t = re.sub(r'（[^）]*）', ' ', t)        # 删全角括号及其中内容
    t = re.sub(r'[【】\[\]]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def extract(title: str, desc: str, category: str, price: str = '') -> dict:
    """从原文只抽客观事实，抽不到就留空，绝不猜。"""
    t, d = title or '', desc or ''
    raw = _clean_orig(t + ' ' + d)
    raw = re.sub(r'[（(][^（）()]*[)）]', ' ', raw)   # 先把括号及尺码/备注剥掉，型号规格提取才不会误抓

    brand = _detect_brand(raw)
    unsafe = bool(UNSAFE_RE.search(title + ' ' + desc))
    model = ''
    # 不能用 \b：中文也是 \w，「博世BT150」中间没有词边界，型号会整段漏掉
    m = re.search(r'(?<![A-Za-z0-9])([A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+){0,3}\d[A-Z0-9\-/]*)', raw)
    if m and len(m.group(1)) >= 4:
        model = m.group(1)

    specs = []
    pats = [
        r'\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?\s*(?:cm|mm|m|寸)?',
        r'\d+(?:\.\d+)?\s*(?:mm|cm|英寸|寸|米|片|枚|贴|支|个|只|条|盒|包|ml|mL|L|kg|g|GB|TB|W|V|A|目|贴/盒)',
        r'\b\d+(?:-\d+){2,}\b',
        r'\b1/4\s*(?:标准)?螺口|\bType-C\b|\bUHS-I\b|\bIP\d{2}\b',
    ]
    for rx in pats:
        for x in re.findall(rx, raw, re.I):
            x = re.sub(r'\s+', '', str(x)).strip()
            if x and x not in specs and not re.fullmatch(r'\d+', x):
                specs.append(x)
        if len(specs) >= 4:
            break
    specs = specs[:4]
    # 型号的数字尾段常被规格正则再抓一次（HG6940L → 6940L），去掉重复
    if model:
        specs = [x for x in specs if x.lower() not in model.lower()]

    cond = next((c for c in COND_WORDS if c in raw), '')
    pack = next((p for p in PACK_WORDS if p in raw), '')
    exp = ''
    e = re.search(r'(?:有效期(?:到|至)?|保质期(?:到|至)?|日期到)\s*(\d{4}\s*年(?:\s*\d{1,2}\s*月)?|\d{4}(?!\d))', raw)
    if e:
        exp = re.sub(r'\s+', '', e.group(1))

    core = re.sub(r'[（(].*?[)）]', '', category or '').strip()
    if core in ('', '其他闲置', '其他', '闲置', '未分类') or len(core) > 12:
        c = BOILER.sub(' ', t)
        c = re.sub(r'[|｜·,，。/\-—\\]', ' ', c)
        for b in BRANDS:
            c = re.sub(re.escape(b), ' ', c, flags=re.I)
        if brand:
            c = re.sub(re.escape(brand), ' ', c, flags=re.I)
        # 去掉型号（字母数字串，含数字尾）
        c = re.sub(r'[A-Za-z0-9][A-Za-z0-9\-/]{2,}\d[A-Za-z0-9\-/]*', ' ', c)
        # 去掉数量 / 规格数字串（带或不带单位）
        c = re.sub(r'\d+(?:\.\d+)?\s*(?:mm|cm|英寸|寸|米|片|枚|贴|支|个|只|条|盒|包|ml|mL|L|kg|g|GB|TB|W|V|A|目)?', ' ', c, re.I)
        # 去掉中文数词 + 量词（一包 / 一盒 / 三件套…），避免截断后留「一」字尾巴
        c = re.sub(r'[一二三四五六七八九十百千零两]+\s*(?:包|盒|袋|瓶|对|套|罐|件|只|个|条|支|枚|贴|片|双|副|根)', ' ', c)
        # 去掉括号及其中内容（尺码 / 规格备注）、残括号、尺码词（4XL / XXL 码）
        c = re.sub(r'[（(][^（）()]*[)）]', ' ', c)
        c = re.sub(r'[（()）]', '', c)
        c = re.sub(r'\d*(?:XXXL|XXL|XS|XL|L|M|S)\b', ' ', c, re.I)
        c = re.sub(r'码\b', ' ', c)
        # 去掉成色 / 包装 / 营销套话（整词移除，绝不切半截留「封」「教」之类的尾巴）
        for w in list(COND_WORDS) + list(PACK_WORDS) + ['全新', '几乎', '闲置', '转让', '开封', '未拆', '冬天用的', '夏天用的', '适合', '用的', '现货', '型号', '规格', '颜色', '尺寸', '款']:
            c = c.replace(w, ' ')
        # 型号常带字母前缀（SW-001 / HG6940L），数字段已被上面清掉，这里把字母前缀也抹掉
        if model:
            c = c.replace(model, ' ')
            mp = re.sub(r'\d.*', '', model).rstrip('-/ ')
            if mp:
                c = c.replace(mp, ' ')
        c = re.sub(r'\s+', '', c).strip(' -—·|,，。')
        core = c[:10] or '闲置好物'
    core = core[:12]

    # 类目名 / 从标题剥出来的核心词也要过一遍风险词，绝不能带进新文案
    if BAN_RE.search(core) or UNSAFE_RE.search(core):
        core = '闲置好物'
    if BAN_RE.search(brand):
        brand = ''

    return {
        'brand': brand, 'model': model, 'specs': specs, 'spec': ' '.join(specs[:2]),
        'cond': cond, 'condAlt': COND_ALT.get(cond, cond or ''),
        'pack': pack, 'exp': exp, 'core': core,
        'price': str(price or ''), 'unsafe': unsafe,
        'name': (brand + core) if brand else core,
    }


def _fill(tpl: str, f: dict) -> str:
    s = tpl.format(**f)
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'[|｜·,，]{2,}', lambda m: m.group(0)[0], s)   # 合并连续分隔符（避免 "，，")
    s = re.sub(r'\s*([|｜·,，])\s*', r'\1', s)
    s = re.sub(r'[|｜]\s*[|｜]', '｜', s)
    s = re.sub(r'^\s*[|｜·,，。\s]+|[|｜·,，。\s]+$', '', s)
    return s.strip()


def _trim_title(s: str, limit: int = 30) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # 别把半截词留下，回退到最近的分隔符
    m = list(re.finditer(r'[ |｜·,，]', cut))
    if m and len(cut) - m[-1].end() <= 6:
        cut = cut[:m[-1].start()]
    cut = re.sub(r'\s+\d{1,3}$', '', cut)       # 别留半截数字
    return cut.rstrip(' |｜·,，。') or s[:limit]


def _params(f: dict, rotate: int, bullet: bool) -> str:
    rows = []
    if f['brand']:
        rows.append(('品牌', f['brand']))
    if f['model']:
        rows.append(('型号', f['model']))
    if f['specs']:
        rows.append(('规格', ' / '.join(f['specs'])))
    if f['exp']:
        rows.append(('有效期', f['exp']))
    if f['pack']:
        rows.append(('包装', f['pack']))
    if f['cond']:
        rows.append(('成色', f['cond']))
    k = rotate % max(1, len(rows))
    rows = rows[k:] + rows[:k]
    if bullet:
        return '\n'.join('· ' + a + '：' + b for a, b in rows)
    return '；'.join(a + '是 ' + b for a, b in rows) + '。'


def _segments(f: dict, r: int) -> dict:
    n = f['name']
    open_s = OPENERS[r % len(OPENERS)].format(name=n)
    param_s = _params(f, r, bullet=True)
    cond_s = ''
    if f['cond'] or f['pack']:
        bits = [x for x in (f['cond'], f['pack']) if x]
        cond_s = '成色方面，' + '，'.join(bits) + '，具体以图为准。'
    trade_s = TRADES[r % len(TRADES)]
    close_s = CLOSERS[r % len(CLOSERS)]
    return {'open': open_s, 'param': param_s, 'cond': cond_s,
            'trade': trade_s, 'close': close_s}


def _compose(f: dict, struct: str, r: int) -> str:
    """开场固定第一、结尾固定最后，中间三段顺序按商品轮换，读得通又不同构。"""
    g = _segments(f, r)
    mid = ['param', 'cond', 'trade']
    k = r % len(mid)
    mid = mid[k:] + mid[:k]
    seq = [('open', g['open'])] + [(x, g[x]) for x in mid if g[x]] + [('close', g['close'])]

    if struct == '叙述式':
        body = {'param': _params(f, r, bullet=False), 'cond': g['cond'], 'trade': g['trade']}
        parts = [g['open']] + [body[x] for x in mid if body.get(x)] + [g['close']]
        return re.sub(r'\s{2,}', ' ', ' '.join(parts)).strip()

    if struct == '问答式':
        qa = {
            'param': ('Q：规格和配件？', 'A：' + _params(f, r, bullet=False)),
            'cond': ('Q：成色怎么样？', 'A：' + (g['cond'] or '以图为准，需要其它角度可以留言。')),
            'trade': ('Q：怎么发货、能退吗？', 'A：' + g['trade']),
        }
        first = ('Q：具体是什么东西？', 'A：' + f['name'] +
                 (('，' + f['spec']) if f['spec'] else '') + '，闲置转出。')
        pairs = [first] + [qa[x] for x in mid]
        return '\n\n'.join(q + '\n' + a for q, a in pairs) + '\n\n' + g['close']

    labels = {'param': '【物品参数】', 'cond': '【成色包装】',
              'trade': '【交易说明】', 'open': '', 'close': ''}
    if struct == '清单式':
        chunks = []
        for key, val in seq:
            if not val:
                continue
            if key == 'param':
                val = g['param']
            chunks.append(val)
        return '\n\n'.join(chunks)

    # 分栏式
    return '\n\n'.join((labels[key] + '\n' if labels[key] else '') + val
                       for key, val in seq if val)


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a or '', b or '').ratio()


def rewrite(title: str, desc: str, category: str, price: str,
            item_id: str, index: int, seed: str = '', used: set[str] | None = None) -> tuple[str, str, dict]:
    """生成一件商品的新标题 / 新描述。used 为同批次已用标题，用于防撞车。

    高危商品返回空标题 + unsafe 标记，由调用方直接跳过，绝不靠删词把违规品洗白。
    """
    f = extract(title, desc, category, price)
    if f['unsafe']:
        return '', '', {
            'unsafe': True, 'structure': '—', 'titleTpl': -1,
            'titleSim': 0, 'descSim': 0, 'removed': [],
            'warnings': ['原文含医用 / 成人 / 功效类高危词，已拒绝自动重写；请人工判定是否放弃'],
        }
    h = int(hashlib.md5((seed + '|' + str(item_id)).encode('utf-8')).hexdigest()[:8], 16)
    base = (index + h) % 12
    used = used if used is not None else set()
    best = None
    for step in range(24):
        r = base + step
        nt = _trim_title(_fill(TITLE_TPL[r % len(TITLE_TPL)], f))
        nd = _compose(f, STRUCTS[r % len(STRUCTS)], r)
        nt, rm1 = sanitize(nt)
        nd, rm2 = sanitize(nd)
        # 标题绝不能为空：净化过度时用「类目 + 规格 + 成色」兜底
        if not nt or len(nt) < 8:
            nt, _ = sanitize(_trim_title(_fill('{brand}{core} {spec}｜{condAlt} 闲置转出', f)))
        if not nt or len(nt) < 6:
            nt = '闲置' + f['core'] + '转出'
        if len(nd) < 55:
            nd += '\n\n' + '信息以实物和包装为准，拿不准的地方留言问。'
        s_t, s_d = sim(nt, title), sim(nd, desc)
        clash = nt in used
        banned = audit(nt) + audit(nd)
        ok = (len(nt) >= 8 and s_t < 0.60 and s_d < 0.48
              and not clash and not banned)
        cand = (nt, nd, {'structure': STRUCTS[r % len(STRUCTS)],
                         'titleTpl': r % len(TITLE_TPL),
                         'titleSim': round(s_t, 3), 'descSim': round(s_d, 3),
                         'removed': rm1 + rm2, 'warnings': [], 'unsafe': False})
        if ok:
            return cand
        # 挑兜底：撞车 / 违规的候选排到最后
        score = s_t + s_d + (2.0 if clash else 0) + (2.0 if banned else 0)
        if best is None or score < best[0]:
            best = (score, cand)
    _, (nt, nd, meta) = best
    meta['warnings'].append('相似度或撞车检查未完全达标，请人工复核这一件')
    return nt, nd, meta


def rewrite_many(rows: list[dict], seed: str = '') -> list[dict]:
    """rows: [{id,title,desc,category,price}]；返回逐件结果，字段与 workbench 对接。"""
    used: set[str] = set()
    out = []
    for i, row in enumerate(rows):
        nt, nd, meta = rewrite(row.get('title', ''), row.get('desc', ''),
                               row.get('category', ''), row.get('price', ''),
                               row.get('id', ''), i, seed, used)
        if nt:
            used.add(nt)
        out.append({'id': row.get('id', ''), 'title': nt, 'desc': nd, 'meta': meta})
    return out
