# 接到 Luna AI（AI-Vtuber）当数字人

> 一句话：本项目负责**把观众的话搬进来**，Luna 负责**念出来 + 回答 + 动嘴型**。
> 中间那条线是 Luna 自己在 `127.0.0.1:5001` 起的「直播弹幕监听助手」，
> 所以 **Luna 那边一行代码都不用改**（用 `luna_bridge.py` 转发）。

```
 视频号 ──CDP:9223──┐
 抖音  ──:1089──────┤   统一事件                Luna 的
 B站   ──官方WS─────┼─▶ {platform,type,data} ─▶ 「直播弹幕监听助手」
 YouTube ─InnerTube─┘    luna_bridge.py        ws://127.0.0.1:5001
                                                      │
                                            LLM 回复 + TTS + 数字人
```

## 1. 和原来的「数字人桥」有什么区别

| | 原路（`tts.output = "dh"`） | 本桥（`luna_bridge.py`） |
|---|---|---|
| 对端 | `cyber-xiaoya-live` 的 HTTP `:8795/api/danmaku` | Luna 的 websocket `:5001` |
| 谁念 | 对方那套（含它自己的 LLM） | Luna 的 LLM + TTS |
| 谁决定念哪条 | 本项目的 `read.*` 开关 | Luna 的配置（`read_comment` / `thanks.*` / `filter.*`） |
| 本项目要不要开 TTS | 要（`reader.py`） | **不要**（`luna_bridge.py` 不合成语音） |

⚠️ **`reader.py` 和 `luna_bridge.py` 不要同时开**：同一句弹幕会被念两遍（一个 SAPI/语音桥，一个 Luna）。

## 2. 三步跑起来

### 第 1 步：让 Luna 收弹幕

1. 双击 Luna 的 `Scripts\1.双击我启动程序.bat`（或 `python -m webui_server`）。
2. WebUI → 通用配置 → 功能管理 → **平台** → 选 **`直播弹幕监听助手`**；
   同页「平台相关 → 直播弹幕监听助手」的地址/端口保持 `127.0.0.1` / `5001`。
3. 启动主程序。Luna 日志出现
   `直播弹幕监听助手 WebSocket 服务器已在 127.0.0.1:5001 启动` 就对了。

> 这条入口本来是为「游猴」那类助手留的，协议是扁平的几条 JSON，谁都能推。

### 第 2 步：让视频号有「调试浏览器」

视频号没有公开弹幕接口，本项目读的是**视频号助手网页**的评论面板：
浏览器必须开调试端口 + 已登录。

- 双击 `启动调试浏览器.cmd`（自动用 `browser-profile` 目录、端口 9223），
  在弹出的窗口里登录视频号、进入「直播中控台」的直播间；
- 或者让桥接脚本代劳：`luna_bridge.py --only wechat --open-browser`。

最小化/被遮挡都不影响采集（读的是 DOM，不是屏幕）。

### 第 3 步：起桥接

```powershell
py -3 luna_bridge.py --only wechat          # 只跑视频号（推荐先这样验证）
py -3 luna_bridge.py                        # 按 config.json 里启用的平台全上
```

或双击 **`启动-Luna数字人.cmd`**（等于跑上面第一条）。

成功的表现：控制台出现
`[Luna] 已连上 ws://127.0.0.1:5001，弹幕开始转发`，
此后每条弹幕一行 `[视频号] 💬 昵称: 内容`，Luna 日志里出现
`[📧直播间弹幕消息] [昵称]：内容`。

## 3. 先验链路，再开播

```powershell
py -3 luna_bridge.py --test          # 只发一条测试弹幕，验证 5001 通不通
py -3 luna_bridge.py --print-only    # 起采集但只打印报文，不发给 Luna
```

`--test` 报「连不上」的三种原因，按顺序查：

1. Luna 主程序没起（只是开了 WebUI 页面不算）；
2. Luna 的平台没选成 `直播弹幕监听助手`（选了 B站/抖音就不会起 5001）；
3. 端口被改过 → 把 `config.json → luna.ws_url` 改成 Luna 里的实际端口。

## 4. 配置对照

