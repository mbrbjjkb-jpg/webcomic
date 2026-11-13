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
パーサは実HTML（2026-11-13 の実地サンプル）に基づく構造解析：
  - ブックマークは div.entry[data-comic-no] の行ブロック単位で切り分ける
  - 詳細ページは「最近の更新」の ul/li を走査する。話リンクを持つ li と、
    日付＋更新テキストだけの li（話URLを持たないサイト）の両方に対応し、
    日付は ISO 形式（YYYY-MM-DD）に正規化する
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
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

# 作品詳細ページ: https://webcomics.jp/<サイト略称>/<作品ID>
# 作品IDの形式はサイトごとに様々（数字のみとは限らない）：
#   数字:        seiga/27620
#   16進風:      mangacross/82594f4a166ec
#   英数字混在:  comic-walker/KC_003957_S、dre/CD00007
#   スラッグ:    kurage-bunch/yamashoku、kirapo/mashirochan、omocoro-comic/view
DETAIL_URL_RE = re.compile(r"^https://webcomics\.jp/([a-z0-9-]+)/([A-Za-z0-9_-]+)/?$")
# 詳細ページではないことが分かっている第1セグメント
# （ブックマーク行にはコメントへのリンク comment/<数字> も並ぶため除外が必須）
NON_DETAIL_SEGMENTS = {
    "comment", "bookmark", "mylist", "data", "img", "static", "css", "js",
    "search", "tag", "tags", "author", "about", "help", "login",
    "user", "users", "site", "sites", "ranking", "new", "info",
    "api", "feed", "rss", "policy", "terms", "contact", "settings",
    "history", "genre", "publisher",
}
MONTH_DAY_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
# 絶対日付（2026.11.13 / 2026/11/11 / 2026年11月11日 のいずれも拾う）
ABS_DATE_RE = re.compile(r"(\d{4})[./年](\d{1,2})[./月](\d{1,2})")
REL_MIN_RE = re.compile(r"(\d+)\s*分前")
REL_HOUR_RE = re.compile(r"(\d+)\s*時間前")
REL_DAY_RE = re.compile(r"(\d+)\s*日前")
REL_WEEK_RE = re.compile(r"(\d+)\s*週間前")


# ---------------------------------------------------------------- HTTP

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def fetch_text(url):
    return fetch_bytes(url).decode("utf-8", errors="replace")


def polite_sleep():
    time.sleep(REQUEST_INTERVAL)


# ---------------------------------------------------------------- 簡易DOM
# webcomics.jp の実HTMLは div / span の入れ子構造が情報の単位になっている
# （ブックマークの行、詳細ページの更新リストなど）ため、タグ構造を保持する
# 最小限の DOM で解析する。タグの対応が崩れた HTML でも落ちない寛容な実装。

VOID_TAGS = {"img", "br", "hr", "meta", "link", "input", "source", "wbr"}


class _El:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = []  # _El と str の混在リスト（文書順）


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        el = _El(tag, dict(attrs))
        self._stack[-1].children.append(el)
        if tag not in VOID_TAGS:
            self._stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_El(tag, dict(attrs)))

    def handle_endtag(self, tag):
        # 対応する開始タグまで巻き戻す（閉じタグの過不足に耐える）
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if data:
            self._stack[-1].children.append(data)


def parse_dom(html):
    builder = _DomBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def el_text(el):
    """要素内のテキストを空白正規化して連結した文字列を返す。"""
    parts = []

    def rec(node):
        for c in node.children:
            if isinstance(c, str):
                parts.append(c)
            else:
                rec(c)

    rec(el)
    return " ".join(" ".join(parts).split())


def has_class(el, name):
    return name in (el.attrs.get("class") or "").split()


def iter_els(el):
    """要素を文書順（深さ優先）に列挙する。"""
    yield el
    for c in el.children:
        if not isinstance(c, str):
            yield from iter_els(c)


def find_first(el, pred):
    for e in iter_els(el):
        if pred(e):
            return e
    return None


def find_all(el, pred):
    return [e for e in iter_els(el) if pred(e)]


def _is_external(href):
    """webcomics.jp 以外への http(s) リンクかどうか。"""
    return bool(href) and href.startswith("http") and "webcomics.jp" not in href


# ---------------------------------------------------------------- URL判定

def normalize_detail_url(url):
    return (url or "").strip().rstrip("/")


def is_detail_url(url):
    m = DETAIL_URL_RE.match(url or "")
    return bool(m) and m.group(1) not in NON_DETAIL_SEGMENTS


def clean_page_title(page_title):
    """詳細ページの <title> から作品タイトルを取り出す。

    末尾の「 - Web漫画アンテナ」「 | Web漫画アンテナ」等のサイト名を取り除く。
    """
    t = (page_title or "").strip()
    t = re.sub(r"\s*[-|｜–—]\s*Web漫画アンテナ.*$", "", t).strip()
    if t == "Web漫画アンテナ":
        t = ""
    return t


