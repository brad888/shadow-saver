# Mac 微信连接器

## 已完成与当前限制

新增 `shadow_saver.desktop` 共用桌面轮询层、`shadow_saver.mac_bridge` Python连接器和 `native/mac_wechat.swift` 原生辅助功能驱动。无需安装Windows，不依赖第三方机器人库，不读取微信数据库或解密聊天记录。

本机微信版本4.2.0。CUA实机检查中，辅助功能树只暴露窗口和三个标准按钮，没有聊天消息/输入框，控件交互还返回 `AXError.notImplemented`。这意味着当前机器的**辅助功能路径不具备可自动收发的条件**。只看到截图并不证明原生驱动能可靠读取消息；当前没有完成Mac自动收发实测，不得宣传为已跑通。

部分公开库会把发送者无法判断的消息默认当作收到；本项目保持unknown，不做这种猜测。驱动在控件不完整、消息行多义、非空/不可读草稿或窗口不匹配时停止。当前仅能校验显示名，无法认证真正的特殊会话身份，因此首版彻底禁用真实发送；原生驱动不包含按键合成或输入框写入代码。

用户已明确选择暂不做OCR。当前不实现或启用OCR，也不申请录屏权限。不会静默回退成坐标点击或盲发消息。

## 原生驱动构建

本机需有Apple Command Line Tools。发布/运行前先测试、提交远端main，再从干净的main源码构建。示例中的编译缓存与二进制都放私密任务目录，不污染用户系统缓存：

```sh
mkdir -p .mobile-data/bin .mobile-data/swift-cache
chmod 700 .mobile-data
xcrun swiftc -module-cache-path .mobile-data/swift-cache native/mac_wechat.swift -o .mobile-data/bin/mac-wechat
.mobile-data/bin/mac-wechat --self-test
python -m shadow_saver.mac_bridge doctor --helper .mobile-data/bin/mac-wechat
```

`--self-test` 只验证本地解析/协议函数，不访问微信。`doctor` 只返回固定诊断状态与控件数量，不打印消息正文或其他联系人。可能状态：`accessibility_required`、`unsupported_ui`、`controls_available`、`wechat_not_running_or_ambiguous`。

辅助功能权限需要用户在“系统设置 → 隐私与安全 → 辅助功能”中明确授予实际运行宿主/驱动。程序不自动打开权限提示或修改系统安全设置。权限已开不等于微信本身一定暴露消息控件。

## 只读观察

先手动打开文件传输助手，清空自己的未发送草稿，并保持微信前台。开始观察（不会请求后端或发送消息）：

```sh
python -m shadow_saver.mac_bridge observe --helper .mobile-data/bin/mac-wechat --state .mobile-data/mac-cursor.db
```

首个非空快照只建立基线；控件不完整返回blocked。进程重启或窗口变化重新建立基线。历史分批加载也可能看起来像追加，因此不能仅凭追加判断真实新消息。共用轮询层的自测模式要求每次运行独立测试码，旧运行的命令不处理；即便请求发送，收件人身份未验证也不能提交后端。原生驱动始终标记 `identity_verified=false`。

当前Mac CLI只有 `doctor` / `observe`，没有 `run --send` 入口；`MacDriver.send` 始终返回false，原生helper直接拒绝send请求。这是代码层禁用，不能通过更改本地配置开启。后续获得可核验会话身份及可靠新消息标识后，需要新的开发和验证才能开放收发。测试码是防止旧命令重放的辅助机制，不是微信身份凭证。

用户仅授权向自己测试，本轮没有向任何会话发送消息。日志只含固定状态/控件数量，不包含消息正文、联系人或令牌。

## Linux云端

Mac连接器通过HTTPS访问Linux后端即可，详见 [CLOUD.md](CLOUD.md)。Linux后端和Linux桌面微信驱动是两种能力；目前Linux桌面驱动未验证。
