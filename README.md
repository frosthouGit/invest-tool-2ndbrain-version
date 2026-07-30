# 投资小工具（Invest Tool）

一个面向个人投资理财的本地网页小工具，覆盖**公司分析 / 宏观看板 / AI 问答**三大块。
纯本地运行，数据从公开市场接口（AkShare 等）抓取，不上传任何个人信息。

> ⚠️ **本仓库是「源码版」**。如果你只是想**零安装给别的电脑用**，请用「绿色拷贝版」
> （整个文件夹含自带 Python 运行时，双击 `runmytools.bat` 即用，详见下方「两种版本」）。

---

## 两种版本怎么选

| 场景 | 用哪个 | 说明 |
|---|---|---|
| 给自己的其他电脑用 / 发给朋友用，**不想装环境** | **绿色拷贝版** | 整个文件夹（含 `runtime/`）整体拷贝，双击 `runmytools.bat` 即可，**无需联网装任何东西**（运行需联网拉数据除外）。 |
| 想看代码、用 WorkBuddy / IDE 改、参与开发 | **本 GitHub 源码版** | `git clone` 后用自己的 Python + `pip install -r requirements.txt` 跑。 |

两种版本代码完全一致，只是分发方式不同。绿色版由源码版加 `runtime/` 打包而成。

---

## 一、源码版（GitHub）运行方式

```bash
# 1. 需要本机已装 Python 3.11+
python -m pip install -r requirements.txt

# 2. 准备配置（AI 问答用，可选）
#    复制模板并按需填写：
cp config.example.json config.json
#    然后编辑 config.json，填入你的 DeepSeek / TokenHub API Key

# 3. 启动
python app.py
# 浏览器打开 http://127.0.0.1:8098
```

首次运行会自动在浏览器打开。如需推荐同行公司功能，可先跑：
```bash
python build_peers_cache.py   # 生成 peers_cache.json（需联网一次）
```

---

## 二、绿色拷贝版（零安装）运行方式

见随包分发的 **`拷贝与运行说明.txt`**：把整个文件夹（**必须含 `runtime/`**）拷到目标电脑任意位置，双击 `runmytools.bat` 即可。

---

## 功能模块

1. **公司分析**：选 2–3 家公司，拉取近 N 年完整合并三大表（资产负债 / 利润 / 现金流）+ 杜邦分析，
   自动计算 19 项固定指标并画图，支持导出 Excel。
2. **宏观看板**：中美宏观指标卡片（中国 GDP/CPI/PPI/M2/M1/社零/制造业PMI/LPR/中债10Y 等；
   美国 国债收益率/GDP/CPI/核心PCE/失业率/非农/ISM 等），含「美债收益率曲线」可鼠标框选缩放。
3. **AI 问答**：上传年报 PDF 或直接基于三大表数据，向 DeepSeek / 腾讯混元(HY3) 等模型提问。
   （AI 接入在你本地配置，工具本身不内置任何 Key、无费用。）

---

## 配置 AI 问答（可选）

编辑 `config.json`（由 `config.example.json` 复制而来）：

```json
{
  "deepseek_api_key": "你的DeepSeek密钥",
  "model": "deepseek-chat",
  "provider": "deepseek",
  "allow_general": false
}
```

- `provider`：当前支持 `deepseek`（DeepSeek 官方）与 `tokenhub`（腾讯 TokenHub 聚合，含 hy3-preview 等）。
- `allow_general`：`true` 时允许模型做更自由的通用分析；默认 `false` 仅基于财报/三大表上下文回答。

不填 Key 也能用公司分析和宏观看板；只有 AI 问答需要。

---

## 目录结构

```
app.py                   Flask 入口（端口 8098）
data.py                  三大表 / 指标计算 / Excel 导出
record.py                宏观看板指标定义与抓取
qa.py                   AI 问答（多供应商、本地 PDF 检索）
screen.py                （辅助脚本）
templates/               网页模板（index / record / qa 等）
static/vendor/           本地化的 Chart.js 与缩放插件（已离线，无需 CDN）
build_peers_cache.py     生成同行公司离线缓存
build_valuation_cache.py / build_hk_cache.py  其它缓存构建脚本
```

> 注意：图表库已**本地化**在 `static/vendor/`，整站不依赖任何外部 CDN，
> 即使网络屏蔽 jsDelivr 也能正常画图。

---

## 数据来源与免责声明

- 行情 / 财报 / 宏观数据来自 AkShare 等公开市场接口，数据准确性以原始来源为准。
- 本工具**仅用于个人学习与研究**，所有分析/指标不构成任何投资建议。
- 投资有风险，决策需谨慎，盈亏自负。
- 本工具仅限 Windows 使用（绿色版依赖 Windows 路径与 BAT 启动）。
