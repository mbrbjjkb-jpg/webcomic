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
  6. マンガワン作品だけは履歴に話URLが無いため、公式の話一覧から
     話ラベルを照合して話URLを補完する（専用処理の節を参照）
  7. イラスト・告知等の「話でない更新」は履歴に残しつつ extra の印を
     付け、latest（最新話表示用）は本編エントリから選ぶ（案2。専用処理
     の節を参照）

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


def fetch_final_url(url):
    """GET してリダイレクトを追従し、最終URLを返す（本体は読み捨てる）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        res.read()
        return res.geturl()


def fetch_post_bytes(url):
    """ボディ無しの POST でバイト列を取得する（マンガワンAPI用）。"""
    req = urllib.request.Request(
        url, data=b"", headers={"User-Agent": USER_AGENT}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


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


# ---------------------------------------------------------------- マンガワン専用対応
# webcomics.jp の履歴ではマンガワン作品の話URLが全件 None になるため、
# 公式サイトのビューアAPIから話一覧（話ラベル→章ID）を取得して補完する。
#
# 仕組み（2026-11-14 に実地確認）:
#   - publisherUrl（https://manga-one.com/viewer/<番号>）は
#     https://manga-one.com/manga/<title_id>/chapter/<chapter_id> へ
#     リダイレクトされる。ここから title_id と起点の chapter_id を得る
#   - 話一覧はサイトの内部API（protobuf形式）で得られる:
#       POST https://manga-one.com/api/client?rq=viewer_v2
#         &title_id=..&chapter_id=..&list_type=chapter&sort_type=desc
#         &page=N&limit=10
#     応答（WebViewerResponse）の field 11 が話一覧セクションで、
#     その field 1（繰り返し）が各話: 1=章ID(varint), 2=話ラベル,
#     3=サブタイトル, 5=公開日。保存する話URLは
#     https://manga-one.com/manga/<title_id>/chapter/<章ID>（クエリ無し）
#   - ページ送りは未補完の履歴が残る場合のみ。最大3ページ（30話分）まで

MANGAONE_API_URL = "https://manga-one.com/api/client"
MANGAONE_MAX_PAGES = 3
MANGAONE_PAGE_LIMIT = 10
MANGAONE_CHAPTER_URL_RE = re.compile(r"/manga/(\d+)/chapter/(\d+)")
_EPISODE_PART_RE = re.compile(r"^(前編|中編|後編)")


def _pb_read_varint(buf, pos):
    shift = 0
    val = 0
    while True:
        if pos >= len(buf):
            raise ValueError("protobuf: varint が途中で切れている")
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf: varint が長すぎる")


def _pb_fields(buf):
    """protobuf の wire 形式を (フィールド番号, wire種別, 値) の列にする。

    varint は int、length-delimited は bytes、それ以外（固定長）は
    bytes のまま返す。壊れた入力では例外にする。
    """
    pos = 0
    out = []
    while pos < len(buf):
        key, pos = _pb_read_varint(buf, pos)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise ValueError("protobuf: フィールド番号0")
        if wt == 0:
            val, pos = _pb_read_varint(buf, pos)
            out.append((field, wt, val))
        elif wt == 2:
            ln, pos = _pb_read_varint(buf, pos)
            if pos + ln > len(buf):
                raise ValueError("protobuf: 長さ超過")
            out.append((field, wt, buf[pos:pos + ln]))
            pos += ln
        elif wt == 1:
            out.append((field, wt, buf[pos:pos + 8]))
            pos += 8
        elif wt == 5:
            out.append((field, wt, buf[pos:pos + 4]))
            pos += 4
        else:
            raise ValueError(f"protobuf: 未対応の wire 種別 {wt}")
    return out


def parse_mangaone_chapter_list(data):
    """ビューアAPI応答から話一覧 [(章ID, 話ラベル, サブタイトル)] を取り出す。

    WebViewerResponse の field 11（話一覧セクション）の field 1 が各話。
    各話メッセージ: 1=章ID(varint), 2=話ラベル, 3=サブタイトル（任意）。
    取れなければ空リスト。
    """
    entries = []
    try:
        for field, wt, val in _pb_fields(data):
            if field != 11 or wt != 2:
                continue
            for f1, w1, v1 in _pb_fields(val):
                if f1 != 1 or w1 != 2:
                    continue
                chapter_id = None
                label = None
                subtitle = None
                for f2, w2, v2 in _pb_fields(v1):
                    if f2 == 1 and w2 == 0:
                        chapter_id = v2
                    elif f2 == 2 and w2 == 2:
                        label = v2.decode("utf-8", errors="replace")
                    elif f2 == 3 and w2 == 2:
                        subtitle = v2.decode("utf-8", errors="replace")
                if chapter_id is not None and label:
                    entries.append((chapter_id, label, subtitle))
    except ValueError:
        return []
    return entries


def _mangaone_label_key(label):
    """話ラベルの照合キー（空白除去・全角数字を半角化）。"""
    s = "".join((label or "").split())
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def is_mangaone_work(site, publisher_url):
    if site and "マンガワン" in site:
        return True
    return bool(publisher_url) and "manga-one.com" in publisher_url


def enrich_mangaone_history(history, publisher_url):
    """マンガワン作品の履歴に話URLを補完する。(履歴, 補完件数) を返す。

    補完できないエントリ（話一覧に無い「休載のお知らせ」等）は
    url=None のまま残す。失敗しても履歴は壊さず、元のまま返す。
    """
    missing = [h for h in history if not h.get("url")]
    if not missing or not publisher_url:
        return history, 0
    try:
        final_url = fetch_final_url(publisher_url)
        polite_sleep()
        m = MANGAONE_CHAPTER_URL_RE.search(final_url or "")
        if not m:
            print(f"[checker] mangaone: リダイレクト先からIDを取れません "
                  f"({publisher_url} -> {final_url})", file=sys.stderr)
            return history, 0
        title_id, first_chapter_id = m.group(1), m.group(2)

        mapping = {}
        for page in range(1, MANGAONE_MAX_PAGES + 1):
            api_url = (
                f"{MANGAONE_API_URL}?rq=viewer_v2&title_id={title_id}"
                f"&chapter_id={first_chapter_id}&list_type=chapter"
                f"&sort_type=desc&page={page}&limit={MANGAONE_PAGE_LIMIT}"
            )
            data = fetch_post_bytes(api_url)
            polite_sleep()
            entries = parse_mangaone_chapter_list(data)
            if not entries:
                break
            for chapter_id, label, subtitle in entries:
                url = (f"https://manga-one.com/manga/{title_id}"
                       f"/chapter/{chapter_id}")
                key = _mangaone_label_key(label)
                if key and key not in mapping:
                    mapping[key] = url
                # 分割話（第N話＋サブタイトル先頭の前編/中編/後編）用に
                # 合成キーも登録する（履歴ラベルが「第34話前編」形式の
                # 場合に、公式ラベル「第34話」と食い違うため）
                if subtitle:
                    pm = _EPISODE_PART_RE.match(subtitle.strip())
                    if pm:
                        pkey = _mangaone_label_key(label + pm.group(1))
                        if pkey and pkey not in mapping:
                            mapping[pkey] = url
            if len(entries) < MANGAONE_PAGE_LIMIT:
                break  # 最終ページ
            if all(
                _mangaone_label_key(h.get("label") or h.get("title")) in mapping
                for h in missing
            ):
                break  # 未補完分がすべて解決した
    except Exception as e:
        print(f"[checker] mangaone enrich failed ({publisher_url}): {e}",
              file=sys.stderr)
        return history, 0

    filled = 0
    for h in history:
        if not h.get("url"):
            url = mapping.get(
                _mangaone_label_key(h.get("label") or h.get("title")))
            if url:
                h["url"] = url
                filled += 1
    return history, filled


# ---------------------------------------------------------------- 話URL補完（日付照合型）
# ガンガンONLINE／COMIC FUZ／がうがう は、webcomics.jp の履歴が
# 「日付だけ」で話URLを持たない。そのため公式の作品ページから
# 話一覧を取り、日付で履歴エントリと突き合わせて話URLを補完する
# （マンガワン対応と同じ枠組みだが、照合キーは日付）。
#   - ガンガン: 作品ページの __NEXT_DATA__
#     （props.pageProps.data.default.chapters）に話一覧があり、
#     publishingPeriod の開始日が更新日。status 3（次回更新）は除外
#   - FUZ: 作品ページの __NEXT_DATA__（pageProps.chapters の各
#     セクション）に話一覧があり、updatedDate が更新日。話URLは
#     https://comic-fuz.com/manga/viewer/<chapterId>
#   - がうがう: 作品ページの話グリッドに話リンクと更新日がある
#     （古い話には日付表示が無いものがあり、それらは照合できない）
# 日付が同じ話が複数ある場合は本編を優先し、同種なら古い方
# （続きの開始にふさわしい方）を採用する。失敗しても履歴は壊さない。
# pixivコミックは作品ページが完全なクライアント描画で、公開APIも
# 見つからなかったため、今回は対象外（2026-11-14 調査）。

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S)


def _next_data_json(html):
    """Next.js ページの __NEXT_DATA__ JSON を返す（無ければ None）。"""
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _is_main_episode_label(label):
    """特典・おまけ等の非本編ラベルかどうか（本編なら True）。"""
    return not any(kw in (label or "")
                   for kw in ("特典", "おまけ", "告知", "お知らせ"))


def parse_gangan_episodes(html, title_id):
    """ガンガン作品ページから (更新日ISO, ラベル, 話URL, 本編か) の一覧を返す。"""
    data = _next_data_json(html)
    chapters = []
    try:
        chapters = data["props"]["pageProps"]["data"]["default"]["chapters"] or []
    except Exception:
        return []
    out = []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        label = (ch.get("mainText") or "").strip()
        if not label or label.startswith("次回更新"):
            continue
        if ch.get("status") == 3:
            continue
        m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})",
                     ch.get("publishingPeriod") or "")
        cid = ch.get("id")
        if not m or cid is None:
            continue
        date_iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        url = (f"https://www.ganganonline.com/title/{title_id}"
               f"/chapter/{cid}")
        out.append((date_iso, label, url, _is_main_episode_label(label)))
    return out


def parse_fuz_episodes(html):
    """FUZ作品ページから (更新日ISO, ラベル, 話URL, 本編か) の一覧を返す。"""
    data = _next_data_json(html)
    sections = []
    try:
        sections = data["props"]["pageProps"]["chapters"] or []
    except Exception:
        return []
    out = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        for ch in sec.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            label = (ch.get("chapterMainName") or "").strip()
            m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})",
                         ch.get("updatedDate") or "")
            cid = ch.get("chapterId")
            if not label or not m or cid is None:
                continue
            date_iso = (f"{m.group(1)}-{int(m.group(2)):02d}"
                        f"-{int(m.group(3)):02d}")
            url = f"https://comic-fuz.com/manga/viewer/{cid}"
            out.append((date_iso, label, url, _is_main_episode_label(label)))
    return out


def parse_gaugau_episodes(html):
    """がうがう作品ページから (更新日ISO, ラベル, 話URL, 本編か) の一覧を返す。"""
    out = []
    blocks = re.split(r'<div class="episode__grid">', html or "")
    for b in blocks[1:]:
        m = re.search(
            r'<a href="(https://gaugau\.futabanet\.jp/list/work/[^"]+'
            r'/episodes/\d+)"', b)
        if not m:
            continue
        url = m.group(1)
        num = re.search(r'<div class="episode__num">([^<]*)</div>', b)
        label = (num.group(1).strip() if num else "") or "更新"
        dm = re.search(r"<span>(\d{4})年(\d{1,2})月(\d{1,2})日 更新</span>", b)
        if not dm:
            continue  # 日付が無い古い話は照合できない
        date_iso = (f"{dm.group(1)}-{int(dm.group(2)):02d}"
                    f"-{int(dm.group(3)):02d}")
        is_main = label.startswith("第") and _is_main_episode_label(label)
        out.append((date_iso, label, url, is_main))
    return out


def fill_history_urls_by_date(history, episodes):
    """履歴の url=None エントリを、日付で突き合わせた話URLで補完する。

    (履歴, 補完件数) を返す。同日の話が複数ある場合は本編を優先し、
    同種なら一覧の後方（＝古い方）を採用する。
    """
    missing = [h for h in history if not h.get("url")]
    if not missing or not episodes:
        return history, 0
    by_date = {}
    for date_iso, _label, url, is_main in episodes:
        cur = by_date.get(date_iso)
        if cur is None or is_main or is_main == cur[1]:
            by_date[date_iso] = (url, is_main)
    filled = 0
    for h in history:
        if not h.get("url"):
            cand = by_date.get(h.get("date"))
            if cand:
                h["url"] = cand[0]
                filled += 1
    return history, filled


def _enrich_by_date_history(history, publisher_url, parser, site_name):
    """日付照合型の補完の共通処理（parser は作品ページHTMLを受け取る）。"""
    if not any(not h.get("url") for h in history):
        return history, 0
    try:
        html = fetch_text(publisher_url)
        polite_sleep()
        return fill_history_urls_by_date(history, parser(html))
    except Exception as e:
        print(f"[checker] {site_name} enrich failed ({publisher_url}): {e}",
              file=sys.stderr)
        return history, 0


def is_gangan_work(site, publisher_url):
    if site and "ガンガン" in site:
        return True
    return bool(publisher_url) and "ganganonline.com" in publisher_url


def is_fuz_work(site, publisher_url):
    if site and "FUZ" in (site or ""):
        return True
    return bool(publisher_url) and "comic-fuz.com" in publisher_url


def is_gaugau_work(site, publisher_url):
    if site and "がうがう" in site:
        return True
    return bool(publisher_url) and "gaugau.futabanet.jp" in publisher_url


def enrich_gangan_history(history, publisher_url):
    """ガンガン作品の履歴に話URLを補完する。(履歴, 補完件数) を返す。"""
    m = re.search(r"ganganonline\.com/title/(\d+)", publisher_url or "")
    if not m:
        return history, 0
    title_id = m.group(1)
    return _enrich_by_date_history(
        history, publisher_url,
        lambda html: parse_gangan_episodes(html, title_id), "gangan")


def enrich_fuz_history(history, publisher_url):
    """FUZ作品の履歴に話URLを補完する。(履歴, 補完件数) を返す。"""
    return _enrich_by_date_history(
        history, publisher_url, parse_fuz_episodes, "fuz")


def enrich_gaugau_history(history, publisher_url):
    """がうがう作品の履歴に話URLを補完する。(履歴, 補完件数) を返す。"""
    return _enrich_by_date_history(
        history, publisher_url, parse_gaugau_episodes, "gaugau")


# ---------------------------------------------------------------- 非本編の分類（案2）
# webcomics.jp の「最近の更新」には、本編の話だけでなくイラスト・
# 人気投票企画・休載のお知らせ等の「話でない更新」も並ぶ。これらが
# 履歴の先頭に来ると、従来は latest（＝アプリの「最新話」表示）まで
# 非本編になってしまい、本編の最新話と誤認される（2026-11-14 に
# 『魔都精兵のスレイブ』の「[イラスト+第3回人気投票企画]」で報告）。
#
# ユーザー決定の案2（2026-11-14）:
#   - 非本編も履歴には残し、未読判定・未読件数は従来どおり数える
#   - 非本編エントリには "extra": true の印を付ける（アプリ側は
#     この印を見て「続きを読む」の飛び先を本編から選ぶ）
#   - latest は履歴の中で最も新しい本編エントリにする。履歴の
#     全てが非本編の作品では従来どおり先頭にする（空にしない）
#
# 非本編とみなすキーワード（ラベル／タイトルへの部分一致）。
# 本編のラベル（「更新」「2026/11/13 更新」「11.13(金)」
# 「[第122話]…」等）は、いずれのキーワードも含まない。

EXTRA_LABEL_KEYWORDS = (
    "イラスト", "人気投票", "お知らせ", "休載", "告知",
    "企画", "アンケート", "募集", "発売記念", "宣伝",
)


def is_extra_entry(entry):
    """履歴エントリが「話でない更新」（非本編）かどうか。

    既に "extra" の印が付いているものはそのまま非本編として扱い、
    印が無いものはラベル／タイトルにキーワードを含むかで判定する。
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("extra"):
        return True
    text = " ".join([entry.get("label") or "", entry.get("title") or ""])
    return any(kw in text for kw in EXTRA_LABEL_KEYWORDS)


