"""Phase 3 选股：估值/质量/成长 指标计算（A股 + 港股）。

设计要点（已实测验证）：
- 估值（PE/PB/股息率）因东方财富「行情」快照接口在用户网络被拦，改为自算：
    PE = 总市值 ÷ 归母净利润(最新年报)
    PB = 总市值 ÷ 股东权益合计(最新年报)
    股息率 = 每股股息 ÷ 当前价
  - 总市值/当前价：腾讯行情 qt.gtimg.cn（与行情不同的域名，用户网络可用）
  - 净利润/净资产/ROE/负债率/增速：A股走 AkShare 财务摘要（数据中心，可用）；
    港股走港交所报表（data._fetch_hk_statement，数值已为 HKD）
- 质量(ROE/资产负债率) + 成长(营收/利润增速)：
    A股直接取财务摘要对应行；港股由报表按公式计算（ROE=np/eq，负债率=负债/资产，增速=同比）
- 该模块只做「计算」，缓存构建见 build_valuation_cache.py，筛选见 app.py 的 /screen。
"""
import socket
import re
import requests
import akshare as ak
import data  # 复用港股报表抓取

socket.setdefaulttimeout(15)


def _gtimg_a(code):
    sym = ('sh' + code) if code[0] in '69' else ('sz' + code)
    r = requests.get('https://qt.gtimg.cn/q=' + sym, timeout=8)
    r.encoding = 'gbk'
    return r.text.split('"')[1].split('~')


def _gtimg_hk(code5):
    r = requests.get('https://qt.gtimg.cn/q=hk' + code5, timeout=8)
    r.encoding = 'gbk'
    return r.text.split('"')[1].split('~')


def _abs_latest_annual(sym, name):
    """取财务摘要某指标「最新年报(12-31)」数值；缺失返回 None。"""
    try:
        df = ak.stock_financial_abstract(symbol=sym)
    except Exception:
        return None
    cols = [c for c in df.columns if c not in ('选项', '指标')]
    annual = sorted([c for c in cols if c.endswith('1231')], reverse=True)
    if not annual:
        return None
    row = df[df['指标'] == name]
    if len(row) == 0:
        return None
    for c in annual:
        v = row.iloc[0][c]
        try:
            fv = float(v)
            if fv == 0:
                continue
            return fv
        except Exception:
            continue
    return None


def _abs_latest_annual_col(sym):
    """返回财务摘要最新年报列名（如 '20251231'）。"""
    df = ak.stock_financial_abstract(symbol=sym)
    cols = [c for c in df.columns if c not in ('选项', '指标')]
    annual = sorted([c for c in cols if c.endswith('1231')], reverse=True)
    return annual[0] if annual else None


def get_metrics_a(code):
    """A股单只指标。返回 dict，失败返回 None。"""
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    try:
        f = _gtimg_a(code)
        name = f[1]
        price = float(f[3])
        mcap = float(f[45]) * 1e8  # 总市值字段 45，单位亿元
    except Exception:
        return None
    sym = ('SH' + code) if code[0] in '69' else ('SZ' + code)
    np_ = _abs_latest_annual(sym, '归母净利润')
    eq = _abs_latest_annual(sym, '股东权益合计(净资产)')
    roe = _abs_latest_annual(sym, '净资产收益率(ROE)')
    debt = _abs_latest_annual(sym, '资产负债率')
    rg = _abs_latest_annual(sym, '营业总收入增长率')
    pg = _abs_latest_annual(sym, '归属母公司净利润增长率')
    asof = _abs_latest_annual_col(sym)
    # 股息率：最近一次实施的「每10股派X元」→ 每股 = X/10
    dy = None
    dps = None
    try:
        dd = ak.stock_history_dividend_detail(symbol=code, indicator='分红')
        dd = dd[dd['进度'] == '实施'].sort_values('除权除息日', ascending=False)
        if len(dd):
            dps = float(dd.iloc[0]['派息']) / 10.0
            dy = dps / price * 100 if price else None
    except Exception:
        pass
    pe = mcap / np_ if (np_ not in (None, 0)) else None
    pb = mcap / eq if (eq not in (None, 0)) else None
    return {
        'code': code, 'name': name, 'market': 'A股', 'currency': 'CNY',
        'price': price, 'mcap': mcap, 'pe': pe, 'pb': pb, 'dy': dy,
        'roe': roe, 'debt': debt, 'rev_g': rg, 'profit_g': pg,
        'asof': asof, 'dps': dps,
    }


