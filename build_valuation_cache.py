"""Phase 3 选股：估值快照缓存构建（限定池 B = 沪深300 + 港股通）。

运行一次（或偶尔刷新）即可把池内每只股票的估值/质量/成长指标算好存本地，
之后网页筛选秒级出结果，运行期零网络。

用法：
    python build_valuation_cache.py          # 构建/增量续跑
    python build_valuation_cache.py --force  # 全量重建

注意：
- 依赖 screen.py（估值计算）。
- 单只抓取有硬超时保护；已成功的股票会落盘，重跑自动跳过（断点续跑）。
- 港股池来自 hk_stock_cache.json。本脚本运行时会自动检查该缓存是否偏少，
  偏少则自动先跑 build_hk_cache.py 补全港股通列表，无需手动分步。
"""
import os
import sys
import json
import time
import socket
import threading
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screen as screen

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'valuation_cache.json')
HK_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hk_stock_cache.json')
socket.setdefaulttimeout(20)
HARD_TIMEOUT = 45  # 单只硬超时（秒）


def _run_with_timeout(fn, timeout):
    """在守护线程里跑 fn，超时返回 None。"""
    res = [None]
    def _w():
        try:
            res[0] = fn()
        except Exception:
            res[0] = None
    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout)
    return res[0]


def get_universe():
    """返回 [(code, name, market), ...]"""
    out = []
    # A股：沪深300
    try:
        import akshare as ak
        df = ak.index_stock_cons(symbol='000300')
        for _, r in df.iterrows():
            out.append((str(r['品种代码']).strip(), str(r['品种名称']).strip(), 'A股'))
        print(f'沪深300：{len(df)} 只')
    except Exception as e:
        print('沪深300 获取失败：', repr(e)[:120])
    # 港股：hk_stock_cache.json
    try:
        with open(HK_CACHE_PATH, encoding='utf-8') as fh:
            hk = json.load(fh)
        ctn = hk.get('code_to_name', {})
        for code, name in ctn.items():
            out.append((str(code).strip().zfill(5), str(name).strip(), '港股'))
        print(f'港股通：{len(ctn)} 只')
    except Exception as e:
        print('港股缓存读取失败（请先跑 build_hk_cache.py）：', repr(e)[:120])
    return out


def _ensure_hk_cache():
    """港股缓存太稀疏时，自动先跑 build_hk_cache.py 补全（仅在真机联网时生效）。

    返回值：补全后港股数量。即使失败也不影响 A股部分。
    """
    HK_FULL_THRESHOLD = 400  # 完整港股通约 500+；低于此值视为“偏少”，自动补全
    try:
        with open(HK_CACHE_PATH, encoding='utf-8') as fh:
            hk = json.load(fh)
        n = len(hk.get('code_to_name', {}))
    except Exception:
        n = 0
    if n >= HK_FULL_THRESHOLD:
        print(f'港股缓存已较全（{n} 只），跳过补全。')
        return n
    print(f'港股缓存偏少（当前 {n} 只，完整港股通约 500+）。正在自动补全港股通列表...')
    try:
        import subprocess
        builder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build_hk_cache.py')
        # 用与当前脚本相同的 Python 解释器运行，继承断点续跑/超时保护
        subprocess.run([sys.executable, builder], check=False)
        with open(HK_CACHE_PATH, encoding='utf-8') as fh:
            hk = json.load(fh)
        n2 = len(hk.get('code_to_name', {}))
        print(f'港股缓存补全后：{n2} 只。')
        if n2 <= n:
            print('提示：本次未能拉到更多港股（可能当前网络仍拿不到港股通全量）。'
                  '可稍后在网络更好时单独双击 build_hk_cache.py 再补；A股部分不受影响。')
    except Exception as e:
        print('港股缓存自动补全失败（不影响 A股；可稍后单独运行 build_hk_cache.py）：', repr(e)[:120])
    return n


def load_cache():
    try:
        with open(CACHE_PATH, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {'meta': {}, 'stocks': {}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='全量重建')
    args = ap.parse_args()

    _ensure_hk_cache()  # 港股偏少则自动补全，先于估值计算

    cache = load_cache()
    stocks = cache.get('stocks', {})
    if args.force:
        stocks = {}
        print('全量重建模式')

    universe = get_universe()
    total = len(universe)
    done = sum(1 for c, _, _ in universe if c in stocks)
    print(f'总计 {total} 只，已缓存 {done} 只，待处理 {total - done} 只')

    processed = 0
    for i, (code, name, market) in enumerate(universe, 1):
        if code in stocks:
            continue
        rec = _run_with_timeout(lambda: screen.get_metrics(code, market), HARD_TIMEOUT)
        if rec and rec.get('pe') is not None:
            rec['name'] = name or rec.get('name')  # 以成分表名为准
            stocks[code] = rec
            processed += 1
            if processed % 10 == 0:
                cache['stocks'] = stocks
                cache['meta'] = {'built_at': time.strftime('%Y-%m-%d %H:%M'), 'count': len(stocks)}
                with open(CACHE_PATH, 'w', encoding='utf-8') as fh:
                    json.dump(cache, fh, ensure_ascii=False)
                print(f'  [{i}/{total}] {code} {name} 已存（累计 {len(stocks)}）')
        else:
            print(f'  [{i}/{total}] {code} {name} 失败/超时，跳过')
        # 轻量节流，避免过快请求
        time.sleep(0.05)

    cache['stocks'] = stocks
    cache['meta'] = {'built_at': time.strftime('%Y-%m-%d %H:%M'), 'count': len(stocks)}
    with open(CACHE_PATH, 'w', encoding='utf-8') as fh:
        json.dump(cache, fh, ensure_ascii=False)
    print(f'完成：共缓存 {len(stocks)} 只 → {CACHE_PATH}')


if __name__ == '__main__':
    main()
