# -*- coding: utf-8 -*-
"""
投资小工具 · 运行环境预检（双击 runmytools.bat 时自动先跑）
作用：在启动服务前，检查本机自带的 runtime/python 是否装齐了必要的库。
      缺什么就用中文直接告诉你，避免“黑框一闪、没有任何提示”的困惑。
只使用 Python 标准库，因此一定能跑起来（不会因第三方库缺失而自身崩溃）。
"""
import sys
import importlib

# 核心依赖：缺失则整套程序无法启动
CORE = ['flask', 'openpyxl', 'requests', 'akshare']
# AI 问答依赖：缺失仅影响 AI 问答功能，核心功能（公司分析/选股/记录）仍可运行
QA = ['pdfplumber', 'jieba', 'rank_bm25']


def _try_import(names):
    """尝试导入一组模块，返回 [(模块名, 异常)] 的缺失列表。"""
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - 这里需要捕获一切导入错误
            missing.append((name, str(e)))
    return missing


def main():
    print('=== 投资小工具 · 运行环境预检 ===')
    print('Python: %s' % sys.version.split()[0])
    print('解释器: %s' % sys.executable)
    print('-' * 40)

    core_miss = _try_import(CORE)
    qa_miss = _try_import(QA)

    fatal = False

    # 业务模块（data / record 是核心；qa 是可选的 AI 问答）
    try:
        importlib.import_module('data')
        importlib.import_module('record')
        print('[OK] 核心业务模块 data / record 加载正常')
    except Exception as e:  # noqa: BLE001
        fatal = True
        print('[严重] 核心业务模块加载失败：%s' % e)

    try:
        importlib.import_module('qa')
        print('[OK] AI 问答模块 qa 加载正常')
    except Exception as e:  # noqa: BLE001
        print('[提示] AI 问答模块 qa 暂不可用（不影响核心功能）：%s' % e)

    print('-' * 40)
    if core_miss:
        fatal = True
        print('[严重] 核心依赖缺失（程序无法启动）：')
        for m, e in core_miss:
            print('   - %s : %s' % (m, e))
    else:
        print('[OK] 核心依赖齐全：flask / openpyxl / requests / akshare')

    if qa_miss:
        print('[提示] AI 问答依赖缺失（仅 AI 问答不可用，核心功能正常）：')
        for m, e in qa_miss:
            print('   - %s : %s' % (m, e))
    else:
        print('[OK] AI 问答依赖齐全：pdfplumber / jieba / rank_bm25')

    print('-' * 40)
    if fatal:
        print('预检未通过：核心依赖或核心业务模块缺失。')
        print('最常见原因：文件夹没有完整拷贝（少了 runtime 子目录或其中文件）。')
        print('请重新把整个“投资小工具”文件夹一起拷贝（务必包含 runtime 目录），再双击 runmytools.bat。')
        return 1

    print('预检通过，正在启动服务……')
    return 0


if __name__ == '__main__':
    sys.exit(main())
