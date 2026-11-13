#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Webコミック管理 チェッカー（webcomics.jp 専用）

リポジトリ webcomic のルートで GitHub Actions から実行される。

処理の流れ:
  1. library.json を読み、アプリから追加された作品URL（added）を取り込む
  2. 公開ブックマークを ?p=1 から全ページ取得し、登録作品の一覧を得る
  3. 「新しい作品／更新が新しい作品／前回エラー／履歴が空」だけ詳細ページを開き、
     「最近の更新」から更新履歴（最大30件）を取得する。それ以外は前回結果を再利用
  4. サムネイルが thumbs/ に無ければダウンロードして保存する
  5. latest.json を書き出す（このファイルと thumbs/ の書き手は checker だけ）

Python 標準ライブラリのみで動作する。
パーサは webcomics.jp の生HTMLのタグ構造に依存しすぎないよう、
文書順の要素列（画像・リンク・テキスト）を走査する寛容な実装にしている。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

JST = timezone(timedelta(hours=9))

BOOKMARK_URL = os.environ.get(
    "BOOKMARK_URL", "https://webcomics.jp/bookmark/3682scqd"
)
USER_AGENT = (
    "webcomic-manager-checker/1.0 "
    "(personal use; +https://github.com/mbrbjjkb-jpg/webcomic)"
)
REQUEST_INTERVAL = 0.3  # 秒
TIMEOUT = 30
MAX_BOOKMARK_PAGES = 20
MAX_HISTORY = 30

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LIBRARY_PATH = os.path.join(DATA_DIR, "library.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")

# 作品詳細ページ: https://webcomics.jp/<サイト略称>/<数字ID>
DETAIL_URL_RE = re.compile(r"^https://webcomics\.jp/([A-Za-z0-9_-]+)/\d+/?$")
# 詳細ページではないことが分かっている第1セグメント
# （ブックマーク行にはコメントへのリンク comment/<数字> も並ぶため除外が必須）
NON_DETAIL_SEGMENTS = {
    "comment", "bookmark", "mylist", "img", "static", "css", "js",
    "search", "tag", "tags", "author", "about", "help", "login",
    "user", "users", "site", "sites", "ranking", "new", "info",
}
# 相対・絶対の日時ラベル
TIME_TEXT_RE = re.compile(
    r"(\d+\s*分前|\d+\s*時間前|\d+\s*日前|\d+\s*週間前|\d+\s*か月前|"
    r"\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日)"
)
MONTH_DAY_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
# 出版社ドメインらしい文字列（例: tonarinoyj.jp）
DOMAIN_TEXT_RE = re.compile(
    r"^([A-Za-z0-9-]+\.)+(jp|com|net|org|io|co\.jp)(/.*)?$"
)


# ---------------------------------------------------------------- HTTP

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def fetch_text(url):
    return fetch_bytes(url).decode("utf-8", errors="replace")


def polite_sleep():
    time.sleep(REQUEST_INTERVAL)


# ---------------------------------------------------------------- HTML 走査