# ---------------------------------------------------------------- ブックマーク解析

def parse_bookmark_page(html):
    """ブックマーク1ページ分から作品リストを取り出す。

    作品行は <div class="entry" data-comic-no="..."> 単位で切り分け、
    行内の決まった位置から各項目を取る（実HTMLで確認済みの構造）：
      - detailUrl:    span.entry-detail の <a href>（ID形式では弾かない）
      - title:        div.entry-title の <a> テキスト
      - publisherUrl: div.entry-title の <a href>（最新話へのリンク）
      - site:         div.entry-site の <a> テキスト（掲載サイト名）
      - thumbUrl:     div.entry-thumb の <img src>
      - whenText:     div.entry-date のテキスト（相対時刻表示）
      - comicNo:      data-comic-no
    行の外にあるリンク（サイドバー等）は拾わない。
    """
    root = parse_dom(html)
    works = []
    seen = set()
    entries = find_all(
        root,
        lambda e: e.tag == "div" and has_class(e, "entry")
        and e.attrs.get("data-comic-no"),
    )
    for entry in entries:
        detail_box = find_first(entry, lambda e: has_class(e, "entry-detail"))
        detail_a = (
            find_first(detail_box, lambda e: e.tag == "a" and e.attrs.get("href"))
            if detail_box is not None else None
        )
        if detail_a is None:
            continue
        detail_url = normalize_detail_url(detail_a.attrs["href"])
        if not detail_url or detail_url in seen:
            continue
        seen.add(detail_url)

        title = ""
        publisher_url = None
        title_box = find_first(entry, lambda e: has_class(e, "entry-title"))
        if title_box is not None:
            a = find_first(title_box, lambda e: e.tag == "a")
            if a is not None:
                title = el_text(a)
                publisher_url = (a.attrs.get("href") or "").strip() or None

        site = None
        site_box = find_first(entry, lambda e: has_class(e, "entry-site"))
        if site_box is not None:
            a = find_first(site_box, lambda e: e.tag == "a")
            site = (el_text(a) if a is not None else el_text(site_box)) or None

        thumb_url = None
        thumb_box = find_first(entry, lambda e: has_class(e, "entry-thumb"))
        if thumb_box is not None:
            img = find_first(thumb_box, lambda e: e.tag == "img")
            if img is not None:
                thumb_url = (img.attrs.get("src") or "").strip() or None

        when_text = None
        date_box = find_first(entry, lambda e: has_class(e, "entry-date"))
        if date_box is not None:
            when_text = el_text(date_box) or None

        raw_no = entry.attrs.get("data-comic-no")
        try:
            comic_no = int(raw_no)
        except (TypeError, ValueError):
            comic_no = raw_no

        works.append(
            {
                "detailUrl": detail_url,
                "title": title,
                "site": site,
                "thumbUrl": thumb_url,
                "whenText": when_text,
                "publisherUrl": publisher_url,
                "comicNo": comic_no,
            }
        )
    return works


# ---------------------------------------------------------------- 日付の正規化

