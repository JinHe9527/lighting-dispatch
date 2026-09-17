# CI 与部署

## 当前部署状态（2026-09-17 16:04 CST）

- 普通 main push `780fcf70d0b803f3ba3c9eb6b36b947e030d3754` 触发
  [CI 35197411133](https://github.com/JinHe9527/lighting-dispatch/actions/runs/35197411133)：
  80 项 Python 测试、完整浏览器冒烟、制品生成均通过；同一工作流的 `deploy` job 于
  16:02:58 CST 成功，没有手工 dispatch 或人工投递应用包。
- 公司新机 `123.206.229.200` 的 `current`、`RELEASE`、`/api/health` 中的 revision 均为该 SHA，
  `wuyou-dispatch.service=active/running`、重启计数 0，包内 SHA-256 全部通过。
  数据库 `quick_check=ok`，用户和工单均为 0；调度状态 `ok`，没有失败或卡住的通知。
- 首页、Manifest、4 个图标、Lucide 脚本均返回 200；匿名 `/api/state` 返回 401，
  `/auth/test` 返回 404。浏览器通过 SSH 隧道实际打开新机页面，显示 `v0806a` 和
  “等待飞书配置”。没有开启测试登录，也没有导入旧业务数据。
- 启动后已生成同盘备份 `dispatch-20260917-160257.db`（212992 字节）。同盘备份不覆盖
  整机故障，异地备份尚未配置。微信/飞书真实登录、通知和外部平台审核仍待单独验收。
- 金河分镜同时健康运行 `e6ceb88`；应用分别只监听 `127.0.0.1:8848`、
  `127.0.0.1:18787`，公网监听仍只有 SSH 22，DNS/Nginx/防火墙没有随发布更改。
- 仓库级 `SERVER_DEPLOY_ENABLED=true`，主机为新机，账号为 `wuyou-deploy`；
  独立 `SERVER_SSH_KEY`、固定 `SERVER_KNOWN_HOSTS` 已由真实 CI 使用成功。
  后续 main push 测试通过后自动部署，不需要每次手工执行 deploy。

本节记录首次自动发布的验收 SHA，后续提交会正常继续自动部署；查最新运行版本使用
`/api/health` 和对应 GitHub Actions 运行记录，不把文档中的历史 SHA 当成固定目标。

## 接入与 CI 修复记录

- 独立本地分支 `codex/company-server-deploy` 基于远端
  `6f108c1a343f96e86a08651ed60cd9fc4b729172`（应用 `v0806a`），保留其微信小程序和业务更新。
  原本地 `main=03d860d` 未修改；其独有历史尚未合并，不用旧版本覆盖远端。
- 仅移植本次部署脚本、CI 与完整 SHA 健康标识；补齐 Web Manifest 和 4 个图标的打包清单。
  干净测试库缺少团队的失败已复现，修复仅在 `TEST_MODE` 下补测试团队，不影响正式数据。
- 当前 80 项 Python 测试通过，包含发布包、失败回滚和远端已有微信账户用例；
  微信/飞书外部调用采用测试替代，不代表真实供应商认证通过。
- 用户已将仓库改为私有，GitHub API 已复核 `isPrivate=true`；源码可见性阻塞已解除。
  独立发布公钥已安装，新机真实受限 SSH 协议验证通过；两个仓库级 Secrets 和主机变量已配置。
- 首次真实 CI `35195576295` 在依赖缓存阶段发现远端缺少 `pnpm-lock.yaml`，本次补入固定版本锁文件。
  新建 `production` 环境返回 404，查询确认仓库没有任何环境；移除本轮新增但未生效的环境依赖，
  使用仓库级 Secrets，不移除既有审批规则。发布仍要求 main push、测试成功、开关开启和受限 SSH 校验。

- 随后真实 CI 暴露浏览器用例依赖旧本地数据：团队优先级用例现显式准备两间本职直播间和
  一间非本职直播间，并恢复页面状态；`TEST_MODE` 空库的测试直播间带编号，匹配短任务编号
  展示用例。没有删除断言、跳过浏览器测试或改正式数据库。

上述首次自动发布及回环验收已完成，公网和供应商验收仍未完成。
不得 force push 覆盖远端功能。

## CI

工作流位于 `.github/workflows/ci.yml`，在以下事件运行：

- 向 `main` 推送
- 创建或更新拉取请求

CI 使用 Python 3.12、Node.js 22 和 pnpm 11.4，执行：

1. 安装 `pnpm-lock.yaml` 锁定的 JavaScript 依赖。
2. 检查 Python、Linux 安装脚本和 macOS 安装脚本语法。
3. 运行 Python 业务与网络回归。
4. 启动端口 18848 的隔离测试服务，安装 Playwright Chromium，运行四类角色浏览器冒烟。
5. 生成原手动安装包 `lighting-dispatch-linux.tar.gz`，以及按 commit 命名的仅应用发布包，均保留 14 天。

发布包包含应用代码、Linux 服务配置、README 和文档，不包含：

- `.env` 或 `/etc/wuyou-dispatch.env`
- SQLite 数据库及 WAL/SHM 文件
- 业务附件和日志
- TLS 私钥和证书
- Windows Python 运行时

默认只验证和打包；只有 main 的 push 测试成功且 `SERVER_DEPLOY_ENABLED=true` 才运行发布 job。
工作流不取消正在执行的部署；已不是 main 最新版本的提交跳过发布。

### 公司新机发布准备（2026-09-17）

新机 `123.206.229.200` 已运行金河分镜。12:48 CST 安装了无忧排班运行账号、发布账号、
固定发布命令与 systemd；独立 SSH 发布密钥随后安装，16:02 CST 已由 GitHub 自动发布启动应用。
域名继续停用。不要在新机直接运行下文旧的 `install-linux.sh`：它会修改 SSH、启用 Nginx 并开放公网端口。
受限自动发布必须只更新应用，不能允许 CI 安装包覆盖主机服务或网络配置。

`lighting-dispatch-app-<40位SHA>.tar.gz` 只含以下文件，另附外层 `.sha256`：

```text
RELEASE                            # 完整 Git commit SHA
SHA256SUMS                         # 以下应用文件及 RELEASE 的摘要
app/
├── server.py
├── business.py
├── service.py
├── index.html
├── manifest.webmanifest
├── app-icon.svg
├── app-icon-180.png
├── app-icon-192.png
├── app-icon-512.png
└── vendor/lucide.min.js
```

打包器会自检；`verify` 同时检查外层摘要、固定文件集合、普通文件类型、重复条目、大小上限、
包内摘要与期望 commit，不提取或执行归档。拒绝路径穿越、软/硬链接与额外文件；不打包凭据、
数据库、附件、systemd、Nginx 或 SSH 配置。原手动安装包保持不变。

```bash
python3 scripts/deploy/release.py build . "$REVISION" "/tmp/lighting-dispatch-app-$REVISION.tar.gz"
python3 scripts/deploy/release.py verify "/tmp/lighting-dispatch-app-$REVISION.tar.gz" "$REVISION"
```

该工具使用 Python 3.9+ 标准库，无新增依赖。生成文件必须尚不存在；校验时归档和 `.sha256` 应放在同一目录。
这是自动部署的制品准备，不是目标主机已发布的证明。GitHub 仓库已改为私有，真实发布结果单独记录。

### 受限自动部署

管理员仅在新机首次接入或运维脚本更新时执行：

```bash
sudo bash scripts/deploy/host-bootstrap.sh /path/to/wuyou-deploy.pub
```

公钥参数可省略；已有公钥不会被覆盖。脚本创建独立的 `dispatch` 运行用户与
`wuyou-deploy` 发布用户，安装固定 systemd 单元、受限命令与 sudoers，不启动应用，
不安装 Nginx、不修改 SSH 主配置或防火墙。空的飞书凭据表示尚未开通真实登录；管理员另行填写
`/etc/wuyou-dispatch.env`。没有导入旧账号、旧数据库或旧附件。

首次使用不带公钥参数的初始化，随后另行安装独立发布公钥。主机上的运维摘要为
`4e6ff14580869aaac2a7305fd4d71fb3d7d0ec49a2954059dd1ef74c2d560366`。
以 `sudo -u wuyou-deploy` 验证协议返回、拒绝任意 `id` 命令；sudoers 仅允许重启/停止本服务，
`dispatch` 不能写应用目录。随后使用独立密钥以 `wuyou-deploy` 登录，真实 SSH 协议验证通过。

发布密钥由 `restrict,command=...` 限制为 `protocol`、`upload <SHA> archive|checksum`、
`deploy <SHA>`。上传与发布共用非阻塞锁，600 秒触发中断；切换后的失败会尝试回滚，
重启命令另有 90 秒超时、健康检查另有有限重试。运行账号不能写发布目录。
发布账号不具有通用 sudo，只能重启或停止 `wuyou-dispatch.service`；不能更新 root 所有的
脚本、unit 或运行凭据。服务只监听 `127.0.0.1:8848`，内存上限 512MB。

在目标仓库分别配置，不复用金河发布私钥：

| 名称 | 类型 | 值或用途 |
|---|---|---|
| `SERVER_HOST` | Variable | `123.206.229.200` |
| `SERVER_PORT` | Variable | `22` |
| `SERVER_USER` | Variable | `wuyou-deploy` |
| `SERVER_DEPLOY_ENABLED` | Variable | 设 `true` 启用 main 自动发布；停发设 `false` |
| `SERVER_SSH_KEY` | Secret | 无忧排班独立受限私钥 |
| `SERVER_KNOWN_HOSTS` | Secret | 从可信控制台核对的新机 SSH 公钥 |

CI 校验应用包和主机运维摘要，通过受限 SSH 投递。服务器校验后写入
`/opt/wuyou-dispatch/releases/<SHA>`，原子切换 `current`，重启并核对 `/api/health` 的
`ok` 与完整 `revision`。重复发布同一 SHA 必须文件一致；失败时恢复上一链接并复验健康。
首次发布失败则移除本次创建的链接并停止服务，保留失败版本供排查。

回滚只针对应用版本，不自动回滚数据库。既有启动/每日备份策略保持不变；有不向后兼容的数据
迁移时，需另行安排备份及恢复验证。当前保留所有发布版本和传入归档，不自动删除历史文件。
修改 `release.py` 或 systemd 单元后，先在主机更新这两份文件；摘要不匹配会拒绝发布。
域名、HTTPS、飞书应用及真实账户验收不随代码 push 自动开通。

测试不等于新机验收：本地回滚测试替代了 systemd 与健康探针；真实成功发布证据见本文顶部，
没有在新机故意制造故障来验证回滚。此前 47 项测试与临时包浏览器记录属于旧本地基线，
不得用作当前 `v0806a` 的浏览器验收结果。

当前验收：80 项 Python 测试、Bootstrap ShellCheck、工作流 YAML/Bash/ShellCheck 通过。
将本地提交 `6e1b326413b501e145049ff92f42d9b9a9f2accc` 的应用包解包到临时目录，
独立 SQLite、测试模式、回环端口 18849；健康接口返回该 SHA、`v0806a`、数据库与调度 ok，
5 个图标/Manifest URL 返回 200。浏览器验证管理员登录、人员与总览页面，无 error/warn。
验收后已停止临时进程，保留临时文件；这不是远端 CI 或新机应用验收。

公网恢复前还需更换页面原有的个人 ICP 备案标识、配置公司的微信/飞书凭据并验证真实登录。
本次没有修改官网文案或恢复原域名解析。

### 本地执行同类检查

```bash
nr test
python3 -m py_compile app/server.py app/business.py app/service.py
bash -n scripts/deploy/install-linux.sh scripts/deploy/install.sh
```

浏览器冒烟需要先运行测试服务：

```bash
DISPATCH_TEST_MODE=1 DISPATCH_PORT=18848 python3 app/server.py
```

另开终端执行：

```bash
ni
nr test:ui
```

## Linux（原手动安装流程）

### 运行条件

- Ubuntu 22.04 或 24.04
- Python 3.9 或更高版本
- systemd、Nginx、OpenSSH、Fail2ban、UFW、curl
- 可使用 `sudo` 的部署账号
- 生产域名和 TLS 终止方案

安装脚本会使用以下目录：

| 路径 | 所有者与用途 |
|---|---|
| `/opt/wuyou-dispatch/releases` | `root:dispatch`，只读版本 |
| `/opt/wuyou-dispatch/current` | 指向当前版本的原子链接 |
| `/var/lib/wuyou-dispatch` | `dispatch` 可写，数据库、附件和日志 |
| `/var/backups/wuyou-dispatch` | `dispatch` 可写，SQLite 与附件备份 |
| `/var/backups/wuyou-dispatch-config` | `root` 可读，服务配置快照 |
| `/etc/wuyou-dispatch.env` | `root` 可读，生产凭据 |

### 首次配置

先创建生产配置：

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/wuyou-dispatch.env
sudoedit /etc/wuyou-dispatch.env
```

内容至少包括：

```text
DISPATCH_APP_ID=cli_xxxxxxxxxxxxxxxx
DISPATCH_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DISPATCH_BASE_URL=https://dispatch.example.com
DISPATCH_TEST_MODE=0
```

从 CI 下载并解压 Linux 发布包到独立目录，然后执行：

```bash
cd /home/ubuntu/wuyou-dispatch-release
bash scripts/deploy/install-linux.sh "$PWD"
```

也可以直接上传仓库检出并运行同一命令。脚本兼容 `scripts/deploy/` 布局和旧的平铺发布目录。

### 发布行为

安装脚本按以下顺序执行：

1. 检查代码、静态资源、systemd、Nginx、SSH 和 Fail2ban 文件。
2. 保存当前服务配置和启用状态。
3. 把新代码安装到带时间戳的只读版本目录。
4. 对 Python 源码做语法解析。
5. 停止旧服务，通过 SQLite backup API 保存部署前数据库；首次从旧目录迁移时也使用同一 API，不直接复制活动中的 WAL 文件。
6. 核对页面和服务版本号一致，再原子切换 `/opt/wuyou-dispatch/current`。
7. 重启服务并轮询 `/api/health`，同时核对健康接口版本与新代码中的 `SERVER_VERSION`。

健康检查失败时，脚本恢复旧链接、服务配置、数据库迁移前快照和部署前的服务启用状态。

### 验证与日志

```bash
curl --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8848/api/health
sudo systemctl status wuyou-dispatch --no-pager
sudo journalctl -u wuyou-dispatch -n 100 --no-pager
sudo nginx -t
sudo sshd -t
```

健康响应至少要满足：

```json
{
  "ok": true,
  "db": "ok",
  "scheduler": "ok",
  "stuck": 0,
  "failed": 0
}
```

`version` 必须等于本次发布版本。`pending` 可以短暂大于 0；持续增长时要检查飞书权限和网络。

### HTTPS

仓库中的 `nginx-wuyou-dispatch.conf` 只监听 80 端口，适用于上游负载均衡器已经终止 TLS 的场景。直接把主机暴露到公网时，必须补充证书、443 监听和 HTTP 到 HTTPS 跳转，再把 `DISPATCH_BASE_URL` 设置为同一 HTTPS 地址。

在 HTTPS 未生效前，不要验收 OAuth 登录，也不要让真实账号使用系统。应用只会在 `DISPATCH_BASE_URL` 以 `https://` 开头时写入 Secure Cookie 和 HSTS。

### 备份与恢复

服务启动前和每日后台任务都会通过 SQLite backup API 创建快照，默认保留最近 30 份。附件以不可变文件方式同步到备份目录。设置 `DISPATCH_BACKUP_MIRROR_DIR` 后会把数据库和附件再同步到挂载目录。

上线前至少做一次隔离恢复演练：

1. 停止验收实例，不停止生产实例。
2. 把选定快照恢复到新的临时数据目录。
3. 先用 SQLite 只读连接检查 `PRAGMA integrity_check`、`PRAGMA foreign_key_check`、用户和工单。
4. 如果需要界面验收，再复制一次恢复库，在隔离网络中以测试模式和不同端口启动副本，避免向真实飞书账号发送通知。
5. 对照数据库中的附件路径检查文件存在性；验收结束后停止副本并保留恢复日志。

不要用文件复制覆盖正在运行的 `dispatch.db`。

## macOS

macOS launchd 适用于办公室局域网或验收机，不作为公网生产方案。

```bash
bash scripts/deploy/install.sh
```

脚本会把当前 Python、仓库和日志绝对路径写入用户 LaunchAgent，检查 8848 端口冲突，并等待健康接口成功。卸载：

```bash
bash scripts/deploy/install.sh remove
```

卸载只移走 LaunchAgent，不删除代码、数据库、附件或日志。

## Windows

Windows 自包含发布目录需要包含：

```text
python\python.exe
app\server.py
app\business.py
app\service.py
app\index.html
app\vendor\lucide.min.js
start-service.bat
restart-service.bat
```

更新时保留 `.env`、`app\dispatch.db`、`app\media` 和 `app\backups`，然后运行 `restart-service.bat`。当前 CI 不构建 `python\python.exe`，所以 Linux 发布包不能直接作为 Windows 自包含包使用。
