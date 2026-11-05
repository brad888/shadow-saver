# 多通道 IM 接入

## 已实现的结构

```text
公众号：微信验签/解密 → IMMessage ┐
个人微信：wxauto → HTTPS bridge ├→ IMGateway → 订单指令 / 推广转链 / 查询入口
未来通道：官方SDK/Webhook适配器 ┘          ↓
                                 IMReply(text) → 原通道发送
```

`shadow_saver/im.py` 不依赖微信、QQ或支付宝的SDK。统一消息包含 `channel/account_id/user_id/conversation_id/message_id/kind/content`；当前回复为文字。`im_business.py` 负责共享业务编排，复用已有订单账本和转链服务，不复制各平台业务。

- 公众号的 XML 验签、加解密仍由 `app.py` 负责，既有活动入口继续保留。
- 第一个公众号账号的 owner 算法保持不变，因此原有订单/登录身份不迁移。其他通道及账号采用命名空间身份，昵称相同也不能互看订单。
- 不同通道身份默认独立；没有自动绑定账号。未来如需共用订单必须增加双边验证绑定流程。
- 消息去重范围为通道、接入账号、会话和平台消息ID，保留24小时；正文仅保存指纹及回复。同ID不同正文拒绝，处理中30秒内重试返回409，不重复执行业务；任务取消或进程中断留下的未完成记录不自动清理、重执，后续重试超过30秒转为“结果不确定”终态，提示核对订单或发新消息。缓存完成回复保留24小时，超过保留窗口不承诺去重；每用户每分钟最多20条新消息。
- HTTP返回已缓存的回复不代表适配器已发出。适配器负责出站投递状态，禁止将网络响应当成真实平台送达凭证。

## 可选 HTTP bridge

默认关闭。管理员在私密运行配置中设置 `IM_BRIDGES_JSON`，内容为数组（JSON须放在环境变量的一行中）。以下为结构示意，密钥必须另行随机生成且至少32字符：

```json
[{"id":"desktop","channel":"wechat_personal","account_id":"desktop-account-01","token":"REPLACE_WITH_A_PRIVATE_RANDOM_TOKEN_AT_LEAST_32_CHARS"}]
```

一个凭据只能向指定通道和接入账号提交消息，不能在正文选择其他通道。公众号保留独立官方鉴权，不允许通过bridge冒充 `wechat_official`。配置错误会阻止服务启动；同一个凭据不能共享给不受信任客户端。bridge属于受信任的服务器侧适配器，持有凭据者可以代表其账号下用户提交消息，不能把它嵌入网页或分发给终端用户。生产环境建议同时限制来源网络。

```http
POST /im/desktop/events
Authorization: Bearer <private-token>
Content-Type: application/json

{"user_id":"locally-assigned-contact-01","conversation_id":"private:contact-01","message_id":"platform-message-id","kind":"text","content":"帮助"}
```

响应为 `{"text":"...","duplicate":false}`。错误：404桥接未配置，403鉴权失败，409消息冲突/处理中，422格式不支持，429限流。不要把令牌放进URL。当前不支持bridge文件上传，图片仅返回识图未开放提示；不能把个人微信图片路径当成公众号素材ID使用。

## Windows 个人微信连接器（待实机验证）

可选模块为 `python -m shadow_saver.wxauto_bridge`，后端不导入wxauto。现有Mac仅运行假客户端/HTTP模拟测试，没有登录或控制个人微信。Windows上需自行安装与微信版本匹配、许可适用的 `wxauto4` 或 `wxautox4`；服务端不自动安装、不购买Plus、不规避账号限制。

私密配置示例：

```json
{
  "base_url":"https://your-existing-backend.example",
  "bridge_id":"desktop",
  "token":"SAME_PRIVATE_TOKEN_AS_THE_SERVER",
  "library":"wxauto4",
  "contacts":[{"chat_name":"唯一且已核实的测试好友备注","user_id":"contact-01"}]
}
```

`user_id` 是管理员为已核实联系人分配的稳定本地ID，不是昵称，更不是自动获取的微信账号ID。重命名时保留该ID；不得给其他人复用，或把同名联系人随意映射到同一ID。连接器只监听配置会话，并检查好友消息类、发件人、窗口名称及 `chat_type=friend`；拒绝群聊、自己的回复和不匹配窗口。最多10个明确配置联系人；不自动添加好友，不监听所有聊天。先用专用测试好友进行双边验证，文件传输助手若被库识别为self会被过滤。

```powershell
python -m shadow_saver.wxauto_bridge --config C:\private\bridge.json --state C:\private\delivery.db
```

本地 `delivery.db` 持久记录发送结果；Windows需用NTFS ACL限制私密配置、状态文件所在目录（Python chmod不能替代Windows ACL）。发送前先标记结果不确定；调用出错、返回未知或程序中断后不自动重发，需人工核对后处理。网络失败发生在发送前时允许重试相同事件，服务器会复用处理结果。当前没有后台自动重试队列、发送确认查询或模糊状态自动恢复。

**重要边界：wxauto文档说明 `msg.id` 是UI ID，切换UI后会变化，`hash`也可能重复。** 连接器使用UI ID，只能在该ID保持不变时去重，无法保证跨微信重启/窗口重建绝不重复。保持监听窗口，避免重放历史消息；遇到窗口重建先人工检查。该桌面连接器属于实验接入，不可当成具有稳定消息ID与送达回执的官方IM服务。

## 扩展 QQ、支付宝等

实现平台适配器：验证官方签名/身份 → 映射统一消息 → 调用gateway → 将文字回复发送回平台，并处理平台专有超时、权限及消息ID。平台账号与终端用户不能混为同一身份。

本轮只验证了自定义 `qq`、`alipay` 名称能被核心处理和隔离；**没有申请、取得或联调任何QQ/支付宝消息接口**。尤其支付宝不能预设为可任意监听个人私聊的通用IM通道，应先确认开放产品及其事件/回复能力。

无论入口是哪一个，原始商品分享内容转链仍受联盟接口权限和好单库额度约束。当前 `DIRECT_LINK_ENABLED=0` 保持不变。

## 上线验证

公众号原入口无需用户修改。HTTP bridge须明确配置私密凭据并从远端main镜像发布后才启用；Windows连接器另行在用户设备运行。公网探针通过只能证明后端可用，不能代替个人微信客户端实测。

参考：[wxauto消息属性](https://docs.wxauto.org/docs/class/Message.html)、[聊天窗口类型](https://docs.wxauto.org/docs/class/Chat.html)、[监听示例](https://docs.wxauto.org/docs/example.html)。

## Mac及Linux云端

Mac原生驱动和共用桌面轮询见 [MAC.md](MAC.md)，当前本机微信4.2辅助功能控件不完整，未完成自动收发实测。Linux云部署与桌面驱动边界见 [CLOUD.md](CLOUD.md)。