def _hk_dividend_per_share(code5):
    """港股最近财政年度每股股息（港元）；解析 分红方案『每股派港币X元』。"""
    try:
        dd = ak.stock_hk_dividend_payout_em(symbol=code5)
        if dd is None or len(dd) == 0:
            return None
        # 取最新财政年度
        yrs = sorted(dd['财政年度'].dropna().unique(), reverse=True)
        for y in yrs:
            row = dd[dd['财政年度'] == y].iloc[0]
            s = str(row.get('分红方案', ''))
            m = re.search(r'每股派[港人民币]*\s*([\d.]+)\s*元', s)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


def get_metrics_hk(code5):
    """港股单只指标。返回 dict，失败返回 None。"""
    code5 = code5.strip().zfill(5)
    if not re.match(r'^\d{5}$', code5):
        return None
    try:
        f = _gtimg_hk(code5)
        name = f[1]
        price = float(f[3])
        mcap = float(f[45]) * 1e8  # 腾讯行情港股总市值(亿元) → 元
    except Exception:
        return None
    try:
        h = data._fetch_hk_statement(code5, 'balance')
        inc = data._fetch_hk_statement(code5, 'income')
    except Exception:
        return None
    yrs = [y for y in inc if inc[y].get('PARENT_NETPROFIT')]
    if not yrs:
        return None
    y = max(yrs)
    y_prev = max([yy for yy in inc if yy < y], default=None)
    np_ = inc[y].get('PARENT_NETPROFIT')
    eq = h[y].get('TOTAL_EQUITY')
    ta = h[y].get('TOTAL_ASSETS')
    tl = h[y].get('TOTAL_LIABILITIES')
    rev = inc[y].get('OPERATE_INCOME')
    rev_prev = inc[y_prev].get('OPERATE_INCOME') if y_prev else None
    np_prev = inc[y_prev].get('PARENT_NETPROFIT') if y_prev else None
    roe = (np_ / eq * 100) if (np_ not in (None, 0) and eq) else None
    debt = (tl / ta * 100) if (tl not in (None, 0) and ta) else None
    rg = ((rev - rev_prev) / rev_prev * 100) if (rev and rev_prev not in (None, 0)) else None
    pg = ((np_ - np_prev) / np_prev * 100) if (np_ and np_prev not in (None, 0)) else None
    dps = _hk_dividend_per_share(code5)
    dy = dps / price * 100 if (dps and price) else None
    pe = mcap / np_ if (np_ not in (None, 0)) else None
    pb = mcap / eq if (eq not in (None, 0)) else None
    return {
        'code': code5, 'name': name, 'market': '港股', 'currency': 'HKD',
        'price': price, 'mcap': mcap, 'pe': pe, 'pb': pb, 'dy': dy,
        'roe': roe, 'debt': debt, 'rev_g': rg, 'profit_g': pg,
        'asof': y, 'dps': dps,
    }


def get_metrics(code, market=None):
    """统一入口：根据市场路由；market 省略时按代码猜测（5位=港股）。"""
    if market == '港股' or (market is None and re.match(r'^\d{5}$', code.strip())):
        return get_metrics_hk(code)
    return get_metrics_a(code)


if __name__ == '__main__':
    for c, m in [('600519', 'A股'), ('601899', 'A股'), ('00700', '港股')]:
        r = get_metrics(c, m)
        print(c, r)
