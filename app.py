from flask import Flask, request, jsonify, render_template, send_file, Response
import io
import os
import json as _json
import openpyxl
from data import get_peers, get_comparison, build_excel
import record as rec
import qa as qa_mod
import sys, time, threading, subprocess, webbrowser, re

app = Flask(__name__)
# 本地开发：强制每次从磁盘重读模板，且禁止浏览器缓存页面，避免改完 HTML 还得手动硬刷新
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0


@app.after_request
def _no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


VAL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'valuation_cache.json')



@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/peers')
def peers():
    code = request.args.get('code', '')
    try:
        return jsonify(get_peers(code))
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/compare', methods=['POST'])
def compare():
    body = request.get_json(force=True)
    companies = body.get('companies', [])
    years = int(body.get('years', 5))
    try:
        return jsonify(get_comparison(companies, years))
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/export', methods=['POST'])
def export():
    body = request.get_json(force=True)
    try:
        buf = build_excel(body)
        return send_file(
            buf,
            download_name='公司对比.xlsx',
            as_attachment=True,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/record')
def record_page():
    return render_template('record.html')


@app.route('/api/watch', methods=['GET', 'POST', 'DELETE'])
def api_watch():
    if request.method == 'GET':
        return jsonify(rec.list_watch())
    if request.method == 'POST':
        b = request.get_json(force=True)
        rid = rec.add_watch(b.get('code'), b.get('name'), b.get('market'), b.get('reason'))
        return jsonify({'ok': True, 'id': rid})
    # DELETE: ?id= or JSON body
    idv = request.args.get('id') or (request.get_json(force=True, silent=True) or {}).get('id')
    if idv:
        rec.del_watch(int(idv))
    return jsonify({'ok': True})


@app.route('/api/stocks')
def api_stocks():
    q = request.args.get('q', '')
    return jsonify(rec.search_stocks(q))


@app.route('/api/plans', methods=['GET', 'POST', 'DELETE'])
def api_plans():
    if request.method == 'GET':
        return jsonify(rec.list_plans())
    if request.method == 'POST':
        b = request.get_json(force=True)
        rid = rec.add_plan(b.get('code'), b.get('name'),
                           b.get('current_valuation'), b.get('sell_mcap_3y'),
                           b.get('buy_point_3y'), b.get('notes'))
        return jsonify({'ok': True, 'id': rid})
    idv = request.args.get('id') or (request.get_json(force=True, silent=True) or {}).get('id')
    if idv:
        rec.del_plan(int(idv))
    return jsonify({'ok': True})


@app.route('/api/macro')
def api_macro():
    return jsonify({'indicators': rec.get_macro_indicators(), 'curve': rec.get_treasury_curve()})


@app.route('/api/macro/refresh', methods=['POST'])
def api_macro_refresh():
    rec.refresh_macro()
    return jsonify({'ok': True})


@app.route('/api/macro/history')
def api_macro_history():
    key = request.args.get('key', '')
    try:
        return jsonify(rec.get_indicator_history(key))
    except Exception as e:
        return jsonify({'key': key, 'error': str(e), 'dates': [], 'series': []})


@app.route('/screen')
def screen_page():
    return render_template('screen.html')


def _load_val_cache():
    try:
        with open(VAL_CACHE, encoding='utf-8') as f:
            c = _json.load(f)
        return c.get('stocks', {}), c.get('meta', {})
    except Exception:
        return {}, {}


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _screen_filter(body):
    """按筛选条件过滤估值缓存，返回排序后的结果列表。"""
    stocks, meta = _load_val_cache()
    f = body.get('filters', {})
    market = f.get('market', 'all')
    pe_min, pe_max = _num(f.get('pe_min')), _num(f.get('pe_max'))
    pb_max = _num(f.get('pb_max'))
    dy_min = _num(f.get('dy_min'))
    roe_min = _num(f.get('roe_min'))
    debt_max = _num(f.get('debt_max'))
    revg_min = _num(f.get('revg_min'))
    pg_min = _num(f.get('profitg_min'))
    rows = []
    for code, r in stocks.items():
        if market != 'all' and r.get('market') != market:
            continue
        pe, pb, dy, roe, debt, rg, pg = (r.get('pe'), r.get('pb'), r.get('dy'),
                                         r.get('roe'), r.get('debt'), r.get('rev_g'), r.get('profit_g'))
        if pe_min is not None and (pe is None or pe < pe_min):
            continue
        if pe_max is not None and (pe is None or pe > pe_max):
            continue
        if pb_max is not None and (pb is None or pb > pb_max):
            continue
        if dy_min is not None and (dy is None or dy < dy_min):
            continue
        if roe_min is not None and (roe is None or roe < roe_min):
            continue
        if debt_max is not None and (debt is None or debt > debt_max):
            continue
        if revg_min is not None and (rg is None or rg < revg_min):
            continue
        if pg_min is not None and (pg is None or pg < pg_min):
            continue
        rows.append(r)
    sort_by = body.get('sort_by', 'dy') or 'dy'
    desc = body.get('desc', True)
    rows.sort(key=lambda r: (r.get(sort_by) is not None, r.get(sort_by) or 0), reverse=desc)
    return rows, meta


@app.route('/api/screen', methods=['POST'])
def api_screen():
    try:
        rows, meta = _screen_filter(request.get_json(force=True))
        return jsonify({'count': len(rows), 'rows': rows, 'built_at': meta.get('built_at'), 'meta': meta})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/screen/export', methods=['POST'])
def api_screen_export():
    try:
        body = request.get_json(force=True)
        rows, _ = _screen_filter(body)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '选股结果'
        headers = ['代码', '名称', '市场', '货币', '当前价', '总市值(亿)', 'PE', 'PB',
                   '股息率(%)', 'ROE(%)', '资产负债率(%)', '营收增速(%)', '利润增速(%)', '数据截至']
        ws.append(headers)
        for r in rows:
            cur = r.get('currency', 'CNY')
            mcap_yi = (r.get('mcap') or 0) / 1e8
            ws.append([
                r.get('code'), r.get('name'), r.get('market'), cur,
                round(r.get('price') or 0, 2),
                round(mcap_yi, 1),
                _r(r.get('pe')), _r(r.get('pb')), _r(r.get('dy')),
                _r(r.get('roe')), _r(r.get('debt')), _r(r.get('rev_g')), _r(r.get('profit_g')),
                r.get('asof'),
            ])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, download_name='选股结果.xlsx', as_attachment=True,
                        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        return jsonify({'error': str(e)})


def _r(v):
    return round(v, 2) if isinstance(v, (int, float)) else None


@app.route('/qa')
def qa_page():
    return render_template('qa.html')


@app.route('/api/qa/config', methods=['GET'])
def qa_config_get():
    prov = qa_mod.get_provider()
    providers = []
    for pid, cfg in qa_mod.PROVIDERS.items():
        providers.append({
            'id': pid,
            'name': cfg['name'],
            'key_hint': cfg['key_hint'],
            'models': [{'id': m['id'], 'label': m['label'], 'pricing': m['pricing']}
                       for m in cfg['models']],
        })
    keys = {pid: qa_mod.has_key(pid) for pid in qa_mod.PROVIDERS}
    return jsonify(provider=prov, model=qa_mod.get_model(), providers=providers,
                   has_key=keys[prov], keys=keys, allow_general=qa_mod.get_allow_general())


@app.route('/api/qa/config', methods=['POST'])
def qa_config_post():
    body = request.get_json(force=True) or {}
    provider = (body.get('provider') or '').strip()
    if provider not in qa_mod.PROVIDERS:
        provider = qa_mod.DEFAULT_PROVIDER
    model = (body.get('model') or '').strip()
    if not any(m['id'] == model for m in qa_mod.PROVIDERS[provider]['models']):
        model = qa_mod.PROVIDERS[provider]['models'][0]['id']
    qa_mod.save_provider_model(provider, model)
    key = (body.get('key') or '').strip()
    if key:
        qa_mod.save_key(provider, key)
    # 允许结合公开常识补充（财报数据不足时）
    if 'allow_general' in body:
        qa_mod.set_allow_general(bool(body.get('allow_general')))
    return jsonify(ok=True)


@app.route('/api/qa/library')
def qa_library():
    return jsonify(qa_mod.list_library())


@app.route('/api/qa/upload', methods=['POST'])
def qa_upload():
    company = (request.form.get('company') or '').strip()
    year = (request.form.get('year') or '').strip()
    files = request.files.getlist('files')
    if not company:
        return jsonify(error='请填写公司名称'), 400
    if not files:
        return jsonify(error='未选择任何 PDF 文件'), 400
    added, failed = [], []
    import tempfile
    for f in files:
        if not f or not f.filename:
            continue
        tmp = os.path.join(qa_mod.UPLOAD_DIR, '_tmp_' + f.filename)
        try:
            f.save(tmp)
            rep = qa_mod.add_report(company, tmp, year or None)
            if rep.get('error'):
                failed.append({'file': f.filename, 'error': rep['error']})
            else:
                added.append(rep)
        except Exception as e:
            failed.append({'file': getattr(f, 'filename', '?'), 'error': str(e)})
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    return jsonify(ok=True, company=company, added=added, failed=failed)


@app.route('/api/qa/remove', methods=['POST'])
def qa_remove():
    data = request.get_json(force=True) or {}
    company = (data.get('company') or '').strip()
    file = (data.get('file') or '').strip()
    if not company or not file:
        return jsonify(error='缺少参数'), 400
    qa_mod.remove_report(company, file)
    return jsonify(ok=True)


@app.route('/api/qa/ask', methods=['POST'])
def qa_ask():
    data = request.get_json(force=True) or {}
    companies = data.get('companies') or []
    question = (data.get('question') or '').strip()
    history = data.get('history') or []
    metrics = data.get('metrics') or ''
    allow_general = bool(data.get('allow_general'))
    if not question:
        return jsonify(error='请先输入问题'), 400
    if not companies:
        return jsonify(error='请先在文档库选择要问的公司'), 400
    provider = qa_mod.get_provider()
    cfg = qa_mod.get_provider_cfg(provider)
    api_key = qa_mod.get_api_key(provider)
    if not api_key:
        return jsonify(error=f'请先在「AI 问答设置」页填写并保存「{cfg["name"]}」的 API Key'), 400
    model = qa_mod.get_model()
    endpoint = cfg['endpoint']
    # 资料来源有两类：①「年报 PDF」文档库（上传了才检索）；②「三大表数据」metrics
    # （公司分析页已生成对比即自动携带：资产负债表/利润表/现金流量表/固定指标/杜邦分析）。
    # 二者有其一即可回答，都不给才报错。未上传年报的公司不再被硬拒，也不会误建空目录。
    lib = qa_mod.list_library()
    valid = [c for c in companies if lib.get(c)]          # 上传了年报的公司
    skipped = [c for c in companies if c not in valid]    # 没上传年报（但可能带三大表数据）
    has_metrics = bool(metrics and metrics.strip())
    if not valid and not has_metrics:
        names = '、'.join(companies)
        return jsonify(error=(f'所选公司（{names}）既没有上传年报，也没有可分析的三大表数据。'
                              f'请先在「公司分析」页生成对比，或在「AI 问答设置」页上传年报 PDF。')), 400
    sources = qa_mod.retrieve(valid, question, k=6) if valid else []
    if skipped:
        if valid:
            holder = {'skipped': '以下公司未上传年报，已仅基于其三大表数据回答：' + '、'.join(skipped)}
        else:
            holder = {'skipped': '所选公司均未上传年报，已基于「公司分析」模块的三大表数据回答。'}
    else:
        holder = {}

    def gen():
        if holder.get('skipped'):
            yield _json.dumps({'type': 'info', 'data': holder['skipped']},
                              ensure_ascii=False) + "\n"
        yield _json.dumps({'type': 'sources', 'data': sources},
                          ensure_ascii=False) + "\n"
        try:
            for tok in qa_mod.stream_answer(api_key, question, history,
                                            sources, metrics,
                                            on_usage=lambda u: holder.__setitem__('usage', u),
                                            model=model, endpoint=endpoint,
                                            allow_general=allow_general):
                yield _json.dumps({'type': 'delta', 'data': tok},
                                  ensure_ascii=False) + "\n"
        except Exception as e:
            yield _json.dumps({'type': 'error', 'data': str(e)},
                              ensure_ascii=False) + "\n"
        # 累计本次用量（按当前模型费率）
        u = holder.get('usage')
        if u:
            try:
                pricing = qa_mod.get_pricing(provider, model)
                saved = qa_mod.add_usage(
                    u.get('prompt_tokens', 0) or 0,
                    u.get('completion_tokens', 0) or 0,
                    (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0,
                    pricing)
                yield _json.dumps({'type': 'usage', 'data': saved},
                                  ensure_ascii=False) + "\n"
            except Exception:
                pass
        yield _json.dumps({'type': 'done'}, ensure_ascii=False) + "\n"

    return Response(gen(), mimetype='application/x-ndjson')


@app.route('/api/qa/usage')
def qa_usage():
    return jsonify(qa_mod.load_usage())


@app.route('/api/qa/usage/reset', methods=['POST'])
def qa_usage_reset():
    return jsonify(qa_mod.reset_usage())


PORT = 8098

def _free_port(port):
    '''启动前清理占用该端口的旧 Flask 进程（Windows），避免“重启”后浏览器仍是旧页面'''
    if os.name != 'nt':
        return
    try:
        res = subprocess.run(['netstat', '-ano'], capture_output=True, timeout=15)
        out = (res.stdout or b'').decode('utf-8', 'ignore') + '\n' + (res.stderr or b'').decode('utf-8', 'ignore')
        pids = set()
        for line in out.splitlines():
            if 'LISTENING' in line and f':{port}' in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.run(['taskkill', '/PID', pid, '/F', '/T'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                print(f'[启动] 已结束占用端口 {port} 的旧进程 PID={pid}')
            except Exception as e:
                print(f'[警告] 无法结束 PID={pid}：{e}（请手动在任务管理器结束占用 {port} 的 python 进程）')
        if pids:
            time.sleep(1.0)   # 等端口释放
    except Exception as e:
        print(f'[警告] 自动清理端口 {port} 失败：{e}（若端口冲突请手动结束旧进程）')

if __name__ == '__main__':
    _free_port(PORT)
    time.sleep(0.5)
    threading.Timer(1.2, lambda: webbrowser.open_new(f'http://127.0.0.1:{PORT}')).start()
    try:
        app.run(host='0.0.0.0', port=PORT, debug=False)
    except OSError as e:
        print(f'\n[严重] 无法在端口 {PORT} 启动服务：{e}')
        print(f'说明：端口仍被占用，旧进程未被结束。请手动结束后再启动：')
        print(f'  1) 在 CMD/PowerShell 运行： netstat -ano | findstr :{PORT}')
        print(f'  2) 找到状态为 LISTENING 那一行的最后一个数字（即 PID）')
        print(f'  3) 运行： taskkill /PID <上面查到的PID> /F')
        print(f'  4) 重新运行： runtime/python/python.exe app.py')
        input('按回车退出...')