def mark_extra_entries(history):
    """非本編エントリに "extra": True の印を付けた履歴を返す。

    本編エントリには印を付けない（印が無い＝本編、という後方互換。
    旧データの履歴も、チェックのたびにこの処理を通すことで印が
    付く）。元の辞書は書き換えず、印を付ける要素だけコピーして
    差し替える。
    """
    out = []
    for entry in history or []:
        if is_extra_entry(entry) and not entry.get("extra"):
            entry = dict(entry, extra=True)
        out.append(entry)
    return out


def select_latest(history):
    """latest（最新話表示用）に使うエントリを選ぶ。

    履歴の中で最も新しい本編エントリ。全部が非本編なら先頭
    （従来どおり）。履歴が空なら None。
    """
    for entry in history or []:
        if not is_extra_entry(entry):
            return entry
    return history[0] if history else None


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
    """THUMBS_DIR に残っている _debug_*.html / _probe_*.html を削除し、削除件数を返す。"""
    removed = 0
    if os.path.isdir(THUMBS_DIR):
        for name in os.listdir(THUMBS_DIR):
            if (name.startswith("_debug_") or name.startswith("_probe_")) \
                    and name.endswith(".html"):
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
    mangaone_filled_total = 0  # マンガワン作品で話URLを補完した件数
    extra_entries_total = 0  # 非本編（extra）の印が付いた履歴件数

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

        # マンガワン専用対応：履歴に話URLが無いマンガワン作品は、
        # 公式の話一覧から話URLを補完する（他サイトには影響しない）
        if history and is_mangaone_work(site, publisher_url):
            history, n_filled = enrich_mangaone_history(
                history, publisher_url)
            mangaone_filled_total += n_filled

        # 日付照合型の話URL補完：ガンガン／FUZ／がうがう（2026-11-14）
        if history and is_gangan_work(site, publisher_url):
            history, n_filled = enrich_gangan_history(
                history, publisher_url)
            mangaone_filled_total += n_filled
        if history and is_fuz_work(site, publisher_url):
            history, n_filled = enrich_fuz_history(history, publisher_url)
            mangaone_filled_total += n_filled
        if history and is_gaugau_work(site, publisher_url):
            history, n_filled = enrich_gaugau_history(history, publisher_url)
            mangaone_filled_total += n_filled

        # 非本編の分類（案2）：履歴が確定した最終ステップとして、
        # イラスト・告知等の「話でない更新」に印を付ける。履歴自体と
        # 未読判定は従来どおりで、latest の再選択（下の out_works 側）
        # とアプリの飛び先選択だけがこの印を使う
        history = mark_extra_entries(history)
        extra_entries_total += sum(1 for h in history if h.get("extra"))

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
                # 案2：最新話表示は本編から選ぶ（履歴先頭がイラスト・
                # 告知等の非本編でも、本編の最新話を指すようにする）
                "latest": select_latest(history),
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
        f"datesFixed={dates_fixed_total} mangaoneFilled={mangaone_filled_total} "
        f"extraEntries={extra_entries_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
