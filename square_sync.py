#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
square_sync.py — 把 X(推特)的新推文同步到币安广场。

与网上流传的教程版本相比,这个脚本解决三件会真出事的事:

  1. 首次运行绝不补发历史。教程版第一次跑会把 API 返回的最近 N 条推文
     一次性全发到广场 —— 广场发帖不可撤回、不可编辑,瞬间刷屏就是风控事故。
     本脚本首次运行只记录水位线然后退出,第二次起才真正同步。

  2. 绝不重复发同一条。币安 /content/add 在超时的情况下会返回 504,
     而官方自己的库把它当成 "success_without_post_id" —— 也就是说
     帖子很可能已经发出去了,只是没拿到 id。所以本脚本在调用发帖接口
     **之前**就把该条推文标记为已处理:宁可漏一条,也不赌它没发出去。

  3. 不靠常驻进程扛重启。教程版是 while True + sleep(300),电脑一重启就停,
     而且不会有任何提示。本脚本跑一次就退出,交给系统调度器
     (Windows 计划任务 / macOS launchd / Linux systemd timer 或 cron)定时唤醒。
     README 里三个平台的配置都写好了,复制即用。

另外只需要 1 个 X 凭据(Bearer Token)而不是 4 个,且零第三方依赖 —— 只用 Python 标准库。

用法:
    python3 square_sync.py --config config.json          # 正常同步
    python3 square_sync.py --config config.json --dry-run # 只看会发什么,不真发
    python3 square_sync.py --config config.json --reset   # 重置水位到当前(下次不补发历史)

作者:CoinRebate(https://coinrebate.vip)· MIT License
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

X_API = "https://api.x.com/2"
SQUARE_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"

# 广场短帖正文上限(官方文档口径 2100;这里留 100 字余量给尾巴)
SQUARE_TEXT_LIMIT = 2000

# 每次最多同步几条。防止你几小时没跑、积压了一堆推文时一次性刷屏。
MAX_PER_RUN = 3


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] {}".format(stamp, msg), flush=True)


# ---------------------------------------------------------------- 配置与状态

