# 团播派工系统

面向团队下单人、工作人员、组长和管理员的内部派工系统。后端仅使用 Python 标准库和 SQLite，前端为单页应用，支持飞书 OAuth、消息通知和任务提醒。

## Windows 正式部署

正式目录：`D:\BaiduNetdiskDownload\灯光排班-Windows\dispatch`

核心文件：

| 文件 | 作用 |
|---|---|
| `app\server.py` | HTTP、飞书接入、后台任务与配置管理 |
| `app\business.py` | 状态、排期、权限、公平派单和数据库迁移 |
| `app\service.py` | 工单、取消、改派和转包事务 |
| `app\index.html` | 四类角色页面 |
| `app\dispatch.db` | 正式数据库，部署更新不得覆盖 |
| `.env` | 飞书密钥和公网地址，部署更新不得覆盖 |
| `app\media` | 业务附件，部署更新不得覆盖 |

更新代码后双击 `app\重启服务-windows.bat`。启动时会先在线备份正式数据库和媒体副本，再执行原地迁移；数据库备份最多保留最近 30 份。

## 腾讯云 Linux 正式部署

Linux 使用相互隔离的目录：

| 目录 | 权限与内容 |
|---|---|
| `/opt/wuyou-dispatch/releases` | `root` 持有的只读版本 |
| `/opt/wuyou-dispatch/current` | 原子指向当前版本的链接 |
| `/var/lib/wuyou-dispatch` | 运行用户可写的数据库、媒体和日志 |
| `/var/backups/wuyou-dispatch` | 数据库快照和媒体备份 |
| `/var/backups/wuyou-dispatch-config` | `root` 专用的配置备份 |

把部署包上传到 `/home/ubuntu/wuyou-dispatch-release` 后执行：

```bash
bash /home/ubuntu/wuyou-dispatch-release/install-linux.sh
```

安装脚本先校验新版本；首次从旧目录升级时会停止旧服务，通过 SQLite backup API 迁移数据库，并在覆盖已有空库前保存迁移前备份。随后原子切换版本并检查 `/api/health`，失败时恢复部署前的程序链接和服务配置。若有异盘或远端挂载目录，可在环境中设置 `DISPATCH_BACKUP_MIRROR_DIR`，每日同步第二份数据库和媒体备份。

## 正式配置

`.env` 至少包含：

```text
DISPATCH_APP_ID=飞书应用ID
DISPATCH_APP_SECRET=飞书应用密钥
DISPATCH_BASE_URL=https://你的公网域名
DISPATCH_TEST_MODE=0
```

正式环境必须使用 HTTPS。飞书后台的网页地址和 OAuth 回调必须与 `DISPATCH_BASE_URL` 完全一致。

当前正式域名配置为：

```text
DISPATCH_BASE_URL=https://wuyoupaiban.cn
网页应用桌面端主页=https://wuyoupaiban.cn
网页应用移动端主页=https://wuyoupaiban.cn
OAuth 重定向 URL=https://wuyoupaiban.cn/auth/callback
```

Linux 部署前应先为 `wuyoupaiban.cn` 申请证书，证书路径必须为：

```text
/etc/letsencrypt/live/wuyoupaiban.cn/fullchain.pem
/etc/letsencrypt/live/wuyoupaiban.cn/privkey.pem
```

## 本地测试

```bash
DISPATCH_TEST_MODE=1 python3 app/server.py
python3 -m unittest discover -s tests -v
```

浏览器冒烟测试需要 Playwright 和本机 Chrome：

```bash
npm install
npm run test:ui
```

测试模式使用独立数据库，不发送真实飞书消息。

## 核心业务规则

- 工单状态：待派工、已派待开始、进行中、结束核实、已完成、已取消、异常。
- 团队账号必须绑定真实团队，只能查看本团队数据，不能指定工作人员。
- 指派必须通过完整班次、离岗（请假/出差）、账号资格和时间冲突检查。
- 预约默认限未来 7 天、2 个未开始预约、每天 240 分钟。
- 转包接受前原负责人继续负责；紧急、重复或进行中交接需组长审批。
- 取消、异常和转包全部留存订单事件及审计，不物理删除业务历史。
- 正式环境禁止清空工单或审计日志。

详细规则见 `docs/派工业务规则基线.md`，上线验证见 `docs/上线前全链路审计报告.md`。
