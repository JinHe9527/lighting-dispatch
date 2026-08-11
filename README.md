# 团播派工系统

面向团队下单人、工作人员、组长和管理员的灯光派工与排期系统。项目包含 Web 应用、Python 服务端和原生微信小程序，三端共用同一套账号、权限、工单、排期和审计数据。

## 主要能力

- 支持团队下单、自动派单、人工指派、改派、转包、取消和异常处理。
- 按班次、离岗状态、账号资格和时间冲突校验工作人员可用性。
- 支持普通任务、自定义任务、超长任务审核和团队预约限制。
- 支持管理员、组长、工作人员、团队账号和管理层只读账号。
- 支持飞书 OAuth、微信小程序登录与账号绑定。
- 工单可同步到飞书共享日历，并通过后台任务发送提醒。
- 关键业务操作保留订单事件和审计记录。
- 支持 Windows 与腾讯云 Linux 部署。

## 项目结构

```text
.
├── app/                 Web 页面、Python 服务端和业务逻辑
├── miniprogram/         原生微信小程序
├── docs/                业务规则、配置指引和上线验收文档
├── scripts/deploy/      Linux、Nginx、systemd 和安全配置
├── tests/               Python 全链路测试和 Playwright UI 冒烟测试
├── .env.example         环境变量示例
└── package.json         UI 测试依赖和命令
```

## 技术组成

- 服务端：Python 标准库、SQLite
- Web：原生 HTML、CSS、JavaScript
- 微信小程序：原生 WXML、WXSS、JavaScript
- UI 测试：Playwright
- 部署：Nginx、systemd、Let's Encrypt

## 本地运行

项目需要 Python 3。测试模式使用独立数据库，不连接飞书或微信正式服务。

```bash
DISPATCH_TEST_MODE=1 python3 app/server.py
```

默认访问地址：<http://127.0.0.1:8848>

## 正式配置

复制 `.env.example` 中的配置项到部署环境的 `.env`，并填写真实参数：

```text
DISPATCH_APP_ID=飞书应用ID
DISPATCH_APP_SECRET=飞书应用密钥
DISPATCH_WECHAT_APP_ID=微信小程序AppID
DISPATCH_WECHAT_APP_SECRET=微信小程序AppSecret
DISPATCH_BASE_URL=https://你的公网域名
DISPATCH_TEST_MODE=0
```

`.env`、数据库、媒体文件、日志以及 `miniprogram/project.private.config.json` 不应提交到 Git。

## 微信小程序

使用微信开发者工具导入 `miniprogram/` 目录。小程序通过 HTTPS API 与现有服务端通信，服务端负责使用 AppSecret 换取微信身份并签发原生会话，AppSecret 不进入小程序代码。

配置步骤和发布检查见 [微信小程序配置指引](docs/微信小程序配置指引.md)。

## 测试

运行 Python 测试：

```bash
python3 -m unittest discover -s tests -v
```

运行 UI 冒烟测试前，先在一个终端中启动 `18848` 端口的测试服务：

```bash
DISPATCH_TEST_MODE=1 DISPATCH_PORT=18848 python3 app/server.py
```

在另一个终端中安装依赖并运行测试：

```bash
npm install
npm run test:ui
```

## 部署与验收

- [应用部署说明](app/README.md)
- [飞书后台配置指引](docs/飞书后台配置指引.md)
- [微信小程序配置指引](docs/微信小程序配置指引.md)
- [派工业务规则基线](docs/派工业务规则基线.md)
- [上线前全链路审计报告](docs/上线前全链路审计报告.md)
- [正式上线验收矩阵](docs/正式上线验收矩阵.md)

正式环境必须使用 HTTPS。数据库、媒体目录和 `.env` 应作为持久化数据独立保管，部署更新不得覆盖。