def load_config(path):
    if not os.path.exists(path):
        sys.exit(
            "找不到配置文件:{}\n"
            "请先复制 config.example.json 为 config.json 并填好三项。".format(path)
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    missing = [k for k in ("x_bearer_token", "x_user_id", "square_api_key") if not cfg.get(k)]
    if missing:
        sys.exit("配置缺少这几项:{}".format(", ".join(missing)))

    # 允许用环境变量覆盖,方便不把密钥写进文件
    cfg["x_bearer_token"] = os.environ.get("X_BEARER_TOKEN") or cfg["x_bearer_token"]
    cfg["square_api_key"] = os.environ.get("BINANCE_SQUARE_OPENAPI_KEY") or cfg["square_api_key"]
    cfg.setdefault("state_file", "square_sync_state.json")
    cfg.setdefault("skip_replies", True)
    cfg.setdefault("skip_retweets", True)
    cfg.setdefault("append_source_link", False)
    return cfg


def load_state(path):
    if not os.path.exists(path):
        return {"last_tweet_id": None, "handled": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (ValueError, IOError):
        # 状态文件坏了就当第一次跑 —— 比拿一个半截状态去发帖安全
        log("状态文件读不出来,按首次运行处理(不会补发历史)")
        return {"last_tweet_id": None, "handled": {}}
    state.setdefault("handled", {})
    return state


def save_state(path, state):
    """先写临时文件再改名 —— 中途断电不会留下半个 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- X 侧

def http_json(url, headers=None, body=None, method="GET", timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"_raw": raw}


def fetch_new_tweets(cfg, since_id):
    """拉自己的新推文。只需要 Bearer Token(OAuth 2.0 App-Only)。"""
    params = {
        "max_results": "10",
        "tweet.fields": "created_at,text,entities,referenced_tweets",
        "exclude": ",".join(
            ([  "replies"] if cfg["skip_replies"] else [])
            + (["retweets"] if cfg["skip_retweets"] else [])
        ),
    }
    if not params["exclude"]:
        params.pop("exclude")
    if since_id:
        params["since_id"] = str(since_id)

    url = "{}/users/{}/tweets?{}".format(
        X_API, cfg["x_user_id"], urllib.parse.urlencode(params)
    )
    status, payload = http_json(url, {"Authorization": "Bearer " + cfg["x_bearer_token"]})

    if status == 401:
        sys.exit("X 认证失败(401):Bearer Token 不对或已失效。")
    if status == 402:
        sys.exit(
            "X 提示需要付费(402):读取时间线不在免费额度内,\n"
            "去 developer.x.com 的 Dashboard 点 Buy Credits 充一点即可(读一条约 $0.002)。"
        )
    if status == 429:
        log("X 限流(429),这一轮跳过,下次调度再试。")
        return []
    if status != 200:
        log("X 返回异常 {}:{}".format(status, json.dumps(payload)[:300]))
        return []

    return payload.get("data") or []


def tweet_to_text(tweet, cfg):
    """把推文正文整理成适合发广场的纯文本。"""
    text = tweet.get("text", "")

    # 把 t.co 短链换回真实地址 —— 广场上的 t.co 链接读者点开是 X,体验很差
    for u in (tweet.get("entities") or {}).get("urls", []):
        expanded = u.get("expanded_url") or ""
        # 指向推文自身的媒体链接直接删掉,它在广场上打不开
        if "/photo/" in expanded or "/video/" in expanded:
            text = text.replace(u.get("url", ""), "")
        elif expanded:
            text = text.replace(u.get("url", ""), expanded)

    # X 的 hashtag 在广场是"选择式"的:只有平台已有的话题实体才会点亮,
    # 自造窄词不但不亮,还可能触发标签滥用判定。所以一律摘掉井号只留词。
    text = re.sub(r"#(\S+)", r"\1", text)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if cfg.get("append_source_link"):
        text += "\n\n原文:https://x.com/i/status/{}".format(tweet["id"])

    return text[:SQUARE_TEXT_LIMIT]


# ---------------------------------------------------------------- 广场侧

def post_to_square(api_key, text, title=None):
    """
    发到广场。官方判据只有一条:有 title 就是文章(contentType 2),没有就是短帖(1)。
    返回 (ok, info)。
    """
    body = {"contentType": 2 if title else 1, "bodyTextOnly": text}
    if title:
        body["title"] = title

    status, payload = http_json(
        SQUARE_V1 + "/content/add",
        {
            "X-Square-OpenAPI-Key": api_key,
            "Content-Type": "application/json",
            "clienttype": "binanceSkill",
        },
        body=body,
        method="POST",
        timeout=60,
    )

    # 504 = 超时。官方库把它当成"发成功了但没返回 id"。
    # 绝不能因为这个去重试 —— 帖子多半已经在广场上了。
    if status == 504:
        return True, {"note": "超时,但很可能已发出(不重试)", "shareLink": None}

    code = payload.get("code")
    if code == "000000":
        return True, payload.get("data") or {}

    return False, {"code": code, "message": payload.get("message"), "http": status}


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="把 X 新推文同步到币安广场")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印会发什么,不真的发")
    ap.add_argument("--reset", action="store_true", help="把水位重置到当前,之后不补发历史")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state_path = cfg["state_file"]
    state = load_state(state_path)

    first_run = state.get("last_tweet_id") is None

    tweets = fetch_new_tweets(cfg, None if (first_run or args.reset) else state["last_tweet_id"])

    if not tweets:
        log("没有新推文。")
        return

    # X 返回的是倒序(最新在前),同步要按发布顺序来
    tweets = list(reversed(tweets))
    newest_id = tweets[-1]["id"]

    # —— 这一段是整个脚本最重要的地方 ——
    # 首次运行(或 --reset)只把水位记到"现在",一条都不发。
    # 否则你第一次跑就会把最近十条推文一次性倒进广场,而广场发帖不可撤回。
    if first_run or args.reset:
        state["last_tweet_id"] = newest_id
        save_state(state_path, state)
        log("已把水位记到最新一条(id {})。".format(newest_id))
        log("首次运行不补发历史 —— 从下一条新推文开始同步。")
        return

    todo = [t for t in tweets if t["id"] not in state["handled"]]
    if len(todo) > MAX_PER_RUN:
        log("积压 {} 条,本轮只发最早的 {} 条,其余下轮继续。".format(len(todo), MAX_PER_RUN))
        todo = todo[:MAX_PER_RUN]

    for tweet in todo:
        text = tweet_to_text(tweet, cfg)
        if len(text.strip()) < 10:
            log("跳过 {}(去掉链接和图片后基本没内容)".format(tweet["id"]))
            state["handled"][tweet["id"]] = "skipped_too_short"
            state["last_tweet_id"] = tweet["id"]
            save_state(state_path, state)
            continue

        if args.dry_run:
            log("[dry-run] 会发这条 ↓\n{}\n{}".format("-" * 50, text))
            continue

        # 关键顺序:先落盘标记"正在发",再真的发。
        # 如果发帖过程中脚本被杀 / 断网 / 断电,重启后这条会被当作已处理而跳过。
        # 理由:广场帖子不可撤回,重复发同一篇是风控风险,漏发一条只是少一条。
        state["handled"][tweet["id"]] = "in_flight"
        state["last_tweet_id"] = tweet["id"]
        save_state(state_path, state)

        ok, info = post_to_square(cfg["square_api_key"], text)

        if ok:
            state["handled"][tweet["id"]] = info.get("id") or "posted"
            log("已发布 → {}".format(info.get("shareLink") or info.get("note") or "成功"))
        else:
            state["handled"][tweet["id"]] = "failed:{}".format(info.get("code"))
            log("发布失败 {}:{}".format(info.get("code"), info.get("message")))
            # 20022 = 命中敏感词。这条永远不会成功,标记掉继续走,不能卡住后面的。
            if info.get("code") == "20022":
                log("(命中敏感词,这条不会重试。想发的话请改写后手动发。)")

        save_state(state_path, state)
        time.sleep(2)  # 别把请求打得太密

    # 只保留最近 200 条处理记录,状态文件不会无限长大
    if len(state["handled"]) > 200:
        keys = sorted(state["handled"].keys())[-200:]
        state["handled"] = {k: state["handled"][k] for k in keys}
        save_state(state_path, state)

    log("本轮结束。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
