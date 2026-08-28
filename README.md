# binance-square-sync — 把 X 的新推文自动同步到币安广场

一个单文件 Python 脚本。**零第三方依赖**(只用标准库),**只需要 1 个 X 凭据**,
**跑一次就退出**——靠系统调度器唤醒,所以电脑重启后它照样工作。

```bash
python3 square_sync.py --config config.json
```

---

## 为什么又造一个轮子

网上流传的同步脚本大同小异:`while True` 里每 5 分钟拉一次推文,发到广场。
能跑,但有三个地方会真出事。我们自己用这套 API 发了三个月广场帖,这三个都撞过。

### 坑一:第一次运行会把历史推文一次性倒进广场

X 接口默认返回最近若干条推文。脚本第一次跑的时候没有"上次同步到哪"的记录,
于是**把这一批全当成新推文发出去**。

后果不是"多发了几条"这么轻——**币安广场的帖子不可撤回、不可编辑**,
而短时间连发多条是明确的风控信号。

> 本脚本的做法:首次运行(以及 `--reset`)**只记录水位线,一条都不发**,
> 打印一行说明后退出。从下一条新推文开始才真正同步。

### 坑二:超时会导致同一条推文发两遍

币安的 `/content/add` 接口在超时的时候返回 **HTTP 504**。
关键在于:**504 不代表没发成功**——官方自己的客户端库把它当作
`success_without_post_id`(发出去了,只是没拿到帖子 id)。

所以任何"失败就重试"的写法,都会在超时时把同一条推文发第二遍。

> 本脚本的做法:调发帖接口**之前**就把这条推文落盘标记为已处理,
> 并且明确把 504 当成成功、永不重试。
> 取舍很直白:**宁可漏发一条,也不冒重复发的风险**——漏了只是少一条,重了是风控事故。

### 坑三:`while True` 扛不住重启

常驻脚本一关机就没了,而且**不会有任何提示**。等你发现的时候,可能已经好几天没同步。

> 本脚本的做法:**不常驻**。跑一轮、退出,把"定时"这件事交给操作系统自己的调度器。
> 三个平台的配置在下面,复制即用。开机自动恢复,不需要你记得做任何事。

### 顺带简化的两件

- **凭据从 4 个减到 1 个**。读自己的时间线用 OAuth 2.0 App-Only 就够了,
  只要一个 Bearer Token;不需要 Consumer Key / Secret / Access Token / Secret 四件套。
- **零依赖**。不用 `pip install` 任何东西,Python 3.7+ 直接跑。

另外还处理了几个小事:t.co 短链会还原成真实地址(否则读者在广场点开会跳回 X);
`#标签` 会摘掉井号只留文字(广场的话题标签是**选择式**的,只有平台已有的话题实体才会点亮,
自造窄词不但不亮,还可能被判定为标签滥用);积压太多时一轮最多发 3 条,不刷屏。

---

## 安装

需要 Python 3.7 以上。没有其他依赖。

```bash
git clone https://github.com/DPNDolphin/binance-square-sync.git
cd binance-square-sync
cp config.example.json config.json
```

### 第一步 · 拿币安广场 API Key

