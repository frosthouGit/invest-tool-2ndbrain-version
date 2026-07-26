"""一次性构建「同行」离线缓存 peers_cache.json（可断点续跑）。
数据源：akshare index_realtime_sw（申万二级行业清单，124 个）+ index_component_sw（各行业成分股，含名称）。
两者均走东方财富数据中心（与三大表同源），可在被拦截 Eastmoney 行情接口的网络下使用。
缓存结构：
  code_to_industry : 6位股票代码 -> {industry_code, industry_name}
  industry_to_peers: 行业代码 -> [{code, name}, ...]
  code_to_name     : 6位代码 -> 名称
运行：python build_peers_cache.py  （仅需联网一次；产物随工具分发，运行时零网络依赖）
每次拉完一个行业就落盘，进程被中断后重跑会自动跳过已完成的行业。
"""
import ssl, urllib3, json, os, time, datetime, socket, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings()
socket.setdefaulttimeout(25)  # 防止个别行业请求无限挂起，超时后由重试逻辑跳过
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'peers_cache.json')
LOG = os.path.join(HERE, 'peers_build.log')


def log(*a):
    msg = ' '.join(str(x) for x in a)
    print(msg, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')


def save():
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({
            'built': cache.get('built', datetime.date.today().isoformat()),
            'source': '申万二级行业(index_component_sw, 东方财富数据中心)',
            'code_to_industry': code_to_industry,
            'industry_to_peers': industry_to_peers,
            'code_to_name': code_to_name,
            'name_to_code': {name.strip(): code for code, name in code_to_name.items()},
        }, f, ensure_ascii=False)
    os.replace(tmp, OUT)


# ---- 断点续跑 ----
cache = {}
if os.path.exists(OUT):
    try:
        with open(OUT, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        log('resume: 行业', len(cache.get('industry_to_peers', {})), '股票', len(cache.get('code_to_industry', {})))
    except Exception as e:
        log('resume 失败，重建:', e)
        cache = {}

industry_to_peers = cache.get('industry_to_peers', {})
code_to_industry = cache.get('code_to_industry', {})
code_to_name = cache.get('code_to_name', {})

log("1) 拉取申万二级行业清单 (index_realtime_sw) ...")
industries = []
for _ in range(4):
    try:
        df = ak.index_realtime_sw()
        industries = [{'code': str(r['指数代码']), 'name': str(r['指数名称'])} for _, r in df.iterrows()]
        break
    except Exception as e:
        log('   list retry...', repr(e)[:80]); time.sleep(1.5)
if not industries:
    raise SystemExit('无法获取行业清单，请检查网络后重试')
todo = [i for i in industries if i['code'] not in industry_to_peers]
log('   行业总数', len(industries), '待拉取', len(todo))


def _call_index_component_sw(code, timeout=30):
    """在独立守护线程里调用 akshare，硬超时返回 None，避免个别行业请求无限挂起。"""
    box = {}
    def runner():
        try:
            box['r'] = ak.index_component_sw(symbol=code)
        except Exception:
            box['r'] = None
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    return box.get('r')


def fetch(ind):
    d = _call_index_component_sw(ind['code'], timeout=30)
    if d is None:
        return ind['code'], []
    out = []
    try:
        for _, row in d.iterrows():
            sc = str(row['证券代码']).strip()
            sn = str(row['证券名称']).strip()
            if sc and sn and sn != 'nan':
                out.append({'code': sc, 'name': sn})
    except Exception:
        return ind['code'], []
    return ind['code'], []


log("2) 并发拉取各行业成分股（每行业落盘一次，可续跑）...")
ok = 0
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(fetch, i): i for i in todo}
    for k, f in enumerate(as_completed(futs), 1):
        ind = futs[f]
        icode, peers = f.result()
        industry_to_peers[icode] = peers
        if peers:
            ok += 1
        for p in peers:
            code_to_industry.setdefault(p['code'], {'industry_code': icode, 'industry_name': ind['name']})
            code_to_name.setdefault(p['code'], p['name'])
        save()
        if k % 20 == 0 or k == len(todo):
            log(f'   进度 {k}/{len(todo)} 成功 {ok} 已映射股票 {len(code_to_industry)}')
log(f'DONE 成功行业 {ok}/{len(todo)}；覆盖股票 {len(code_to_industry)} 只')
save()
log('已写出', OUT, '大小(KB)=', round(os.path.getsize(OUT) / 1024, 1))
