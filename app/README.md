# 灯光派工系统 · 运维手册

三端一体的灯光排班派工软件:团队下单 / 师傅接单报状态 / 管理端调度。
飞书登录 + 服务器端 RBAC 权限 + SQLite + 审计日志 + 飞书私信通知。零 pip 依赖。

## 日常

```bash
# 手动启动(前台,Ctrl+C 停)
cd "/Users/pc/github 项目/灯光排期软件" && python3 app/server.py

# 常驻安装(开机自启 + 崩溃自拉,推荐)
bash scripts/deploy/install.sh
# 卸载常驻
bash scripts/deploy/install.sh remove

# 看日志
tail -f ~/Library/Logs/dispatch.log
```

访问:`http://<Mac内网IP>:8848`(取 IP:`ipconfig getifaddr en0`)

## 文件

| 文件 | 说明 |
|---|---|
| `app/server.py` | 后端(Python 3.9 stdlib) |
| `app/index.html` | 前端(单页三端) |
| `app/dispatch.db` | 数据库主文件,在线备份请使用下方 SQLite `.backup` 命令 |
| `app/dispatch-test.db` | 独立测试库,仅在 `DISPATCH_TEST_MODE=1` 时使用 |
| `app/data.json` | 旧版本地库,仅作首次迁移源(teams+SLA),已不再写入 |
| `docs/飞书后台配置指引.md` | 飞书应用创建/权限/发布 全步骤 |
| `scripts/deploy/` | launchd 常驻脚本 |

## 飞书配置

- 正式模式只接受飞书 OAuth 真实身份登录。
- `.env` 必须配置 `DISPATCH_APP_ID`、`DISPATCH_APP_SECRET` 和 `DISPATCH_BASE_URL`,具体见配置指引第 6 步。

## 测试模式

在申请飞书应用前,`.env` 设置:

```bash
DISPATCH_TEST_MODE=1
```

重启后登录页提供团队端、师傅端、管理端三个测试身份。所有操作只写入
`app/dispatch-test.db`,不会污染正式库,也不会发送飞书通知。

飞书应用配置完成后,把 `DISPATCH_TEST_MODE` 改为 `0`,再填写三个飞书配置并重启,
系统会直接使用正式库 `app/dispatch.db`。

## 权限模型

| 角色 | 能干什么 |
|---|---|
| team(默认) | 下单、看绿灯板和单据 |
| staff | + 报自己的状态、抢单、完成自己的单 |
| admin | + 派单给任何人、人员/团队/SLA 增删改、看审计日志 |

- 所有权限在**服务器端强制**,改前端绕不过去;所有变更写审计日志。
- 首个管理员:服务器启动日志里的 6 位**引导码**,登录后在页面横幅输入认领(一次有效)。
- 之后加管理员:管理端 → 人员管理 → 把某人角色改为"管理员"。

## 备份 / 恢复

```bash
# 在线备份(SQLite 会把 WAL 中已提交的数据一起写入备份文件)
sqlite3 app/dispatch.db ".backup '$HOME/Desktop/dispatch-$(date +%m%d).db'"

# 恢复前先停常驻服务,恢复后重新安装并启动
bash scripts/deploy/install.sh remove
RESTORE_TRASH="$HOME/.Trash/dispatch-before-restore-$(date +%Y%m%d%H%M%S)"
mkdir -p "$RESTORE_TRASH"
for file in app/dispatch.db*; do
  [ -e "$file" ] && mv "$file" "$RESTORE_TRASH/"
done
cp ~/Desktop/dispatch-0711.db app/dispatch.db
bash scripts/deploy/install.sh
```

## 迁云(以后想全网可用时)

1. 买一台轻量云主机(腾讯/阿里,¥100-300/年,选上海/广州)
2. 先按上方 `.backup` 命令生成最新备份,再上传 `server.py`、`index.html`、备份文件和 `.env` 三个 DISPATCH_ 值到服务器
3. 把备份文件放为 `/opt/dispatch/dispatch.db`,运行 `python3 /opt/dispatch/server.py`,并配置 systemd 常驻
4. `.env` 的 `DISPATCH_BASE_URL` 改成 `http://云IP:8848`;飞书后台改两处:网页应用主页 URL、安全设置回调 URL
5. 完成 —— 代码零改动,手机 4G 也能用,消息卡片按钮等公网能力也解锁

## 排障

| 现象 | 处理 |
|---|---|
| 手机打不开 | 手机和 Mac 要同一 Wi-Fi;IT 是否开了 AP 隔离;Mac 防火墙放行 python3 |
| 登录 redirect_uri 不匹配 | 飞书后台回调 URL 与 `.env` 的 `DISPATCH_BASE_URL` 必须完全一致 |
| 收不到私信 | 飞书后台 im 权限已开?版本已发布?可用范围含该员工? |
| 端口被占 | `lsof -ti:8848 \| xargs kill` |
| 想重置全部数据 | 先停服,再把 `app/dispatch.db*` 移入废纸篓后启动;团队/SLA 会从 data.json 重新迁入 |
