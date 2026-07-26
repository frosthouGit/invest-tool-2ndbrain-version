# -*- coding: utf-8 -*-
"""
公司分析 · AI 问答后端（文档库 + 实时多轮对话版）

模型：
  - 按「公司名称」归集多份年报 PDF（一家公司可传多年报）
  - 本地抽取文字 → 切段 → 按公司合并建 BM25 索引（进程内缓存，新增/删除时失效）
  - 用户提问时，在所选公司范围内检索最相关段落 + 已算好的指标，
    连同多轮对话历史一起流式调「用户自选供应商」的模型回答
不依赖任何向量数据库 / 额外 embedding API，纯本地检索 + 用户自备 API Key。
支持多供应商（DeepSeek 官方 / 腾讯 TokenHub 等），可在「AI 问答设置」页切换。
"""
import os
import re
import json
import time
import threading
import shutil
import hashlib
import requests
import jieba
from rank_bm25 import BM25Okapi
import pdfplumber

# 优先用 PyMuPDF 抽文字（比 pdfplumber 快约 20 倍），缺失时退回 pdfplumber
try:
    import fitz
    _HAVE_FITZ = True
except Exception:
    _HAVE_FITZ = False

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
UPLOAD_DIR = os.path.join(BASE, 'qa_uploads')
MANIFEST_PATH = os.path.join(UPLOAD_DIR, 'manifest.json')
USAGE_PATH = os.path.join(BASE, 'qa_usage.json')

# ---------------- 多供应商（可切换） ----------------
# 每个供应商 = 一个 OpenAI 兼容的 chat/completions 端点 + 它支持的模型 + 各模型费率。
# 费用费率（元 / 百万 tokens）：input_hit=缓存命中输入、input_miss=未命中输入、output=输出。
# 数据来源：DeepSeek / 腾讯云 TokenHub 官方刊例价（2026-07，仅供参考）。
# HY3（hy3-preview）按输入长度分三档，这里用最常见的 <16k 档做预估。
PROVIDERS = {
    'deepseek': {
        'name': 'DeepSeek 官方',
        'endpoint': 'https://api.deepseek.com/chat/completions',
        'key_field': 'deepseek_api_key',
        'key_hint': 'platform.deepseek.com 注册充值后获取',
        'models': [
            {'id': 'deepseek-v4-flash', 'label': 'deepseek-v4-flash（推荐，便宜快）',
             'pricing': {'input_hit': 0.02, 'input_miss': 1.0, 'output': 2.0}},
            {'id': 'deepseek-v4-pro', 'label': 'deepseek-v4-pro（更强，更贵）',
             'pricing': {'input_hit': 0.50, 'input_miss': 2.0, 'output': 8.0}},
        ],
    },
    'tokenhub': {
        'name': '腾讯 TokenHub（HY3 / 混元 / 多模型）',
        'endpoint': 'https://tokenhub.tencentmaas.com/v1/chat/completions',
        'key_field': 'tokenhub_api_key',
        'key_hint': '腾讯云大模型 API（TokenHub）获取 API Key',
        'models': [
            {'id': 'hy3-preview', 'label': 'hy3-preview（腾讯最新，256K 上下文，推荐）',
             'pricing': {'input_hit': 0.4, 'input_miss': 1.2, 'output': 4.0}},
            {'id': 'deepseek-v4-flash', 'label': 'deepseek-v4-flash（TokenHub 原厂直供）',
             'pricing': {'input_hit': 0.02, 'input_miss': 1.0, 'output': 2.0}},
            {'id': 'deepseek-v4-pro', 'label': 'deepseek-v4-pro（TokenHub 原厂直供）',
             'pricing': {'input_hit': 0.025, 'input_miss': 3.0, 'output': 6.0}},
            {'id': 'glm-5.1', 'label': 'glm-5.1',
             'pricing': {'input_hit': 1.3, 'input_miss': 6.0, 'output': 24.0}},
            {'id': 'kimi-k2.6', 'label': 'kimi-k2.6',
             'pricing': {'input_hit': 1.1, 'input_miss': 6.5, 'output': 27.0}},
            {'id': 'minimax-m2.7', 'label': 'minimax-m2.7',
             'pricing': {'input_hit': 0.42, 'input_miss': 2.1, 'output': 8.4}},
        ],
    },
}
DEFAULT_PROVIDER = 'deepseek'

os.makedirs(UPLOAD_DIR, exist_ok=True)
jieba.setLogLevel("ERROR")

