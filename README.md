# 🔍 BidMonitor AI - 智能招投标监控系统

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

## 📖 项目简介

BidMonitor AI 是一款功能强大的招投标信息监控系统，支持：

- 🌐 **多网站监控** - 支持40+招标网站（政府采购、电力、能源等）
- 🤖 **AI 智能过滤** - 使用 AI 二次筛选，精准匹配相关信息
- 📧 **多渠道通知** - 邮件、短信、微信、语音电话
- 🖥️ **双端部署** - Windows 桌面版 + 服务器 Web 版
- 🔄 **自动运行** - 定时监控，24小时不间断

## 🚀 快速开始

### Windows 桌面版

`ash
# 安装依赖
pip install -r requirements.txt

# 运行
python run.py
`

### 服务器 Web 版

`ash
cd server
pip install -r requirements.txt
python app.py
# 访问 http://localhost:8080
`

详细部署请参考 `server/DEPLOY.md`

## ✨ 功能特性

### 1. 关键词配置
- 包含关键词：匹配任意一个即命中
- 排除关键词：过滤不相关内容
- 必含关键词：标题必须包含

### 2. 多网站支持
- 政府采购网、中国招标网、中国能建、国家电网...
- 支持自定义网站配置
- Selenium 模式绕过反爬虫

### 3. 通知方式
- 📧 邮件通知（支持多邮箱）
- 📱 短信通知（阿里云/腾讯云）
- 💬 微信推送（PushPlus）
- 📞 语音电话（阿里云）

### 4. AI 智能过滤
- 支持 DeepSeek、OpenAI 等 API
- 自动过滤无关招标信息

## 📁 项目结构

`
BidMonitor-AI/
├── src/                 # 核心代码
│   ├── gui.py           # 桌面 GUI
│   ├── monitor_core.py  # 监控核心
│   ├── ai_guard.py      # AI 过滤
│   ├── crawler/         # 爬虫模块
│   ├── notifier/        # 通知模块
│   └── database/        # 数据存储
├── server/              # 服务器版
│   ├── app.py           # FastAPI 应用
│   ├── static/          # Web 前端
│   └── setup.sh         # 部署脚本
├── run.py               # 启动入口
└── requirements.txt     # 依赖列表
`

## 🔧 配置说明

首次运行后，在界面中配置：

1. **关键词** - 根据需求设置监控关键词
2. **通知方式** - 配置邮箱/短信/微信等
3. **网站源** - 选择需要监控的网站
4. **AI 过滤** - （可选）配置 API Key

配置会自动保存到 `user_config.json`

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源协议

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

⭐ 如果这个项目对你有帮助，请给个 Star！


## 2026-11-10 本地修复说明

- 数据库使用绝对路径：优先环境变量 `BIDMONITOR_DB_PATH`；否则使用唯一已有的 `server/data/bids.db` 或 `data/bids.db`。两库并存时要求明确选择，不自动覆盖。全新安装默认 `server/data/bids.db`。
- 登录凭据改为环境变量 `BIDMONITOR_USERNAME` + `BIDMONITOR_PASSWORD`，或本机 `server/auth.local.json`。新安装自动生成随机密码，请本机查看该文件；不要提交/分享。本次迁移保留原登录信息。默认部署仅本机访问，外网部署需HTTPS与访问控制。
- 配置接口隐藏密钥；空白或 `***` 保留原密钥，不表示清除。保存失败会报错，不再返回假成功。
- 清空历史需选范围并确认，运行中的任务禁止清理。本次修复不会清理真实数据。
- 没有源站记录/标段ID时，金额差异只进入待核实版本，不新增项目、不覆盖旧值；有可靠ID的更正保留历史版本。已有重复行不会自动删除。
- 无法确认的发布日期留空；添加时间独立保存。不猜测修改旧记录的历史日期。
- Web依赖已包含 `pypdf`。HTTP采集验证TLS证书；企业证书链通过 `REQUESTS_CA_BUNDLE` 配置，不降级跳过验证。
- 单元测试使用临时数据库和构造输入；不代表全部网站已可访问或完成采集验收。
