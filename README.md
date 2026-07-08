# feishu-voice-claude

飞书语音 → 本机 Claude Code 执行。全程 0 费用:语音识别用本地 faster-whisper(离线),
飞书只用免费的「收发消息 + 长连接事件订阅」,**不需要公网 IP / 内网穿透**。

```
飞书 App(说话/发语音)
  → 飞书机器人(长连接推事件)
  → 本机 bridge.py:下载语音 → ffmpeg → whisper 识别
  → 解析「项目名 + 指令」→ 危险操作先确认 → claude -p 在对应项目目录执行
  → 结果回发飞书
```

## 一、飞书开放平台配置(需你本人登录操作)

1. 打开 https://open.feishu.cn → 开发者后台 → 创建「企业自建应用」。
2. 「添加应用能力」→ 开启 **机器人**。
3. 「权限管理」添加以下权限(scope):
   - `im:message`(获取与发送单聊、群组消息)
   - `im:message:send_as_bot`(以应用身份发消息)
   - `im:resource`(获取消息中的图片/文件/音频资源)—— **下载语音必需**
4. 「事件订阅」:
   - 订阅方式选 **长连接(不是 Webhook)**。
   - 添加事件:**接收消息 `im.message.receive_v1`**。
5. 「凭证与基础信息」里拿到 **App ID / App Secret**。
6. 发布版本(创建版本 → 申请发布),权限生效后,在飞书里搜到这个机器人并发起单聊。

## 二、本机配置

```bash
cd /path/to/feishu-voice-claude
cp config.example.json config.json
# 编辑 config.json:填 app_id / app_secret、projects_root、claude.bin,按需改 aliases(中文口令→项目)
```

依赖已装在 `venv/`。首次运行 whisper 会自动下载 small 模型(约 500MB,存到 ~/.cache)。

### ⚠️ 必做:配置发送者白名单(`security.allowed_open_ids`)

机器人带 `--dangerously-skip-permissions` 在你机器上执行任意指令,**必须**只允许你本人。
`security.allowed_open_ids` **为空 = 拒绝所有消息**(安全默认),配好之前机器人不会响应任何人。

抓自己的 open_id 最省事的办法:先启动 bridge,在飞书里给它随便发一条消息,机器人会回
`⛔ 未授权`,同时 `bridge.log` 里会打印这条 `拒绝未授权发送者 open_id=ou_xxxx`——把那个 `ou_...`
填进 `config.json`:

```json
"security": {
  "allowed_open_ids": ["ou_你的openid"],
  "allow_group_chat": false
}
```

重启 bridge 即生效。`allow_group_chat` 默认 `false`:只接受私聊,群聊消息一律忽略
(避免群里其他人发指令或顶替确认)。

## 三、运行

前台调试:

```bash
./venv/bin/python bridge.py
```

开机自启(launchd):先把模板 `com.example.feishu-voice-claude.plist` 里的占位符
(`__INSTALL_DIR__` / `__HOME__` / `__PROXY__`)替换成你的实际路径,另存为 `com.<你的名字>.feishu-voice-claude.plist`,然后:

```bash
cp com.<你的名字>.feishu-voice-claude.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.<你的名字>.feishu-voice-claude.plist
# 停止: launchctl unload ~/Library/LaunchAgents/com.<你的名字>.feishu-voice-claude.plist
# 日志:  tail -f bridge.log bridge.err.log
```

## 四、怎么用

在飞书里跟机器人单聊,**发语音或打字**,开头带项目名:

- 「**网页** 帮我看下登录接口为什么 401」→ 在 my-website 项目跑 Claude(网页=别名)
- 「**my-app** 跑一下单元测试并总结失败原因」

项目名识别优先级:别名表(config 的 `aliases`,推荐给英文项目配中文口令)> 目录名 > 模糊匹配。

**二次确认(默认对所有指令生效)**:机器人**不会直接执行**,而是先回一张交互卡片复述「项目 + 任务」,
带 `✅ 确认执行` / `✖️ 取消` 两个按钮:

- **点按钮**:点「确认执行」才真正跑;点「取消」放弃。
- **或打字**:回复「确认/确定/好的/ok」执行,「取消/算了」放弃。
- 直接发一条**新指令**会覆盖上一条待确认的;确认有效期 5 分钟。
- 命中 `config.json` 的 `danger.keywords`(删除/覆盖/reset --hard/push -f 等)时,卡片变红并标注 `⚠️ 疑似危险操作`。

> ⚠️ **危险标红只是「尽力提醒」,不是安全控制**。真正执行的是自然语言指令,而红标只做字面关键词匹配——
> 「挪到废纸篓」「回到三天前」这类不含关键词的破坏性指令**不会**飘红。别把标红当护栏:
> 每条指令都要自己看清楚再确认。真正的安全边界是上面的**发送者白名单 + 仅私聊**。

**连续对话(带上下文)**:每个「会话 + 项目」维度会续接 Claude 的会话记忆——首次用 `--session-id` 建会话,之后自动 `--resume`,所以可以接着聊:

- 「my-website 看下登录逻辑」→ (执行)→ 「刚那个地方为什么会 401?」→ 它记得"那个地方"
- **追问可省略项目名**:本会话上次用过哪个项目,后续不带项目名的指令就默认续用它
- 给某个项目开新会话:发「**my-website 新会话**」(带项目名)只重置该项目;不带项目名的「重置上下文 / 新会话 / 清空记忆」重置本会话上次用的项目
- 会话映射持久化在 `state/sessions.json`,bridge 重启后仍能续接

## 五、查看任务(执行了什么 / 正在执行什么)

**① 网页台账(推荐,可实时刷新)** —— 随 bridge 自启,浏览器直接打开:

```
http://127.0.0.1:8765
```

正在执行的任务秒级跳动,历史带状态/耗时/结果摘要。端口在 `config.json` 的 `dashboard.port` 改。

历史只保留最近 `dashboard.retain_days` 天(默认 2 天),bridge 启动时和每次任务结束后自动清理过期记录;重启不会清空历史(台账是持久文件)。

**② 命令行查看器**:

```bash
./venv/bin/python tasks.py            # 看一次:正在执行 + 最近历史
./venv/bin/python tasks.py --watch    # 每 2 秒刷新
./venv/bin/python tasks.py -n 30      # 历史看最近 30 条
```

**③ 实时日志**:`tail -f bridge.log` —— 每条任务打 `[task#N] ▶ 开始 …` → `[task#N] ✔ done 12.3s`。

**④ 原始文件**:`tasks.jsonl`(历史,每行一条)、`state/running.json`(正在执行快照)。均已在 `.gitignore` 中排除。

**长任务有心跳**:`claude -p` 可能跑几分钟到 30 分钟,执行期间机器人每 `claude.heartbeat_seconds`(默认 60 秒)回一句「⏳ 还在跑…已 Xs」,跑完再回结果;结果超 3500 字会截断并提示去台账/日志看全文。设 `heartbeat_seconds` 为 0 关闭心跳。

**防重复执行**:飞书事件为「至少一次」投递,断连重投或手抖会重复触发。同一条消息按 `message_id` 幂等去重;确认卡片带一次性 `token`,重复点击「确认执行」只会执行一次。

## 注意

- 本机必须一直开着且 bridge 在跑,它是链路常驻端。
- **发送者白名单是唯一的安全边界**:`--dangerously-skip-permissions` 让机器人能在你机器上执行任意操作,
  所以务必配好 `security.allowed_open_ids`(见上「本机配置」),只放行你本人;白名单为空时机器人拒绝所有消息。
- `claude -p` 默认带 `--dangerously-skip-permissions`(见 config),这样才能无人值守执行操作;
  二次确认 + 关键词标红只是**尽力提醒**,不能替代白名单。觉得不放心可去掉该参数,但那样很多操作会被跳过。
- 若你的 claude 需要走代理,在 `config.json` 的 `claude.proxy` 填代理地址(默认空 = 不走代理);`claude.bin` 填你本机 claude 可执行文件的绝对路径。