### 本项目（`config.json → luna`）

| 字段 | 默认 | 作用 |
|---|---|---|
| `ws_url` | `ws://127.0.0.1:5001` | Luna 的助手地址（要跟 Luna 里的一致） |
| `forward.comments/gifts/enters/follows/subscribes/superchats` | 全 `true` | 哪类事件往 Luna 推 |
| `strip_emoji` | `true` | 去掉表情和链接，念得更快、少踩审核 |
| `queue_size` | `200` | Luna 断线期间最多攒多少条（满了丢最旧） |
| `retry_sec` | `3` | 断线重连间隔 |

### Luna 侧（决定「念什么、怎么念」）

| 想改的事 | Luna 配置位置 |
|---|---|
| 要不要念观众 ID | `read_comment.read_username_enable` / `username_max_len` |
| 弹幕要不要念出来还是只让 LLM 回 | `read_comment.enable` |
| 回答风格 | `chat_type` + 对应模型 + `before_prompt` / `after_prompt` |
| 进场 / 关注 / 礼物感谢文案 | `thanks.entrance_copy` / `follow_copy` / `gift_copy` |
| 礼物低于多少钱不感谢 | `thanks.lowest_price`（`0` = 都感谢） |
| 弹幕频率/去重/长度 | `filter.comment_forget_duration`（攒批窗口，默认 1 秒）/ `comment_forget_reserve_num`（窗口内只留几条，多了丢旧的） |
| 弹幕进 LLM 的模板 | `comment_template` |

## 5. 事件怎么映射的

| 本项目事件 | 发给 Luna 的报文 |
|---|---|
| `Chat` | `{"type":"comment","username":..,"content":..}` |
| `EnterRoom` | `{"type":"entrance","username":..}` |
| `Follow` | `{"type":"follow","username":..}` |
| `Gift` | `{"type":"gift","username":..,"gift":"小心心","num":3,"unit_price":0.1,"total_price":0.3}` |
| `Subscribe`（舰长/会员） | 同上，按 `gift` 走（价格 0，够不到 `lowest_price` 就不感谢，积分/按键映射照常） |
| `SuperChat` | 按 `comment` 发（内容不丢） |
| `Like` / `EndLive` | 丢弃（Luna 没有点赞流水线） |

抖音的连击礼物（`repeatEnd`）会在本地按 `groupId` 合并 1.5 秒，只发**最终数量**，
否则 Luna 会把一次连击感谢十几遍。

## 6. 视频号这条路的真实能力

能拿到：**观众评论（昵称 + 内容）**。

拿不到：礼物、进场、关注、点赞 —— 中控台那块面板只有评论 DOM，
`wechat_client.py` 也只解析 `.live-message-item`。想让视频号也感谢礼物，
只能等视频号助手页面给出对应节点，或换平台。

已知限制：

- **需要登录态**：`browser-profile` 里的登录态，别删这个目录；页面掉登录要重新扫码。
- **必须停在直播间**：找不到评论列表时脚本会自动把页面导航到
  `channels.weixin.qq.com/platform/live/liveBuild`，所以你手动乱跳也可以。
- **首次连接只建基线**：打开页面时已有的历史评论不会补念。
- **1.2 秒轮询**：`config.json → wechat.interval_ms`，太短会空转，太长弹幕会延迟。

## 7. 排错

| 现象 | 原因 |
|---|---|
| 桥接打印「连不上 5001」 | Luna 没起 / 平台没选 `直播弹幕监听助手` / 端口不一致 |
| 桥接说「调试端口 9223 没开」 | 没双击 `启动调试浏览器.cmd`，或加 `--open-browser` |
| 采到弹幕但 Luna 没反应 | Luna 的 `filter.*` 把它当重复丢了，或 `read_comment.enable=false` 且 LLM 没配好 |
| 一句话被念两遍 | `reader.py` 和 `luna_bridge.py` 同时开着 |
| Luna 日志报 `数据解析错误` | 有别的程序在往 5001 推非 JSON —— 关掉那个程序 |

日志：`logs/luna_bridge.log`（桥接）、Luna 的 `log/log-YYYY-MM-DD.txt`。