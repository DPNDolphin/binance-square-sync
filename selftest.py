#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py — 验证 square_sync.py 的三条核心承诺是真的。

不需要网络、不需要任何 API Key。所有 HTTP 调用都被替换成假的。

跑法:python3 selftest.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import square_sync as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  -- " + detail) if detail and not cond else ""))


def fake_tweets(n=4, start=100):
    """X 接口是倒序返回(最新在前),这里照它的样子造。"""
    return [
        {"id": str(start + i),
         "text": "这是编号 {} 的测试推文正文,长度足够通过最短内容检查。".format(start + i),
         "entities": {}}
        for i in range(n - 1, -1, -1)
    ]


class Harness:
    """替换掉 square_sync 的网络层,记录所有发帖调用。"""

    def __init__(self, tweets, square_status=200, square_code="000000"):
        self.tweets = tweets
        self.square_status = square_status
        self.square_code = square_code
        self.posted = []          # 记录每一次真的调用了发帖接口
        self.state_at_post = []   # 记录每次发帖那一刻,磁盘上的状态

    def install(self, state_path):
        self._orig = S.http_json
        self._state_path = state_path

        def stub(url, headers=None, body=None, method="GET", timeout=30):
            if "api.x.com" in url:
                return 200, {"data": self.tweets}
            # 广场发帖
            self.posted.append(body)
            # 关键:在"发帖正在发生"的这一刻,把磁盘上的状态快照下来
            with open(self._state_path, "r", encoding="utf-8") as f:
                self.state_at_post.append(json.load(f))
            if self.square_status == 504:
                return 504, {}
            return self.square_status, {
                "code": self.square_code,
                "message": "mocked",
                "data": {"id": "post_1", "shareLink": "https://example.test/1"},
            }

        S.http_json = stub

    def restore(self):
        S.http_json = self._orig


def run(tmp, tweets, argv_extra=None, **hkw):
    """跑一次 main(),返回 (harness, 最终状态)。"""
    cfg_path = os.path.join(tmp, "config.json")
    state_path = os.path.join(tmp, "state.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "x_bearer_token": "fake", "x_user_id": "1", "square_api_key": "fake",
            "state_file": state_path,
        }, f)

    h = Harness(tweets, **hkw)
    h.install(state_path)
    old_argv = sys.argv
    sys.argv = ["square_sync.py", "--config", cfg_path] + (argv_extra or [])
    try:
        S.main()
    finally:
        sys.argv = old_argv
        h.restore()

    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else None
    return h, state