def _iso_or_none(y, m, d):
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def normalize_history_date(update_text, date_text, fallback_text, today):
    """履歴1件の日付を ISO 形式（YYYY-MM-DD）に正規化する。

    優先順位（today は実行日。テスト可能性のため引数で受け取る）：
      (a) 絶対日付 `2026.11.13` / `2026/11/11` / `2026年11月11日`
          … comic-update-text を最優先に各テキストから探す
      (b) 月日表記（11月11日）… 今年の日付にし、未来なら前年にする
      (c) 相対表記（◯分前・◯時間前 → 今日、◯日前 → today−N、◯週間前 → today−7N）
    どれにも合わなければ None。
    """
    # (a) 絶対日付
    for src in (update_text, date_text, fallback_text):
        m = ABS_DATE_RE.search(src or "")
        if m:
            r = _iso_or_none(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if r:
                return r
    # (b) 月日表記
    for src in (date_text, update_text, fallback_text):
        m = MONTH_DAY_RE.search(src or "")
        if m:
            mo, da = int(m.group(1)), int(m.group(2))
            r = _iso_or_none(today.year, mo, da)
            if r is None:
                continue
            if date(today.year, mo, da) > today:
                r = _iso_or_none(today.year - 1, mo, da)
            return r
    # (c) 相対表記
    for src in (date_text, update_text, fallback_text):
        s = src or ""
        if REL_MIN_RE.search(s) or REL_HOUR_RE.search(s):
            return today.isoformat()
        m = REL_DAY_RE.search(s)
        if m:
            return (today - timedelta(days=int(m.group(1)))).isoformat()
        m = REL_WEEK_RE.search(s)
        if m:
            return (today - timedelta(days=7 * int(m.group(1)))).isoformat()
    return None


ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def renormalize_history_dates(history, today):
    """前回 latest.json から引き継いだ履歴の日付を ISO 形式に直す。

    v2 以前のパーサは date に「11月11日」のような元の表記をそのまま
    保存していた。詳細を取り直さない作品（needs_detail が False）では
    履歴がそのまま引き継がれるため、パーサを通らず旧表記が残り続けて
    いた（第3回チェック後の latest.json で多数確認）。
    絶対日付／月日表記が読み取れるものだけを ISO 化して置き換える。
    相対表記（◯日前など）は旧実行日を基準にしないと換算できないため、
    従来通り元のテキストのまま残す。
    戻り値: (履歴, 直した件数)。元の辞書は書き換えず、直した要素だけ
    コピーして差し替える。
    """
    fixed = 0
    out = []
    for item in history or []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        raw = item.get("date")
        if raw and ISO_DATE_RE.match(str(raw)):
            out.append(item)
            continue
        srcs = [str(raw or ""), item.get("label") or "",
                item.get("title") or ""]
        iso = None
        for src in srcs:  # (a) 絶対日付
            m = ABS_DATE_RE.search(src)
            if m:
                iso = _iso_or_none(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if iso:
                    break
        if iso is None:  # (b) 月日表記（未来なら前年）
            for src in srcs:
                m = MONTH_DAY_RE.search(src)
                if not m:
                    continue
                mo, da = int(m.group(1)), int(m.group(2))
                r = _iso_or_none(today.year, mo, da)
                if r is None:
                    continue
                if date(today.year, mo, da) > today:
                    r = _iso_or_none(today.year - 1, mo, da)
                iso = r
                break
        if iso:
            item = dict(item, date=iso)
            fixed += 1
        out.append(item)
    return out, fixed


# ---------------------------------------------------------------- 詳細ページ解析

def _find_update_list(root):
    """「最近の更新」の ul 要素を探す。

    実HTMLでは div.comic-update の中に見出しと ul が入っている。
    見つからなければ「最近の更新」見出しの直後にある ul を使う。
    """
    box = find_first(root, lambda e: has_class(e, "comic-update"))
    if box is not None:
        ul = find_first(box, lambda e: e.tag == "ul")
        if ul is not None:
            return ul
    els = list(iter_els(root))
    for i, e in enumerate(els):
        if e.tag in ("h1", "h2", "h3", "h4") and "最近の更新" in el_text(e):
            for nxt in els[i + 1:]:
                if nxt.tag == "ul":
                    return nxt
            break
    return None


def _parse_update_item(li, today):
    """「最近の更新」の li 1件を履歴アイテムに変換する。

    話リンクを持つ li:  url=リンク先、label=リンクテキスト、
                        title=title属性（無ければ label）
    話リンクを持たない li: url=None、label=comic-update-text のテキスト
    date は normalize_history_date で ISO 形式に正規化する。
    """
    a = find_first(li, lambda e: e.tag == "a" and _is_external(e.attrs.get("href")))
    date_box = find_first(li, lambda e: has_class(e, "comic-update-date"))
    text_box = find_first(li, lambda e: has_class(e, "comic-update-text"))
    date_text = el_text(date_box) if date_box is not None else ""
    update_text = el_text(text_box) if text_box is not None else ""
    iso = normalize_history_date(update_text, date_text, el_text(li), today)

    if a is not None:
        url = (a.attrs.get("href") or "").strip()
        label = el_text(a)
        title = (a.attrs.get("title") or "").strip() or label
    else:
        url = None
        label = update_text or el_text(li)
        title = label
    if not url and not label:
        return None
    return {"date": iso, "label": label, "title": title, "url": url}


def parse_detail_page(html, today=None):
    """詳細ページを解析する。

    戻り値: (履歴, 作品タイトル, 公式作品ページURL)
      - 履歴: 「最近の更新」の新しい順・最大30件。
              各要素は {"date": ISO日付 or None, "label": 表示文字列,
                        "title": 話タイトル or label, "url": 話URL or None}
      - 作品タイトル: <title> からサイト名を取り除いたもの（補完用）
      - 公式作品ページURL: div.comic-title 内の外部リンク（サムネイル側の
        div.comic-thumb のリンクでも可）。取れなければ None。
    today は日付正規化の基準日（省略時は JST の今日）。
    """
    if today is None:
        today = datetime.now(JST).date()
    root = parse_dom(html)

    title_m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    page_title = clean_page_title(
        " ".join(title_m.group(1).split()) if title_m else ""
    )

    # 公式の作品ページURL（comic-title の見出しリンクが本命。
    # 実HTMLでは comic-thumb のサムネイルリンクも同じURLを指す）
    publisher_url = None
    for cls in ("comic-title", "comic-thumb"):
        box = find_first(root, lambda e, c=cls: has_class(e, c))
        if box is None:
            continue
        a = find_first(
            box, lambda e: e.tag == "a" and _is_external(e.attrs.get("href"))
        )
        if a is not None:
            publisher_url = (a.attrs.get("href") or "").strip() or None
            break

    history = []
    ul = _find_update_list(root)
    if ul is not None:
        for li in ul.children:
            if isinstance(li, str) or li.tag != "li":
                continue
            item = _parse_update_item(li, today)
            if item is not None:
                history.append(item)
            if len(history) >= MAX_HISTORY:
                break
    return history, page_title, publisher_url


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


# ------------------------------------------------- デバッグファイル掃除
# v2〜v3 では実HTMLの構造確認用に data/thumbs/_debug_*.html を保存して
# いたが、第3回チェックで取り込み完成（346件・エラー0）を確認したため
# 保存処理は v4 で撤去した。既にコミット済みのデバッグファイルは、
# 起動時にここで削除する（ワークフローが data/thumbs を git add する
# ため、削除も bot コミットに乗ってリポジトリから消える）。

def cleanup_debug_files():
    """THUMBS_DIR に残っている _debug_*.html を削除し、削除件数を返す。"""
    removed = 0
    if os.path.isdir(THUMBS_DIR):
        for name in os.listdir(THUMBS_DIR):
            if name.startswith("_debug_") and name.endswith(".html"):
                try:
                    os.remove(os.path.join(THUMBS_DIR, name))
                    removed += 1
                except OSError as e:
                    print(f"[checker] debug cleanup failed ({name}): {e}",
                          file=sys.stderr)
    if removed:
        print(f"[checker] removed {removed} leftover debug html file(s)")
    return removed


def main():
    now = datetime.now(JST)
    today = now.date()
    print(f"[checker] start {now.isoformat()} bookmark={BOOKMARK_URL}")

    # v2〜v3 の暫定デバッグファイルが残っていれば掃除する
    cleanup_debug_files()

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
    # 行（div.entry）が0件のページに当たったら終了する。
    # （v1 の不具合：解析件数 < 50 で誤終了。v2 の不具合：行の外の
    #   サイドバーリンクを数えて最終ページ以降も回り続けた）
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
        print(f"[checker] bookmark page {page}: rows={len(page_works)}")
        if not page_works:
            print(f"[checker] bookmark paging stopped at page {page} (no rows)")
            break
        for w in page_works:
            if w["detailUrl"] not in seen:
                seen.add(w["detailUrl"])
                works.append(w)

    # --- アプリから追加された作品（ブックマークに無いもの）
    for u in added_urls:
        if u not in seen:
            seen.add(u)
            works.append(
                {"detailUrl": u, "title": "", "site": None,
                 "thumbUrl": None, "whenText": None,
                 "publisherUrl": None, "comicNo": None, "added": True}
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
    dates_fixed_total = 0  # 引き継ぎ履歴の日付を ISO 化した件数

    for w in works:
        prev = prev_by_url.get(w["detailUrl"])
        history = (prev or {}).get("history") or []
        error = None
        title = w.get("title") or (prev or {}).get("title") or ""
        site = w.get("site") or (prev or {}).get("site") or ""
        thumb_url = w.get("thumbUrl")
        publisher_url = (prev or {}).get("publisherUrl") or w.get("publisherUrl")
        comic_no = w.get("comicNo")
        if comic_no is None:
            comic_no = (prev or {}).get("comicNo")

        if needs_detail(w, prev, today):
            detail_count += 1
            try:
                html = fetch_text(w["detailUrl"])
                polite_sleep()
                new_history, work_title, detail_publisher = parse_detail_page(
                    html, today)
                if new_history:
                    history = new_history
                elif not history:
                    error = "更新履歴を取得できませんでした"
                if not title and work_title:
                    # ブックマーク側でタイトルが取れなかった作品を
                    # 詳細ページの <title> 由来タイトルで補完する
                    title = work_title
                if detail_publisher:
                    publisher_url = detail_publisher
            except Exception as e:
                error = f"詳細ページ取得失敗: {e}"
                polite_sleep()

        # 引き継いだ履歴に旧パーサの生表記（「11月11日」など）が残って
        # いれば ISO 化する（取り直した履歴は元から ISO なので無変化）
        history, n_fixed = renormalize_history_dates(history, today)
        dates_fixed_total += n_fixed

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
                "publisherUrl": publisher_url,
                "comicNo": comic_no,
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
        f"thumbsDownloaded={thumb_count} errors={errors} "
        f"datesFixed={dates_fixed_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
