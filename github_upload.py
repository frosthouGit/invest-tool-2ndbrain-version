#!/usr/bin/env python3
# 通过 GitHub Contents API 上传文件（绕过连不上的 git 协议 / 443 端口）
# 用法（在本项目目录执行）：
#   set GITHUB_TOKEN=你的PAT
#   set GITHUB_REPO=frosthouGit/invest-tool-2ndbrain-version
#   python github_upload.py
# 说明：只上传 git 已跟踪的文件（git ls-files），不碰 runtime/、config.json、私人数据。
import os, sys, base64, subprocess, requests
from urllib.parse import quote

TOKEN = os.environ.get('GITHUB_TOKEN')
REPO = os.environ.get('GITHUB_REPO')          # 形如 frosthouGit/invest-tool-2ndbrain-version
BRANCH = os.environ.get('BRANCH', 'main')
if not TOKEN or not REPO:
    print("缺少环境变量：请先 set GITHUB_TOKEN=你的PAT 与 GITHUB_REPO=owner/repo")
    sys.exit(1)

API = f"https://api.github.com/repos/{REPO}/contents"
HEADERS = {"Authorization": f"Bearer {TOKEN}",
           "Accept": "application/vnd.github+json",
           "User-Agent": "invest-tool-uploader"}

def enc_path(p):
    # 按路径段分别编码，保留 '/' 层级分隔（中文段会被正确编码）
    return '/'.join(quote(seg, safe='') for seg in p.split('/'))

# 只上传 git 已跟踪的文件，天然排除 .gitignore 里的内容
# 注意：Windows 上 git ls-files 输出为系统编码(GBK/cp936)，需用 'oem' 解码，
# 否则中文文件名会被解码成乱码，导致 os.path.isfile 找不到文件而被漏传。
try:
    _raw = subprocess.check_output(['git', '-c', 'core.quotepath=false', 'ls-files'],
                                   stderr=subprocess.DEVNULL)
    _txt = _raw.decode('oem', errors='replace')
except Exception:
    _txt = subprocess.check_output(['git', 'ls-files'],
                                   stderr=subprocess.DEVNULL).decode('utf-8', 'replace')
files = [f for f in _txt.splitlines() if f]

def get_sha(path):
    url = f"{API}/{enc_path(path)}"
    try:
        r = requests.get(url, headers=HEADERS, params={'ref': BRANCH}, timeout=20)
        if r.status_code == 200:
            return r.json().get('sha')
    except Exception:
        pass
    return None

ok = fail = 0
for f in files:
    if not os.path.isfile(f):
        continue
    with open(f, 'rb') as fh:
        raw = fh.read()
    if len(raw) > 950 * 1024:
        print(f"SKIP {f} (超过 API 单文件 1MB 限制)")
        continue
    content = base64.b64encode(raw).decode()
    sha = get_sha(f)
    body = {"message": f"add {f}", "content": content, "branch": BRANCH}
    if sha:
        body["sha"] = sha
    url = f"{API}/{enc_path(f)}"
    try:
        r = requests.put(url, headers=HEADERS, json=body, timeout=40)
    except Exception as e:
        print(f"FAIL {f} -> {e}")
        fail += 1
        continue
    if r.status_code in (200, 201):
        print(f"OK   {f}")
        ok += 1
    else:
        print(f"FAIL {f} -> HTTP {r.status_code} {r.text[:160]}")
        fail += 1

print(f"\n完成：成功 {ok} 个，失败 {fail} 个，共 {len(files)} 个文件")