class Tokenizer(HTMLParser):
    """HTMLを文書順のトークン列に変換する。

    トークン:
      ("img", src)
      ("a", {"href":..., "text":..., "title":..., "img": <内包するimgのsrc or None>})
      ("text", 文字列)   … リンク外のテキスト
      ("heading", 文字列)… h1〜h4 の見出しテキスト
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self._a_stack = []       # 現在開いている <a> の情報
        self._heading_depth = 0  # 見出しタグの中にいる深さ
        self._heading_buf = []
        self.page_title = ""

    # -- 内部ヘルパ
    def _emit_text(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if self._heading_depth:
            self._heading_buf.append(text)
            return
        if self._a_stack:
            self._a_stack[-1]["text_parts"].append(text)
            return
        self.tokens.append(("text", text))

    # -- HTMLParser フック
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._a_stack.append(
                {
                    "href": attrs.get("href", "") or "",
                    "title": attrs.get("title", "") or "",
                    "text_parts": [],
                    "img": None,
                }
            )
        elif tag == "img":
            src = attrs.get("src", "") or ""
            if src:
                if self._a_stack and self._a_stack[-1]["img"] is None:
                    self._a_stack[-1]["img"] = src
                self.tokens.append(("img", src))
        elif tag in ("h1", "h2", "h3", "h4"):
            self._heading_depth += 1
            self._heading_buf = []

    def handle_endtag(self, tag):
        if tag == "a" and self._a_stack:
            a = self._a_stack.pop()
            self.tokens.append(
                (
                    "a",
                    {
                        "href": a["href"],
                        "text": " ".join(a["text_parts"]).strip(),
                        "title": a["title"].strip(),
                        "img": a["img"],
                    },
                )
            )
        elif tag in ("h1", "h2", "h3", "h4") and self._heading_depth:
            self._heading_depth -= 1
            if self._heading_depth == 0:
                text = " ".join(self._heading_buf).strip()
                if text:
                    self.tokens.append(("heading", text))
                self._heading_buf = []

    def handle_data(self, data):
        self._emit_text(data)

    def handle_startendtag(self, tag, attrs):
        # <img ... /> のような自己完結タグ
        if tag == "img":
            self.handle_starttag(tag, attrs)


def tokenize(html):
    tk = Tokenizer()
    tk.feed(html)
    tk.close()
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if title_m:
        tk.page_title = " ".join(title_m.group(1).split())
    return tk.tokens, tk.page_title


def normalize_detail_url(url):
    return url.rstrip("/")


def is_detail_url(url):
    m = DETAIL_URL_RE.match(url or "")
    return bool(m) and m.group(1) not in NON_DETAIL_SEGMENTS


def find_time_text(text):
    m = TIME_TEXT_RE.search(text or "")
    return m.group(1).replace(" ", "") if m else None


# ---------------------------------------------------------------- ブックマーク解析

def parse_bookmark_page(html):
    """ブックマーク1ページ分から作品リストを取り出す。

    詳細ページリンクを基点（=行の締め）とし、それまでに現れた
    サムネイル・タイトルリンク・ドメイン文字列・時刻文字列をひも付ける。
    詳細リンクの直後に時刻やドメインが来る並びにも対応するため、
    締めた直後のレコードへも不足分を補完する。
    """
    tokens, _ = tokenize(html)
    works = []
    by_url = {}

    cur = {"title": None, "site": None, "thumbUrl": None, "whenText": None}
    last = None  # 直前に締めたレコード（後続テキストでの補完用）

    def attach_text(text):
        """時刻・ドメイン文字列を、現在の行 or 直前のレコードへ割り当てる。"""
        nonlocal last
        t = find_time_text(text)
        if t:
            if cur["whenText"] is None and (
                cur["title"] is not None or cur["thumbUrl"] is not None
            ):
                cur["whenText"] = t
            elif last is not None and last["whenText"] is None:
                last["whenText"] = t
            return
        if DOMAIN_TEXT_RE.match(text or ""):
            if cur["site"] is None and cur["title"] is not None:
                cur["site"] = text
            elif last is not None and last["site"] is None:
                last["site"] = text

    for kind, val in tokens:
        if kind == "img":
            if "/img/thumb/" in val:
                cur["thumbUrl"] = val
                last = None
        elif kind == "text":
            attach_text(val)
        elif kind == "a":
            href = val["href"] or ""
            if is_detail_url(href):
                if val["img"]:
                    # サムネイル画像を包む詳細リンクは行の締めにしない
                    if "/img/thumb/" in val["img"]:
                        cur["thumbUrl"] = val["img"]
                    continue
                if cur["title"] is None and not val["text"]:
                    continue  # 中身のない詳細リンクは締めにしない
                rec = {
                    "detailUrl": normalize_detail_url(href),
                    "title": cur["title"] or val["text"] or "",
                    "site": cur["site"],
                    "thumbUrl": cur["thumbUrl"],
                    "whenText": cur["whenText"],
                }
                if not rec["site"] and cur.get("titleHref"):
                    rec["site"] = _netloc(cur["titleHref"])
                if rec["detailUrl"] in by_url:
                    # 同じ作品の重複行は、先に取れた情報で不足を補うだけにする
                    old = by_url[rec["detailUrl"]]
                    for k in ("title", "site", "thumbUrl", "whenText"):
                        if not old.get(k) and rec.get(k):
                            old[k] = rec[k]
                    last = old
                else:
                    works.append(rec)
                    by_url[rec["detailUrl"]] = rec
                    last = rec
                cur = {"title": None, "site": None, "thumbUrl": None,
                       "whenText": None}
            elif href.startswith("http") and "webcomics.jp" not in href:
                # 出版社サイトへのリンク = タイトルリンク候補
                if cur["title"] is None and val["text"]:
                    cur["title"] = val["text"]
                    cur["titleHref"] = href
                    last = None
                elif val["text"]:
                    attach_text(val["text"])
            else:
                # webcomics.jp 内部リンク（コメント等）のテキストは
                # 時刻・ドメインだけ拾う
                if val["text"]:
                    attach_text(val["text"])
    return works


def _netloc(url):
    m = re.match(r"^https?://([^/]+)", url or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 詳細ページ解析

def parse_detail_page(html):
    """詳細ページから「最近の更新」の履歴（新しい順・最大30件）とページタイトルを返す。

    履歴の各要素: {"date": 日付表示, "label": リンク表示文字列,
                  "title": title属性 or label, "url": 話ページURL}
    """
    tokens, page_title = tokenize(html)
    history = []
    in_section = False
    last_date = None

    for kind, val in tokens:
        if kind == "heading":
            if "最近の更新" in val:
                in_section = True
                continue
            if in_section:
                break  # 次の見出しに来たら「最近の更新」は終わり
            continue
        if not in_section:
            continue
        if kind == "text":
            t = find_time_text(val)
            if t:
                last_date = t
        elif kind == "a":
            href = val["href"] or ""
            if not href.startswith("http") or "webcomics.jp" in href:
                continue  # 「全表示」などの内部リンクは話ではない
            label = val["text"] or ""
            title = val["title"] or label
            date = last_date or label
            history.append(
                {"date": date, "label": label, "title": title, "url": href}
            )
            last_date = None
            if len(history) >= MAX_HISTORY:
                break
    return history, page_title


# ---------------------------------------------------------------- 判定ヘルパ

def is_fresh(when_text, today):
    """ブックマークの相対時刻が「新しい」かどうか。

    新しい = 「◯分前」「◯時間前」「1日前」を含む、または
    月日表示が実行日（JST）の当日・前日。判定できなければ安全側で True。
    """
    if not when_text:
        return True
    if "分前" in when_text or "時間前" in when_text:
        return True
    if when_text.strip() == "1日前":
        return True
    m = MONTH_DAY_RE.search(when_text)
    if m:
        md = (int(m.group(1)), int(m.group(2)))
        yesterday = today - timedelta(days=1)
        return md in {(today.month, today.day), (yesterday.month, yesterday.day)}
    return False


def needs_detail(work, prev, today):
    """詳細ページを取得すべき作品かどうか。"""
    if prev is None:
        return True
    if prev.get("error"):
        return True
    if not prev.get("history"):
        return True
    if work.get("added"):
        return True
    return is_fresh(work.get("whenText"), today)


def thumb_filename(thumb_url):
    """サムネイルURLからファイル名（例: 193078.jpg）を取り出す。"""
    if not thumb_url:
        return None
    name = thumb_url.rstrip("/").rsplit("/", 1)[-1]
    return name if name.lower().endswith((".jpg", ".jpeg", ".png")) else None


# ---------------------------------------------------------------- メイン処理

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    now = datetime.now(JST)
    today = now.date()
    print(f"[checker] start {now.isoformat()} bookmark={BOOKMARK_URL}")

    library = read_json(LIBRARY_PATH, {"version": 1, "works": {}, "added": []})
    added_urls = []
    for u in library.get("added", []) or []:
        if is_detail_url(u or ""):
            nu = normalize_detail_url(u)
            if nu not in added_urls:
                added_urls.append(nu)

    prev_latest = read_json(LATEST_PATH, {"checkedAt": None, "works": []})
    prev_by_url = {}
    for w in prev_latest.get("works", []) or []:
        if w.get("detailUrl"):
            prev_by_url[w["detailUrl"]] = w

    # --- 第1段階: ブックマーク全ページ
    works = []
    seen = set()
    sep = "&" if "?" in BOOKMARK_URL else "?"
    for page in range(1, MAX_BOOKMARK_PAGES + 1):
        url = f"{BOOKMARK_URL}{sep}p={page}"
        try:
            html = fetch_text(url)
        except Exception as e:  # ページ取得に失敗したら、そこまでの分で続行
            print(f"[checker] bookmark page {page} failed: {e}", file=sys.stderr)
            break
        polite_sleep()
        page_works = parse_bookmark_page(html)
        print(f"[checker] bookmark page {page}: {len(page_works)} works")
        for w in page_works:
            if w["detailUrl"] not in seen:
                seen.add(w["detailUrl"])
                works.append(w)
        if len(page_works) < 50:
            break

    # --- アプリから追加された作品（ブックマークに無いもの）
    for u in added_urls:
        if u not in seen:
            seen.add(u)
            works.append(
                {"detailUrl": u, "title": "", "site": None,
                 "thumbUrl": None, "whenText": None, "added": True}
            )
        else:
            for w in works:
                if w["detailUrl"] == u:
                    w["added"] = True

    print(f"[checker] total works: {len(works)} (added: {len(added_urls)})")

    # --- 第2段階: 必要な作品だけ詳細ページ＆サムネイル
    os.makedirs(THUMBS_DIR, exist_ok=True)
    out_works = []
    detail_count = 0
    thumb_count = 0

    for w in works:
        prev = prev_by_url.get(w["detailUrl"])
        history = (prev or {}).get("history") or []
        error = None
        title = w.get("title") or (prev or {}).get("title") or ""
        site = w.get("site") or (prev or {}).get("site") or ""
        thumb_url = w.get("thumbUrl")

        if needs_detail(w, prev, today):
            detail_count += 1
            try:
                html = fetch_text(w["detailUrl"])
                polite_sleep()
                new_history, page_title = parse_detail_page(html)
                if new_history:
                    history = new_history
                elif not history:
                    error = "更新履歴を取得できませんでした"
                if not title and page_title:
                    # ページタイトルからサイト名らしい末尾を取り除く
                    title = re.split(r"\s*[|｜]\s*", page_title)[0].strip()
            except Exception as e:
                error = f"詳細ページ取得失敗: {e}"
                polite_sleep()

        # サムネイル（無ければダウンロード）
        thumb_file = None
        fname = thumb_filename(thumb_url)
        if fname:
            dest = os.path.join(THUMBS_DIR, fname)
            if not os.path.exists(dest):
                try:
                    data = fetch_bytes(thumb_url)
                    polite_sleep()
                    if data:
                        with open(dest, "wb") as f:
                            f.write(data)
                        thumb_count += 1
                except Exception as e:
                    if error is None:
                        error = f"サムネイル取得失敗: {e}"
                    polite_sleep()
            if os.path.exists(dest):
                thumb_file = f"data/thumbs/{fname}"
        elif prev and prev.get("thumbFile"):
            thumb_file = prev["thumbFile"]

        out_works.append(
            {
                "detailUrl": w["detailUrl"],
                "title": title,
                "site": site,
                "thumbFile": thumb_file,
                "history": history,
                "latest": history[0] if history else None,
                "error": error,
            }
        )

    result = {
        "checkedAt": now.isoformat(timespec="seconds"),
        "bookmarkUrl": BOOKMARK_URL,
        "works": out_works,
    }
    tmp_path = LATEST_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, LATEST_PATH)

    errors = sum(1 for w in out_works if w["error"])
    print(
        f"[checker] done: works={len(out_works)} detailFetched={detail_count} "
        f"thumbsDownloaded={thumb_count} errors={errors}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