# 进程内索引缓存：company -> (bm25, chunks)
INDEX_CACHE = {}
_CACHE_LOCK = threading.Lock()


# ---------------- 配置（各供应商 Key 仅存本地） ----------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _clean_key(key):
    k = (key or "").strip()
    if k.lower().startswith("bearer "):
        k = k[7:].strip()
    return k


def get_provider():
    """返回当前选择的供应商 id，默认 deepseek；无效值回退默认。"""
    p = (load_config().get('provider') or DEFAULT_PROVIDER).strip()
    return p if p in PROVIDERS else DEFAULT_PROVIDER


def get_model():
    """返回当前供应商下选中的模型 id。"""
    p = get_provider()
    cfg = PROVIDERS[p]
    saved = (load_config().get('model') or '').strip()
    if saved and any(m['id'] == saved for m in cfg['models']):
        return saved
    return cfg['models'][0]['id']


def get_provider_cfg(provider=None):
    return PROVIDERS[provider or get_provider()]


def get_api_key(provider=None):
    field = get_provider_cfg(provider)['key_field']
    return load_config().get(field) or ''


def has_key(provider=None):
    return bool(get_api_key(provider))


def save_key(provider, key):
    """保存某供应商的 key（去 Bearer 前缀/空白）。provider 无效则忽略。"""
    if provider not in PROVIDERS:
        return
    cfg = load_config()
    cfg[PROVIDERS[provider]['key_field']] = _clean_key(key)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def save_provider_model(provider, model):
    cfg = load_config()
    if provider in PROVIDERS:
        cfg['provider'] = provider
    if model:
        cfg['model'] = model
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_allow_general():
    """是否允许模型在财报数据不足时，结合自身公开常识补充（默认关=严格只基于提供的数据）。"""
    return bool(load_config().get('allow_general'))


def set_allow_general(val):
    cfg = load_config()
    cfg['allow_general'] = bool(val)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_pricing(provider=None, model=None):
    """返回某模型费率；找不到则回退全 0。"""
    p = provider or get_provider()
    m = model or get_model()
    for prov in (PROVIDERS.get(p, {}).get('models', []) or []):
        if prov['id'] == m:
            return prov.get('pricing') or {'input_hit': 0, 'input_miss': 0, 'output': 0}
    return {'input_hit': 0, 'input_miss': 0, 'output': 0}


# ---------------- 用量累计（本地，不上传） ----------------
def load_usage():
    if os.path.exists(USAGE_PATH):
        try:
            return json.load(open(USAGE_PATH, 'r', encoding='utf-8'))
        except Exception:
            pass
    return {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
            'cached_tokens': 0, 'total_tokens': 0, 'cost_rmb': 0.0}


