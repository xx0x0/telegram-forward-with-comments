# telegram-forward-with-comments

A Telethon-based tool that archives Telegram channel posts **together with the comments under each post** to a destination channel and its linked discussion group. Most archiving scripts copy the posts and lose the discussion — this one keeps both.

归档 Telegram 频道帖子时,把帖子下面讨论群的评论也一起完整保留——这是大多数同类脚本缺失的部分。

> **Intended use / 使用说明**:for backing up and archiving channels you own, manage, or have permission to preserve. Respect Telegram's Terms of Service and the content owner's rights.
> 用于备份、归档你自己拥有/管理或已获授权的频道内容。请遵守 Telegram 服务条款并尊重内容所有者的权利。

## 特性

- 归档频道主帖到目标频道
- 把源频道下方讨论群里的评论同步到目标频道的讨论群（**这是大多数同类脚本都没做的部分**）
- 兼容无法直接转发的频道：自动回退到下载-上传模式（仅限对自有/已授权内容做备份）
- 媒体不落项目目录：默认走内存流（BytesIO），>50MB 才回落到系统 temp 用完即删
- FloodWait 遵从：命中服务端限流后按返回的等待秒数 sleep 再恢复，不做对抗
- 全局限速器：默认每分钟 18 条，主动保持在 Telegram 限速之内
- 评论级断点续传：转发到第 73 条崩了，下次启动从 73 条接着发，不重复

## 依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 主要用到：`telethon`、`pyyaml`、`cryptg`、`python-socks`、`opencv-python`（视频缩略图，可选）。

## 配置

1. 复制示例配置：
   ```bash
   cp config.example.yml config.yml
   ```
2. 填入你的凭证：
   - `api_id` / `api_hash`：在 https://my.telegram.org 申请
   - `bot_token`：找 [@BotFather](https://t.me/BotFather) `/newbot` 创建
3. 如果在受限网络环境，把 `proxy.enabled` 设成 `true` 并填入 socks5 / http 代理。

## 运行

```bash
python main.py
```

第一次启动会要求短信验证码登录 user 账号（用来读源频道）。session 信息存在 `session/` 目录下，加在 `.gitignore`。

## 在 Bot 里配置频道

启动后到 Telegram 里和 bot 私聊：
- `/start` — 入口
- 通过菜单：添加目标频道 → 添加源频道 → 配置自动转发间隔

数据全部存在本地 `data.db`（SQLite）。

## 项目结构

```
core/
  client.py                              Telethon 客户端封装
  database.py                            SQLite schema
  message_handlers.py                    /start /setbotcommands 等命令
  callback_handlers.py                   bot 内联按钮路由
  source_channel_callback.py             源频道管理 UI
  target_channel_callback.py             目标频道管理 UI
  forward_handler.py                     通用转发（含自定义文本）
  normal_forward_handler.py              不带评论的简单转发
  comment_forward_handler.py             带评论的转发（核心）
  restricted_normal_forward_handler.py   受限频道：常规转发的下载-上传 fallback
  restricted_comment_forward_handler.py  受限频道：评论转发的下载-上传 fallback
  auto_forward_scheduler.py              定时调度器
utils/
  flood_control.py                       RateLimiter / safe_call / stream_media
  fast_telethon.py                       快速下载/上传（来自社区，已弃用，保留兼容）
  logger.py
config/
  config.py                              YAML 配置读取
config.example.yml                       配置模板
main.py                                  入口
```

## 已知限制

- 受限频道的大文件（>50MB）会临时落到系统 temp，发完即删；不会在项目目录留文件
- 视频缩略图依赖 `opencv-python`；没装也能跑，只是 user 账号发的大视频可能没预览
- 主帖与讨论群同步消息的对应关系基于 `GetDiscussionMessageRequest` + ID 偏移，极少数情况下可能取不到，会自动回退到事件监听 15 秒

## License

MIT
