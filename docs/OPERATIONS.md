# 影省狸健康检查与恢复

## 当前能力与部署边界

`/healthz` 只检查进程响应，返回 `{"status":"alive"}`。`/readyz` 通过现有数据库连接执行 `SELECT 1`，成功返回200，失败返回503及固定错误文本。探针不登记用户、订单或调用商品解析接口；它不证明微信、联盟接口、磁盘剩余空间或数据库写入正常。

`/ops/status` 仅允许 `Authorization: Bearer <OPS_STATUS_TOKEN>`，未配置时404，令牌错误或缺失时403；查询参数不接受管理令牌。将随机管理令牌保存在权限600的运行配置文件中，由部署流程注入容器，勿放入URL、代码、工单或聊天记录。接口返回进程启动时长、通过格式校验的源码提交、数据库就绪状态，以及标准化路由的请求数、错误数、累计/最大耗时。计数按进程保存在内存中，重启清零，多进程分别计数，不代表业务成交或实际返款金额。

应用结构化日志只包含路由模板（如 `/open/{token}`）、HTTP状态、耗时和固定错误类别。未匹配路径记为 `<unmatched>`；不记录请求URL、查询参数、请求体、Cookie、授权头、OpenID、订单号、微信签名或异常正文。公网意外异常返回固定500文本。运行Uvicorn时必须保持 `--no-access-log`；反向代理、隧道、平台访问日志也需禁用完整URL或查询串，不能依赖应用中间件替这些组件脱敏。

## 本地操作命令

在仓库根目录使用已安装项目依赖的Python运行。`status` 和 `doctor` 只探测网络与读取指定Docker字段，不输出容器环境变量。按实际容器名称及现有公网域名替换示例：

```sh
.venv/bin/python scripts/service_ops.py status --container shadow-saver --local-url http://127.0.0.1:8000
.venv/bin/python scripts/service_ops.py doctor --container shadow-saver --local-url http://127.0.0.1:8000 --public-url https://shop.example.com --tunnel-container existing-tunnel
```

`--public-url` 必须使用现有源站的HTTP(S)地址，不能含凭据、查询参数或片段。公网探针失败但本地成功，优先检查DNS、TLS、反向代理和现有隧道。指定的隧道容器缺失时报告 `tunnel_missing`；宿主机进程运行的cloudflared需人工检查，CLI不会搜索进程命令行（其中可能有令牌）。未传公网地址会明确报告 `public_endpoint_not_checked`。诊断输出用于判断原因，不自动执行修复；CLI退出0表示诊断已执行，实际健康以输出字段为准。

数据库备份为显式操作，使用SQLite在线备份，包含已提交的WAL内容；目标文件必须尚不存在，权限600，并执行 `integrity_check`。源数据库只读打开，不创建缺失源文件。备份包含用户数据，按敏感数据保管，不提交Git；必须使用当前真实挂载路径，并分别备份所有数据库：

```sh
.venv/bin/python scripts/service_ops.py backup --database /absolute/data/pilot.db --output /private/backup/pilot-YYYYMMDD-HHMM.db
.venv/bin/python scripts/service_ops.py backup --database /absolute/data/queries.db --output /private/backup/queries-YYYYMMDD-HHMM.db
```

父目录需提前创建并限制访问。多个数据库的备份各自一致；若需要跨数据库同一时点的灾备，应先安排维护窗口停止写入。备份脚本不会压缩、上传或删除历史备份。恢复数据库需人工安排维护窗口、先备份现有数据库并验证备份，停止所有写入连接后恢复到正确挂载目录；不要在运行的SQLite上直接覆盖文件或只复制主文件而忽略WAL。

## 显式恢复已有容器

`recover` 只重启指定的已有容器，保留该容器原有镜像、环境配置和数据挂载；不执行 `docker run`、构建、拉取镜像、替换数据库或创建隧道。它检查私有 `runtime.env` 存在，但不读取、打印或重新加载文件内容。修改配置文件不会通过此命令生效。

恢复前校验容器镜像ID为确切 `sha256`、容器与镜像的OCI revision标签一致，查询远端main并确认本地 `origin/main` 与之相同，再检查该提交已包含在 `origin/main`。网络、标签或来源无法核实时拒绝恢复。它依赖已有发布记录和构建流程证明镜像来自干净源码；revision标签本身不构成构建证明。

```sh
git fetch origin main
.venv/bin/python scripts/service_ops.py recover --container shadow-saver --config /absolute/private/runtime.env --repository /absolute/path/shadow-saver
.venv/bin/python scripts/service_ops.py doctor --container shadow-saver --public-url https://shop.example.com --tunnel-container existing-tunnel
```

停止的已有容器也可以通过此命令恢复。Docker重启命令成功仅表示容器已启动；随后需检查本地与公网就绪及真实微信回调。容器被删除、镜像缺失、来源不明或数据库损坏时此命令不能重建服务，应依据发布记录人工处理。重启应用不能恢复宿主机睡眠、断网或中断的临时隧道。

## 固定域名与常驻托管操作方案

本机Docker加临时quick tunnel依赖电脑开机联网，临时域名可能变化，不能承诺长期回调可用。不要为了恢复自动创建新quick tunnel；新域名会导致公众号服务器配置、菜单或历史入口失联。当前云资源、固定域名和托管平台尚未选定，本方案不购买或创建资源。

选择固定域名与常驻云服务后，发布负责人按以下顺序执行：

1. 确认域名控制权、HTTPS证书、平台适用的备案/接入要求、预算、区域与数据备份责任。使用常驻容器主机加反向代理，或支持持久磁盘的容器托管；SQLite需要保留单实例写入语义，不将临时容器文件系统作为数据库存储。
2. 将域名DNS指向所选入口，配置HTTPS与现有源站连接。若选Cloudflare Tunnel，使用已命名的固定隧道及固定hostname，独立保管隧道凭据；为其配置重启策略和后台运行机制。无固定域名和凭据时停止在此，不换成新的临时地址。
3. 先测试并提交代码，合入远端main。从干净的确切远端main提交构建，设置 `SOURCE_COMMIT` 与OCI revision，运行镜像内测试，记录仓库、完整SHA、镜像摘要、目标环境及数据挂载。拒绝使用功能分支、未提交工作区或仅有可变镜像标签的构建发布。
4. 将私有配置通过宿主机权限600文件或平台密钥注入，持久挂载所有数据库，设置应用与入口进程的重启策略、就绪探针及资源限制。应用管理状态接口可额外限制在管理网络；外部监控只需读取公开探针。
5. 发布前做在线备份；验证本地/公网 `/readyz`、HTTPS和固定hostname，再按实际固定域名更新公众号服务器配置、域名白名单及菜单。验证微信安全模式回调、登录入口、本人订单隔离与不依赖外部解析的聊天指令。
6. 保留上一确切main镜像与备份，检查重启及宿主机重启后的自动恢复，配置独立探针告警、备份保留策略和恢复演练。数据库结构有升级时，回滚镜像前验证旧代码对新结构兼容；数据库恢复始终需要维护窗口。

发布记录必须包含仓库、提交SHA、镜像摘要和目标环境，并记录实际域名、容器/挂载位置和验证结果。不要在发布记录中记录令牌、OpenID或私有配置内容。
