"""记录块（Phase 1）：观察池 + 买卖记录 + 持仓盈亏 + 宏观看板。
存储：本地 SQLite (invest.db)，与网页同目录，便于随文件夹整体拷贝。
数据：持仓最新价、宏观指标均走 AkShare（东方财富源，与三大表同源）。
"""
import os
import time
import json
import re
import sqlite3
import requests
import akshare as ak
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'invest.db')
STOCK_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_cache.json')
HK_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hk_stock_cache.json')
MACRO_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'macro_cache.json')
MACRO_SCHEMA = 2  # 缓存结构版本；改动历史窗口/结构时 +1，旧缓存自动作废并重新联网

# ---------- 初始化 ----------
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        name TEXT,
        market TEXT,
        reason TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        name TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),  -- 当前时间（自动）
        current_valuation REAL,   -- 当前估值（手动）
        sell_mcap_3y REAL,        -- 三年内卖点市值（手动）
        buy_point_3y REAL,        -- 三年内买点（手动）
        notes TEXT
    )''')
    conn.commit()
    conn.close()

# ---------- 观察池 ----------
def add_watch(code, name=None, market=None, reason=None):
    conn = _conn(); c = conn.cursor()
    c.execute('INSERT INTO watchlist (code,name,market,reason) VALUES (?,?,?,?)',
              (code, name, market, reason))
    conn.commit(); rid = c.lastrowid; conn.close()
    return rid

def list_watch():
    conn = _conn(); c = conn.cursor()
    c.execute('SELECT * FROM watchlist ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return rows

def del_watch(id):
    conn = _conn(); c = conn.cursor()
    c.execute('DELETE FROM watchlist WHERE id=?', (id,))
    conn.commit(); conn.close()

def watch_reason_map():
    return {w['code']: w['reason'] for w in list_watch()}

# ---------- 股票自动补全（离线缓存） ----------
_stock_cache = None

def load_stock_cache():
    global _stock_cache
    if _stock_cache is None:
        try:
            with open(STOCK_CACHE_PATH, encoding='utf-8') as f:
                _stock_cache = json.load(f)
        except Exception:
            _stock_cache = []
    return _stock_cache

def load_hk_stock_cache():
    """加载港股名称缓存（港股通列表，由 build_hk_cache.py 生成）。返回 {code: name}。"""
    if not os.path.exists(HK_CACHE_PATH):
        return {}
    try:
        with open(HK_CACHE_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d.get('code_to_name', {})
    except Exception:
        return {}


def search_stocks(q, limit=20):
    """按名称包含 / 代码前缀 / 代码包含 模糊搜索；返回 [{code,name,market}]。
    同时覆盖 A 股（离线缓存）与港股（港股通离线缓存）。"""
    q = (q or '').strip()
    if not q:
        return []
    ql = q.lower()
    out = []
    # A 股
    for it in load_stock_cache():
        code = it.get('code', '')
        name = it.get('name', '')
        if ql in name.lower() or code.startswith(q) or ql in code.lower():
            out.append({'code': code, 'name': name, 'market': 'A股'})
            if len(out) >= limit:
                break
    # 港股（仍可能补充，直到达到 limit）
    hk = load_hk_stock_cache()
    for code, name in hk.items():
        if len(out) >= limit:
            break
        if ql in name.lower() or code.startswith(q) or ql in code.lower():
            out.append({'code': code, 'name': name, 'market': '港股'})
    return out

# ---------- 买卖计划 / 估值记录 ----------
def add_plan(code, name=None, current_valuation=None, sell_mcap_3y=None, buy_point_3y=None, notes=None):
    conn = _conn(); c = conn.cursor()
    c.execute('''INSERT INTO plans (code,name,current_valuation,sell_mcap_3y,buy_point_3y,notes)
                 VALUES (?,?,?,?,?,?)''',
              (code, name, current_valuation, sell_mcap_3y, buy_point_3y, notes))
    conn.commit(); rid = c.lastrowid; conn.close()
    return rid

def list_plans():
    conn = _conn(); c = conn.cursor()
    c.execute('SELECT * FROM plans ORDER BY created_at DESC, id DESC')
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    # 当前市值（自动，实时行情）
    for r in rows:
        r['current_mcap'] = get_market_cap(r['code'])
    return rows

def del_plan(id):
    conn = _conn(); c = conn.cursor()
    c.execute('DELETE FROM plans WHERE id=?', (id,))
    conn.commit(); conn.close()

# ---------- 当前市值（自动） ----------
_mcap_cache = {}  # code -> (timestamp, mcap)

def _norm_code(code):
    s = str(code).upper()
    for p in ('SH', 'SZ', 'BJ'):
        if s.startswith(p):
            s = s[2:]
    return s

def _prefix(code):
    """6 位代码 -> 腾讯/新浪前缀（sh/sz）。上交所 6/9 开头，深交所 0/3 开头。"""
    s = _norm_code(code)
    if len(s) != 6:
        return None
    if s[0] in '69':
        return 'sh' + s
    if s[0] in '03':
        return 'sz' + s
    return None

def _ak_with_retry(fn, tries=3, delay=1.0):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(delay)
    return None

def get_market_cap(code):
    """当前总市值（元）。

    支持 A 股（6 位）与港股（5 位）。
    A 股主源：腾讯实时行情 qt.gtimg.cn，总市值（亿元，字段 45）→ 元；兜底新浪日线。
    港股主源：腾讯行情 hk 前缀，总市值（亿元，字段 45）→ 元。
    取不到返回 None。
    """
    s = _norm_code(code)
    if len(s) == 5:
        cache_key = 'HK' + s
        now = time.time()
        if cache_key in _mcap_cache and now - _mcap_cache[cache_key][0] < 300:
            return _mcap_cache[cache_key][1]
        mcap = None
        try:
            r = requests.get('https://qt.gtimg.cn/q=hk' + s, timeout=8)
            r.encoding = 'gbk'
            seg = r.text.split('"')
            if len(seg) >= 2:
                f = seg[1].split('~')
                if len(f) > 45:
                    v = f[45]
                    if v not in ('', '--', '0', None) and re.match(r'^\d', v):
                        mcap = float(v) * 1e8
        except Exception:
            mcap = None
        _mcap_cache[cache_key] = (now, mcap)
        return mcap
    if len(s) != 6 or s[0] not in '036':
        return None
    now = time.time()
    if s in _mcap_cache and now - _mcap_cache[s][0] < 300:
        return _mcap_cache[s][1]
    mcap = None
    # 主：腾讯行情总市值（亿元，字段 45）
    sym = _prefix(s)
    if sym:
        try:
            r = requests.get('https://qt.gtimg.cn/q=' + sym, timeout=8)
            r.encoding = 'gbk'
            seg = r.text.split('"')
            if len(seg) >= 2:
                f = seg[1].split('~')
                if len(f) > 45:
                    v = f[45]
                    if v not in ('', '--', '0', None) and re.match(r'^\d', v):
                        mcap = float(v) * 1e8
        except Exception:
            mcap = None
    # 兜底：新浪日线（流通市值）
    if not mcap or mcap <= 0:
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust='')
            last = df.iloc[-1]
            mcap = float(last['close']) * float(last['outstanding_share'])
        except Exception:
            mcap = None
    _mcap_cache[s] = (now, mcap)
    return mcap

# ---------- 宏观看板 ----------
INDICATORS = [
    # ---------------- 中国 ----------------
    {'key': 'cn_gdp', 'region': '中国', 'name': 'GDP 同比', 'unit': '%',
     'src': 'macro_china_gdp', 'date_col': '季度', 'val_col': '国内生产总值-同比增长'},
    {'key': 'cn_cpi', 'region': '中国', 'name': 'CPI 同比', 'unit': '%',
     'src': 'macro_china_cpi', 'date_col': '月份', 'val_col': '全国-同比增长'},
    {'key': 'cn_ppi', 'region': '中国', 'name': 'PPI 同比', 'unit': '%',
     'src': 'macro_china_ppi', 'date_col': '月份', 'val_col': '当月同比增长'},
    {'key': 'cn_m2', 'region': '中国', 'name': 'M2 同比', 'unit': '%',
     'src': 'macro_china_money_supply', 'date_col': '月份', 'val_col': '货币和准货币(M2)-同比增长'},
    {'key': 'cn_m1', 'region': '中国', 'name': 'M1 同比', 'unit': '%',
     'src': 'macro_china_money_supply', 'date_col': '月份', 'val_col': '货币(M1)-同比增长'},
    {'key': 'cn_retail', 'region': '中国', 'name': '社零 同比', 'unit': '%',
     'src': 'macro_china_consumer_goods_retail', 'date_col': '月份', 'val_col': '同比增长'},
    {'key': 'cn_pmi', 'region': '中国', 'name': '制造业PMI', 'unit': '',
     'src': 'macro_china_pmi', 'date_col': '月份', 'val_col': '制造业-指数'},
    {'key': 'cn_lpr1y', 'region': '中国', 'name': 'LPR 1Y', 'unit': '%',
     'src': 'macro_china_lpr', 'date_col': 'TRADE_DATE', 'val_col': 'LPR1Y'},
    {'key': 'cn_lpr5y', 'region': '中国', 'name': 'LPR 5Y', 'unit': '%',
     'src': 'macro_china_lpr', 'date_col': 'TRADE_DATE', 'val_col': 'LPR5Y'},
    {'key': 'cn_cgb10y', 'region': '中国', 'name': '国债10Y', 'unit': '%',
     'src': 'bond_zh_us_rate', 'date_col': '日期', 'val_col': '中国国债收益率10年'},
    {'key': 'cn_cgb_spread', 'region': '中国', 'name': '国债10Y-2Y利差', 'unit': '%',
     'src': 'bond_zh_us_rate', 'date_col': '日期', 'val_col': '中国国债收益率10年-2年'},
    # ---------------- 美国 ----------------
    {'key': 'us_10y', 'region': '美国', 'name': '10Y 国债收益率', 'unit': '%',
     'src': 'bond_zh_us_rate', 'date_col': '日期', 'val_col': '美国国债收益率10年'},
    {'key': 'us_2y', 'region': '美国', 'name': '2Y 国债收益率', 'unit': '%',
     'src': 'bond_zh_us_rate', 'date_col': '日期', 'val_col': '美国国债收益率2年'},
    {'key': 'us_spread', 'region': '美国', 'name': '国债10Y-2Y利差', 'unit': '%',
     'src': 'bond_zh_us_rate', 'date_col': '日期', 'val_col': '美国国债收益率10年-2年'},
    {'key': 'us_gdp', 'region': '美国', 'name': 'GDP 年增率', 'unit': '%',
     'src': 'bond_zh_us_rate', 'date_col': '日期', 'val_col': '美国GDP年增率'},
    {'key': 'us_cpi', 'region': '美国', 'name': 'CPI 同比', 'unit': '%',
     'src': 'macro_usa_cpi_monthly', 'date_col': '日期', 'val_col': '今值'},
    {'key': 'us_core_pce', 'region': '美国', 'name': '核心PCE', 'unit': '%',
     'src': 'macro_usa_core_pce_price', 'date_col': '日期', 'val_col': '今值'},
    {'key': 'us_unemp', 'region': '美国', 'name': '失业率', 'unit': '%',
     'src': 'macro_usa_unemployment_rate', 'date_col': '日期', 'val_col': '今值'},
    {'key': 'us_nfp', 'region': '美国', 'name': '非农就业(万)', 'unit': '万',
     'src': 'macro_usa_non_farm', 'date_col': '日期', 'val_col': '今值'},
    {'key': 'us_ism', 'region': '美国', 'name': 'ISM PMI', 'unit': '',
     'src': 'macro_usa_ism_pmi', 'date_col': '日期', 'val_col': '今值'},
]

def _fetch_bond():
    return ak.bond_zh_us_rate()

def get_macro_indicators(force=False):
    """返回 20 个宏观指标最新值。force=False 时优先读本地缓存（零联网）。"""
    mc = _mc_load()
    if not force and mc.get('indicators'):
        return mc['indicators']
    out = _fetch_indicators()
    # 若全部拉取失败（如离线），不写缓存，避免污染本地；下次仍会尝试联网
    if any(it.get('value') is not None for it in out):
        mc['indicators'] = out
        _mc_save()
    return out

def _ind_meta(ind):
    return {'key': ind['key'], 'region': ind['region'], 'name': ind['name'], 'unit': ind['unit']}

# ---------- 宏观数据本地持久缓存 ----------
# 历史数据（固定不变）与最新值都落盘到项目目录的 macro_cache.json。
# 首次联网加载后永久本地保存；平时读取零联网，仅“刷新数据”时联网更新最新值。
def _mc_load():
    global _mc
    if _mc is None:
        try:
            with open(MACRO_CACHE_PATH, encoding='utf-8') as f:
                _mc = json.load(f)
        except Exception:
            _mc = {}
        # 旧缓存（如历史窗口为 10 年）作废，本次访问会重新联网并写入新结构
        if _mc.get('schema') != MACRO_SCHEMA:
            _mc = {}
    return _mc

def _mc_save():
    global _mc
    if _mc is None:
        return
    try:
        _mc['schema'] = MACRO_SCHEMA   # 写入结构版本，便于后续升级时自动失效
        tmp = MACRO_CACHE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_mc, f, ensure_ascii=False)
        os.replace(tmp, MACRO_CACHE_PATH)   # 原子写，避免半截文件
    except Exception:
        pass

def _fetch_indicators():
    """联网拉取 20 个宏观指标最新值（内部函数，不含缓存）"""
    out = []
    bond_df = None
    for ind in INDICATORS:
        try:
            if ind['key'] == 'us_cpi':
                cdates, cseries = _usa_cpi_yoy_series()
                if cseries:
                    out.append({**_ind_meta(ind), 'value': cseries[-1], 'date': cdates[-1]})
                else:
                    out.append({**_ind_meta(ind), 'value': None, 'date': None})
                continue
            if ind['src'] == 'bond_zh_us_rate':
                if bond_df is None:
                    bond_df = _fetch_bond()
                df = bond_df
            else:
                df = getattr(ak, ind['src'])()
            sub = df[[ind['date_col'], ind['val_col']]].dropna(subset=[ind['val_col']])
            if len(sub) == 0:
                out.append({**_ind_meta(ind), 'value': None, 'date': None}); continue
            sub = sub.sort_values(ind['date_col'], na_position='last')
            last = sub.iloc[-1]
            try:
                val = float(last[ind['val_col']])
            except Exception:
                val = None
            out.append({**_ind_meta(ind), 'value': val, 'date': str(last[ind['date_col']])})
        except Exception as e:
            out.append({**_ind_meta(ind), 'value': None, 'date': None, 'error': str(e)})
    return out

def _fetch_curve():
    """联网拉取美债收益率曲线（内部函数，不含缓存）"""
    df = _fetch_bond()
    df = df.dropna(subset=['美国国债收益率10年'])
    df['日期'] = pd.to_datetime(df['日期'], errors='coerce')
    df = df.dropna(subset=['日期']).sort_values('日期')
    cutoff = pd.Timestamp.today() - pd.DateOffset(years=30)
    df = df[df['日期'] >= cutoff]
    df = df.set_index('日期').resample('W').last().dropna(subset=['美国国债收益率10年']).reset_index()
    dates = [d.strftime('%Y-%m-%d') for d in df['日期']]
    series = {}
    for tenor, col in [('2Y', '美国国债收益率2年'), ('5Y', '美国国债收益率5年'),
                       ('10Y', '美国国债收益率10年'), ('30Y', '美国国债收益率30年')]:
        if col in df.columns:
            series[tenor] = [None if pd.isna(v) else round(float(v), 3) for v in df[col]]
    return {'dates': dates, 'series': series}

def _fetch_history(key):
    """联网拉取单个指标近30年周线历史（内部函数，不含缓存）"""
    ind = next((i for i in INDICATORS if i['key'] == key), None)
    if not ind:
        return {'key': key, 'error': 'unknown indicator', 'dates': [], 'series': []}
    try:
        if key == 'us_cpi':
            cdates, cseries = _usa_cpi_yoy_series()
            s = pd.Series(cseries, index=pd.to_datetime(cdates)).dropna()
            cutoff = pd.Timestamp.today() - pd.DateOffset(years=30)
            s = s[s.index >= cutoff]
            s = s.resample('W').last().dropna()
            dates = [d.strftime('%Y-%m-%d') for d in s.index]
            series = [None if pd.isna(v) else round(float(v), 4) for v in s.values]
            data = {'key': key, 'name': ind['region'] + '·' + ind['name'],
                    'unit': ind['unit'], 'dates': dates, 'series': series}
        else:
            if ind['src'] == 'bond_zh_us_rate':
                df = _fetch_bond()
            else:
                df = getattr(ak, ind['src'])()
            sub = df[[ind['date_col'], ind['val_col']]].dropna(subset=[ind['val_col']]).copy()
            sub[ind['date_col']] = sub[ind['date_col']].apply(_parse_macro_date)
            sub = sub.dropna(subset=[ind['date_col']]).sort_values(ind['date_col'])
            cutoff = pd.Timestamp.today() - pd.DateOffset(years=30)
            sub = sub[sub[ind['date_col']] >= cutoff]
            sub = sub.set_index(ind['date_col']).resample('W').last().dropna(subset=[ind['val_col']]).reset_index()
            dates = [d.strftime('%Y-%m-%d') for d in sub[ind['date_col']]]
            series = [None if pd.isna(v) else round(float(v), 4) for v in sub[ind['val_col']]]
            data = {'key': key, 'name': ind['region'] + '·' + ind['name'],
                    'unit': ind['unit'], 'dates': dates, 'series': series}
    except Exception as e:
        data = {'key': key, 'name': ind['region'] + '·' + ind['name'],
                'unit': ind['unit'], 'error': str(e), 'dates': [], 'series': []}
    return data

_mc = None  # 内存镜像（进程内加速），首次 _mc_load 时从磁盘载入

def get_treasury_curve(force=False):
    """返回美债收益率曲线。force=False 时优先读本地缓存（零联网）。"""
    mc = _mc_load()
    if not force and mc.get('curve'):
        return mc['curve']
    data = _fetch_curve()
    if data.get('series'):   # 至少有一条期限序列才算成功
        mc['curve'] = data
        _mc_save()
    return data

def refresh_macro():
    """联网刷新最新值 + 曲线（历史固定不再重复下载）；本地缺失的历史补拉一次。"""
    get_macro_indicators(force=True)
    get_treasury_curve(force=True)
    mc = _mc_load()
    hist = mc.setdefault('histories', {})
    for ind in INDICATORS:
        if ind['key'] not in hist:
            hist[ind['key']] = _fetch_history(ind['key'])
    _mc_save()
    return True

# ---------- 单个指标近 30 年周线历史 ----------
def _parse_macro_date(s):
    """把 AkShare 宏观接口的中文日期解析成 Timestamp。
    GDP：'2026年第1季度' / '2026年第1-2季度' -> 季度末
    CPI/M2：'2008年03月份' -> 2008-03
    其它：尝试直接解析（'2024-01-01' 等）。
    """
    s = str(s).strip()
    m = re.search(r'(\d{4})年.*?第?(\d)季度', s)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        # 取区间内最后一个季度
        mm = re.findall(r'第(\d)季度', s)
        if mm:
            q = int(mm[-1])
        return pd.Timestamp(y, (q * 3), 1) + pd.offsets.QuarterEnd(0)
    m = re.search(r'(\d{4})年(\d{1,2})月', s)
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    try:
        return pd.to_datetime(s, errors='coerce')
    except Exception:
        return pd.NaT

def _usa_cpi_yoy_series():
    """由 macro_usa_cpi_monthly（月率）推算美国 CPI 同比(年率)。返回 (dates, series) 月度序列。"""
    try:
        df = ak.macro_usa_cpi_monthly()
    except Exception:
        return [], []
    if '商品' in df.columns:
        df = df[df['商品'] == '美国CPI月率']
    df = df.dropna(subset=['今值']).copy()
    df['_d'] = df['日期'].apply(_parse_macro_date)
    df = df.dropna(subset=['_d']).sort_values('_d')
    if len(df) < 12:
        return [], []
    mom = df.set_index('_d')['今值'].astype(float)
    # 同比 = 过去 12 个月环比连乘 - 1
    ratio = mom.add(100).rolling(12, min_periods=12).apply(lambda x: float(x.prod()), raw=True) / (100 ** 12)
    yoy = (ratio - 1) * 100
    yoy = yoy.dropna()
    dates = [d.strftime('%Y-%m-%d') for d in yoy.index]
    series = [round(float(v), 4) for v in yoy.values]
    return dates, series

def get_indicator_history(key, force=False):
    """返回单个指标近30年周线历史。force=False 时优先读本地缓存（零联网）。"""
    mc = _mc_load()
    hist = mc.setdefault('histories', {})
    if not force and key in hist:
        return hist[key]
    data = _fetch_history(key)
    if not data.get('error'):   # 拉取失败不写缓存，避免污染
        hist[key] = data
        _mc_save()
    return data

init_db()