def main():
    print("\n=== square-sync 自测 ===\n")

    # ---------------------------------------------------------------
    print("承诺一:首次运行绝不补发历史")
    tmp = tempfile.mkdtemp()
    try:
        h, state = run(tmp, fake_tweets(4))
        check("首次运行一条都没发", len(h.posted) == 0,
              "实际发了 {} 条".format(len(h.posted)))
        check("水位记到了最新一条(103)", state and state.get("last_tweet_id") == "103",
              "实际 {}".format(state and state.get("last_tweet_id")))
        check("handled 是空的(没把历史标成已处理)", state and not state.get("handled"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n承诺一之二:第二次运行才真的发,且只发新的")
    tmp = tempfile.mkdtemp()
    try:
        run(tmp, fake_tweets(4))                          # 第一次:只记水位
        h2, state2 = run(tmp, fake_tweets(2, start=104))  # 第二次:两条新推
        check("第二次发出了 2 条", len(h2.posted) == 2,
              "实际 {} 条".format(len(h2.posted)))
        check("发的是新推文不是历史",
              all("10" in (p.get("bodyTextOnly") or "") for p in h2.posted)
              and any("104" in (p.get("bodyTextOnly") or "") for p in h2.posted))
        check("水位推进到 105", state2.get("last_tweet_id") == "105")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n承诺二:发帖之前状态就已经落盘(崩溃不会重发)")
    tmp = tempfile.mkdtemp()
    try:
        run(tmp, fake_tweets(1, start=200))
        h3, _ = run(tmp, fake_tweets(1, start=201))
        check("发帖时磁盘上已有该条记录", len(h3.state_at_post) == 1
              and "201" in h3.state_at_post[0].get("handled", {}),
              "发帖那一刻的 handled = {}".format(
                  h3.state_at_post[0].get("handled") if h3.state_at_post else "无"))
        check("而且标记为 in_flight",
              h3.state_at_post and h3.state_at_post[0]["handled"].get("201") == "in_flight")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n承诺二之二:中途崩溃后重跑,不会把同一条发第二遍")
    tmp = tempfile.mkdtemp()
    try:
        run(tmp, fake_tweets(1, start=300))
        state_path = os.path.join(tmp, "state.json")
        # 手动伪造"上次发到一半就断电"的现场
        st = json.load(open(state_path, encoding="utf-8"))
        st["handled"]["301"] = "in_flight"
        st["last_tweet_id"] = "301"
        json.dump(st, open(state_path, "w", encoding="utf-8"))

        h4, _ = run(tmp, fake_tweets(1, start=301))
        check("崩溃现场重跑,该条没有被再发一次", len(h4.posted) == 0,
              "又发了 {} 条 —— 这就是重复发帖".format(len(h4.posted)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n承诺二之三:504 当成成功,绝不重试")
    tmp = tempfile.mkdtemp()
    try:
        run(tmp, fake_tweets(1, start=400))
        h5, state5 = run(tmp, fake_tweets(1, start=401), square_status=504)
        check("504 时只调了一次发帖接口", len(h5.posted) == 1,
              "调了 {} 次".format(len(h5.posted)))
        check("504 被记成已发布(不是失败)",
              state5["handled"].get("401") not in (None, "in_flight")
              and not str(state5["handled"].get("401")).startswith("failed"),
              "实际记成 {}".format(state5["handled"].get("401")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n承诺三:敏感词(20022)跳过而不是卡住后面的")
    tmp = tempfile.mkdtemp()
    try:
        run(tmp, fake_tweets(1, start=500))
        h6, state6 = run(tmp, fake_tweets(2, start=501), square_code="20022")
        check("两条都尝试过,没有卡在第一条", len(h6.posted) == 2,
              "只发了 {} 条".format(len(h6.posted)))
        check("失败的被标记为 failed:20022",
              state6["handled"].get("501") == "failed:20022")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n附加:一轮最多 3 条,不刷屏")
    tmp = tempfile.mkdtemp()
    try:
        run(tmp, fake_tweets(1, start=600))
        h7, _ = run(tmp, fake_tweets(8, start=601))
        check("积压 8 条时本轮只发 3 条", len(h7.posted) == S.MAX_PER_RUN,
              "发了 {} 条".format(len(h7.posted)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------
    print("\n附加:文本清洗")
    cfg = {"append_source_link": False}
    t = {"id": "1", "text": "看这个 https://t.co/abc 还有 #自造标签 结尾",
         "entities": {"urls": [{"url": "https://t.co/abc",
                                "expanded_url": "https://coinrebate.vip/x"}]}}
    out = S.tweet_to_text(t, cfg)
    check("t.co 被还原成真实地址", "coinrebate.vip/x" in out, out)
    check("井号被摘掉但词还在", "#" not in out and "自造标签" in out, out)

    t2 = {"id": "2", "text": "带图 https://t.co/pic",
          "entities": {"urls": [{"url": "https://t.co/pic",
                                 "expanded_url": "https://x.com/a/status/1/photo/1"}]}}
    check("指向推文图片的链接被删掉",
          "photo" not in S.tweet_to_text(t2, cfg) and "t.co" not in S.tweet_to_text(t2, cfg))

    # ---------------------------------------------------------------
    print("\n" + "=" * 46)
    print("通过 {} 项,失败 {} 项".format(len(PASS), len(FAIL)))
    if FAIL:
        print("失败的:")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
