# 健康运维、聊天指令与人工核账实施计划

> 执行方式：并行实现互不改动同一文件的运维与核账模块，主会话实现聊天和集成；测试、审查后合入远端main再发布。

**Goal:** 在免费解析额度等待期完成可观测性、无外部接口依赖的聊天操作、可审计人工核账。不得购买资源、启用付费解析或自动支付。

**Architecture:** 保留FastAPI、SQLite和现有安全模式。用现有claims表做兼容迁移，CLI管理核账，公众号处理用户指令；管理动作不提供公开HTTP写接口。固定域名与常驻云服务先准备操作方案，缺资源时明确保留限制。

## Task 1 健康与恢复
- 新增 `shadow_saver/operations.py::install_operations(app, db, config)`；/healthz进程探活、/readyz数据库就绪、/ops/status仅Bearer管理令牌可读。
- 结构化日志只记标准化路由、状态、耗时和错误类别，禁止消息内容、OpenID、入口令牌、签名、密钥和完整URL。
- `scripts/service_ops.py` 支持status、doctor、backup、recover；默认只读，recover显式调用且只恢复确切已发布镜像，不自动重建临时域名、不更改微信配置。数据库备份使用SQLite在线备份，权限600。
- 测试健康状态、失败诊断、管理鉴权、日志脱敏、备份一致性；编写 `docs/OPERATIONS.md` 明确本机临时部署与固定域名常驻部署的差别。

## Task 2 核账模块
- `shadow_saver/ledger.py::Ledger(db)` 迁移claims，保留原字段。API: register(owner,platform,order_no,promotion_id=None,title='') ->dict; list_orders(owner)->list[dict]; get_order(claim_id)->dict; transition(claim_id,action,actor,**kwargs)->dict; audit(claim_id)->list[dict]。
- register为同用户同平台订单幂等，其他用户已登记时拒绝；没有promotion的聊天登记仅是待核账线索。
- 状态：待人工核账 -> 已匹配 -> 待结算 -> 可返利 -> 已返款。无效、无返利、已退款及退款待处理需独立状态。只有匹配凭据和结算凭据俱全后才能记入可返利。
- transition actions: match(evidence), await_settlement(), settle(commission,evidence), record_payment(amount,evidence), refund(evidence), resolve_refund(recovered,evidence,note), invalidate(evidence)。所有管理动作必须actor与凭据，await_settlement只需actor；付款记录不执行支付。
- 金额统一整数分+Decimal输入，仅实际已结算佣金按20%四舍五入至分；负数、NaN、无限、超大值、超出两位小数输入拒绝。已返款后退款进入人工处理，保留历史付款，不自动扣款。审计前后状态、操作者、凭据且与状态更新同一事务。
- 新增本地 `scripts/manage_orders.py`，公开页面/用户聊天无修改金额、结算、返款权限。测试迁移、非法跳转、并发重复、精度、退款、审计。

## Task 3 聊天与集成
- 新增 commands.py；帮助/菜单/你好、返利规则、登记订单 淘宝|京东 数字订单号、我的订单，全部本地处理，不调用解析或搜索接口。
- 身份使用现有公众号hmac规则，与手机网页记录一致。我的订单显示最近5条，掩码订单号，不把预估返利当实收。
- /api/claims兼容旧契约并接入Ledger，所有查询按owner筛选；公众号管理命令无权限入口。
- 集成operations、给镜像加健康检查；全测试+独立审查+密钥扫描后提交推送main，从干净远端提交构建，镜像内重跑测试，备份后发布并记录SHA/摘要/环境。

## Review Focus
- 历史claims迁移不丢数据，预估金额不变成可返款金额。
- 公众号和网页相同身份能看到自己的订单，不读其他用户订单。
- 退款与重复记款不造成二次付款，状态/审计原子更新。
- 管理令牌不写进日志，公网错误不包含正文或入口令牌。
- 恢复脚本不悄悄生成新域名导致微信回调失联；当前固定云资源尚未选择。