1. 打开 [binance.com/square/creator-center](https://www.binance.com/square/creator-center),
   用你的币安账号登录(需要完成 KYC)
2. 右上角点「+ 创建 API 密钥」
3. 点「查看 API」,复制 Key,填进 `config.json` 的 `square_api_key`

这个 Key **只能发内容**,不涉及资产和交易权限。但仍然不要分享给任何人,
泄露了就回创作者中心重新生成一个。

### 第二步 · 拿 X 的 Bearer Token 和你的用户 ID

1. 去 [developer.x.com](https://developer.x.com) 用你的 X 账号登录
2. **需要充值**:免费额度不包含读取时间线。Dashboard 右上角 `Buy Credits`,
   先充一点点即可(读一条推文约 $0.002 量级)。
   顺手在 Developer Console 里设一个每月消费上限,避免意外超支。
3. 进 Apps → 你的 App → **Keys & Tokens** → 复制 **Bearer Token**,
   填进 `config.json` 的 `x_bearer_token`

拿你的数字用户 ID(注意不是 @用户名):

```bash
curl -H "Authorization: Bearer 你的BearerToken" \
  "https://api.x.com/2/users/by/username/你的用户名"
```

返回里的 `"id"` 就是,填进 `x_user_id`。

### 第三步 · 先空跑一次看看

```bash
python3 square_sync.py --config config.json --dry-run
```

`--dry-run` 只打印会发什么,**不会真的发**。确认排版没问题再往下走。

然后正式跑第一次:

```bash
python3 square_sync.py --config config.json
```

第一次会打印「已把水位记到最新一条」然后退出,**这是正常的**(见上面坑一)。
之后你在 X 上发的新推文才会被同步。

---

## 设成开机自启(重点)

选你的系统,复制即用。都是**每 5 分钟跑一次,跑完退出**。

### Windows(计划任务)

用管理员身份打开 PowerShell:

```powershell
$py  = (Get-Command python).Source
$dir = "C:\path\to\square-sync"

$action  = New-ScheduledTaskAction -Execute $py `
           -Argument "square_sync.py --config config.json" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$repeat  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
           -RepetitionInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "SquareSync" -Action $action `
  -Trigger @($trigger, $repeat) -Description "同步 X 新推文到币安广场"
```

检查有没有跑起来:`Get-ScheduledTaskInfo -TaskName "SquareSync"`,
看 `LastTaskResult` 是不是 `0`。

> 注意:任务动作**不要**写成 `cmd.exe`,Windows 会直接拒绝注册(报 0x80004005)。
> 要么像上面那样直接指向 python.exe,要么用 `powershell.exe -File`。

### macOS(launchd)

存成 `~/Library/LaunchAgents/com.you.squaresync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.squaresync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/square-sync/square_sync.py</string>
    <string>--config</string>
    <string>/path/to/square-sync/config.json</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/square-sync</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/squaresync.log</string>
  <key>StandardErrorPath</key><string>/tmp/squaresync.err</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.you.squaresync.plist
launchctl kickstart -k gui/$(id -u)/com.you.squaresync   # 立刻跑一次验证
```

> macOS 默认会在闲置时睡眠,睡着的时候定时任务不触发。
> 如果这台机器是长期挂着的,去「系统设置 → 电池/电源」把
> **「显示器关闭时防止自动进入睡眠」**打开。

### Linux(cron)

```bash
crontab -e
```

加一行:

```
*/5 * * * * cd /path/to/square-sync && /usr/bin/python3 square_sync.py --config config.json >> sync.log 2>&1
```

> 如果日志里出现 `command not found`,在 crontab **第一行**加上 PATH:
> `PATH=/usr/local/bin:/usr/bin:/bin`
> cron 的默认 PATH 通常只有 `/usr/bin:/bin`,找不到第三方安装的解释器。

---

## 常用命令

| 想做什么 | 命令 |
|---|---|
| 看看会发什么,但不真发 | `python3 square_sync.py --config config.json --dry-run` |
| 正常同步一次 | `python3 square_sync.py --config config.json` |
| 把水位重置到现在(之后不补发历史) | `python3 square_sync.py --config config.json --reset` |
| 让某条推文不被同步 | 在 `square_sync_state.json` 的 `handled` 里加一条 `"推文id": "skip"` |

## 常见问题

**报 402 Payment Required**
X 的免费额度不包含读取时间线,需要在 developer.x.com 充值。这是 X 的规则,绕不过去。

**报 401**
Bearer Token 不对或已失效。注意:如果你在 App 设置里改过权限,Token 需要重新生成。

**发帖失败,code 20022**
命中了币安广场的敏感词。脚本会跳过这条继续走,不会卡住后面的。
具体是哪个词平台不会告诉你,想发的话请改写后手动发。

**改了 X 的资料/权限之后同步停了**
重新生成 Bearer Token 填回配置即可。

**我想反过来:先写好内容再同时发两边**
那这个脚本不合适 —— 它只做「X 有新推 → 同步过去」这一个方向。
反向的做法是在你自己的发布流程里同时调两个接口,两边各用各的过滤规则。

## 边界

- 只同步**纯文本**。图片和视频不会带过去(带图需要走广场的图片上传+轮询流程,
  另外币安对媒体有自己的审核,行为不好预测)。推文里的图片链接会被删掉,
  避免广场上出现一个点不开的链接。
- 只同步**原创推文**。回复和转推默认跳过(可在配置里改)。
- 一轮最多发 3 条。积压更多的话会分几轮发完。
- 不做定时发布、不做内容改写、不做数据统计。这个脚本只干一件事。

## 关于我们

这个脚本是 [CoinRebate](https://coinrebate.vip) 在自己跑广场内容管道的过程中,
把踩过的坑抽出来做的。我们做的是加密货币交易所的**手续费与返佣数据**——
帮人算清楚在哪家交易所、什么档位、实际要付多少手续费。

脚本本身跟返佣没有任何关系,不含推广代码,不回传任何数据,
你的两个 Key 都只存在你自己的机器上。

MIT License. 随便用、随便改。

发现问题欢迎提 issue。