def add_usage(prompt, completion, cached=0, pricing=None):
    """累加一次调用的 token 与预估费用；pricing 缺省时按 0 计（仅统计 token）。"""
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    cached = int(cached or 0)
    if cached > prompt:
        cached = prompt
    if pricing is None:
        pricing = {'input_hit': 0, 'input_miss': 0, 'output': 0}
    u = load_usage()
    u['calls'] += 1
    u['prompt_tokens'] += prompt
    u['completion_tokens'] += completion
    u['cached_tokens'] += cached
    u['total_tokens'] += (prompt + completion)
    cost = (cached * pricing['input_hit']
            + (prompt - cached) * pricing['input_miss']
            + completion * pricing['output']) / 1e6
    u['cost_rmb'] = round(u['cost_rmb'] + cost, 6)
    try:
        json.dump(u, open(USAGE_PATH, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass
    return u


def reset_usage():
    try:
        if os.path.exists(USAGE_PATH):
            os.remove(USAGE_PATH)
    except Exception:
        pass
    return load_usage()


# ---------------- 清单（文档库） ----------------
def _load_manifest():
    if os.path.exists(MANIFEST_PATH):
        try:
            return json.load(open(MANIFEST_PATH, 'r', encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_manifest(m):
    json.dump(m, open(MANIFEST_PATH, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)


def _safe_name(n):
    n = (n or '').strip()
    n = re.sub(r'[\\/:*?"<>|]', '_', n)
    return n or '未命名公司'


def _company_folder(company):
    # 仅返回目录路径，不主动创建目录。
    # 真正创建目录只发生在 add_report（上传年报入库）时，
    # 避免在公司分析页「提问」流程中（检索/建索引会调用本函数）为
    # 未上传任何财报的公司误建空文件夹。
    return os.path.join(UPLOAD_DIR, _safe_name(company))


def _chunks_path(pdf_path):
    h = hashlib.md5(pdf_path.encode('utf-8')).hexdigest()
    return pdf_path + '.' + h[:8] + '.chunks.json'


# ---------------- PDF 抽取 / 切段 / 索引 ----------------
def extract_pdf(path):
    """返回 [(页码, 文字), ...]，图片页文字为空也跳过不影响。优先 PyMuPDF。"""
    if _HAVE_FITZ:
        return _extract_pdf_fitz(path)
    return _extract_pdf_plumber(path)


def _extract_pdf_fitz(path):
    pages = []
    doc = fitz.open(path)
    for i, page in enumerate(doc, 1):
        try:
            t = page.get_text("text") or ""
        except Exception:
            t = ""
        pages.append((i, t))
    doc.close()
    return pages


def _extract_pdf_plumber(path):
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            pages.append((i, t))
    return pages


def _clean(text):
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    return text.strip()


def chunk_doc(doc, size=600, overlap=80):
    """按页滑动窗口切段，保留页码；每段约 size 字。"""
    chunks = []
    for page, text in doc:
        text = _clean(text)
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            seg = text[start:end].strip()
            if seg:
                chunks.append({"page": page, "text": seg})
            if end >= len(text):
                break
            start += size - overlap
    return chunks


def _tokenize(text):
    return [w for w in jieba.lcut(text) if w.strip() and not re.fullmatch(r'\s+', w)]


def build_index(chunks):
    corpus = [_tokenize(c["text"]) for c in chunks]
    return BM25Okapi(corpus)


def get_pdf_chunks(pdf_path):
    """取某 PDF 的切段（带磁盘缓存，避免重复抽取）。"""
    cp = _chunks_path(pdf_path)
    if os.path.exists(cp):
        try:
            return json.load(open(cp, 'r', encoding='utf-8'))
        except Exception:
            pass
    pages = extract_pdf(pdf_path)
    doc = [(p, t) for p, t in pages if t.strip()]
    chunks = chunk_doc(doc)
    try:
        json.dump(chunks, open(cp, 'w', encoding='utf-8'))
    except Exception:
        pass
    return chunks


# ---------------- 文档库增删 ----------------
def add_report(company, src_path, year=None):
    """把一份年报加入某公司的文档库；返回该报告元信息。"""
    company = _safe_name(company)
    folder = _company_folder(company)
    os.makedirs(folder, exist_ok=True)  # 仅在真正上传入库时才创建公司目录
    base = os.path.basename(src_path)
    dest = os.path.join(folder, base)
    # 去重：同名文件已存在（通常是重复上传同一份年报），直接跳过，
    # 不再产生带时间戳的副本（避免文档库里出现重复 PDF）。
    if os.path.exists(dest):
        m = _load_manifest()
        for r in m.get(company, []):
            if r["file"] == base:
                return {**r, "skipped": True}
    shutil.copy2(src_path, dest)
    try:
        chunks = get_pdf_chunks(dest)
    except Exception as e:
        # 抽不出来（扫描版等）：仍入库但标记失败，方便用户知悉
        return {"file": os.path.basename(dest), "year": year or "未知",
                "pages": 0, "n_chunks": 0, "error": f"文字抽取失败：{e}"}
    rep = {
        "file": os.path.basename(dest),
        "year": year or _guess_year(base) or "未知",
        "pages": max((c["page"] for c in chunks), default=0),
        "n_chunks": len(chunks),
    }
    m = _load_manifest()
    m.setdefault(company, [])
    if not any(r["file"] == rep["file"] for r in m[company]):
        m[company].append(rep)
    _save_manifest(m)
    with _CACHE_LOCK:
        INDEX_CACHE.pop(company, None)
    return rep


def remove_report(company, file):
    company = _safe_name(company)
    folder = _company_folder(company)
    fp = os.path.join(folder, file)
    for p in (fp, _chunks_path(fp)):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    m = _load_manifest()
    if company in m:
        m[company] = [r for r in m[company] if r["file"] != file]
        if not m[company]:
            m.pop(company, None)
    _save_manifest(m)
    with _CACHE_LOCK:
        INDEX_CACHE.pop(company, None)


def _guess_year(name):
    m = re.search(r'(19|20)\d{2}', name or '')
    return m.group(0) if m else None


def list_library():
    return _load_manifest()


# ---------------- 检索 ----------------
def get_company_chunks(company):
    company = _safe_name(company)
    m = _load_manifest()
    folder = _company_folder(company)
    allc = []
    for rep in m.get(company, []):
        cp = os.path.join(folder, rep["file"])
        if os.path.exists(cp):
            allc += get_pdf_chunks(cp)
    return allc


def get_company_index(company):
    company = _safe_name(company)
    with _CACHE_LOCK:
        if company in INDEX_CACHE:
            return INDEX_CACHE[company]
    chunks = get_company_chunks(company)
    bm25 = build_index(chunks) if chunks else None
    with _CACHE_LOCK:
        INDEX_CACHE[company] = (bm25, chunks)
    return bm25, chunks


def retrieve(companies, query, k=6):
    """在所选公司范围内检索最相关段落；返回带公司名的列表。"""
    if isinstance(companies, str):
        companies = [companies]
    qtok = _tokenize(query)
    if not qtok:
        return []
    results = []
    for comp in companies:
        bm25, chunks = get_company_index(comp)
        if not bm25 or not chunks:
            continue
        scores = bm25.get_scores(qtok)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        cnt = 0
        for i in order:
            if scores[i] > 0:
                results.append({
                    "company": comp,
                    "page": chunks[i]["page"],
                    "text": chunks[i]["text"],
                })
                cnt += 1
            if cnt >= k:
                break
    return results


# ---------------- DeepSeek 流式调用 ----------------
def stream_answer(api_key, question, history, sources, metrics, on_usage=None, model=None, endpoint=None, allow_general=False):
    """流式返回 token（生成器）。history: [{"role","content"}]；on_usage(usage_dict) 在拿到用量时回调。
    endpoint：OpenAI 兼容的 chat/completions 完整 URL；缺省回退 DeepSeek。
    allow_general：财报数据不足时，是否允许模型结合自身公开常识补充（标注『结合公开常识』）。"""
    if allow_general:
        sys_p = (
            "你是严谨的财务分析助手。优先依据下面【财报上下文】与【已知指标】回答用户问题；"
            "若所提供的数据不足以完整回答，可以结合你自身的公开常识进行合理补充，"
            "但必须明确标注「（结合公开常识）」，且不得把推测伪装成财报原文、不得编造具体数值。"
            "回答时尽量标注信息来源（如“某公司 利润表/资产负债表 的某科目”或“公开资料”）。语言简洁、用中文。"
        )
    else:
        sys_p = (
            "你是严谨的财务分析助手。只依据下面【财报上下文】与【已知指标】回答用户问题，"
            "不得编造或引用上下文以外的数据；若上下文不足以回答，请明确说明“财报中未披露”。"
            "回答时尽量标注信息来源（如“某公司 利润表/资产负债表 的某科目”）。语言简洁、用中文。"
        )
    ctx = "【财报上下文】\n" + "\n\n".join(
        f"（{s['company']} 第{s['page']}页）{s['text']}" for s in sources
    )
    if metrics and metrics.strip():
        ctx += "\n\n【已知指标】\n" + metrics.strip()
    messages = [{"role": "system", "content": sys_p + "\n\n" + ctx}]
    for h in history:
        messages.append({"role": h.get("role", "user"),
                         "content": h.get("content", "")})
    messages.append({"role": "user", "content": question})

    body = {
        "model": (model or get_model()),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4000,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    # 防御性处理：去掉用户可能多粘的 "Bearer " 前缀 / 首尾空白
    api_key = (api_key or "").strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    if not api_key:
        raise RuntimeError("未配置 API Key，请先在「AI 问答设置」页填写并保存")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = endpoint or "https://api.deepseek.com/chat/completions"
    resp = requests.post(url, headers=headers, json=body,
                         stream=True, timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(f"AI 接口返回 {resp.status_code}: {resp.text[:300]}")
    for line in resp.iter_lines(decode_unicode=False):
        if not line:
            continue
        try:
            text = line.decode('utf-8')
        except Exception:
            continue
        if not text.startswith('data:'):
            continue
        data = text[5:].strip()
        if data == '[DONE]':
            break
        try:
            obj = json.loads(data)
            if obj.get("usage"):
                if on_usage:
                    on_usage(obj["usage"])
                continue
            delta = obj["choices"][0]["delta"].get("content", "")
            if delta:
                yield delta
        except Exception:
            continue
