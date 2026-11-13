# Webコミック管理

Web漫画アンテナ（webcomics.jp）の公開ブックマークを情報源に、読んでいるWebコミックの更新をチェックして未読リストで管理する個人用ツールです。

## 使い方

GitHub Pages の公開URL（https://mbrbjjkb-jpg.github.io/webcomic/）を開き、初回だけ専用トークン（このリポジトリへの Contents 読み書きのみ許可した Fine-grained PAT）を入力します。以後はパソコン・スマホどちらからでも同じ未読状態が見られます。

## 構成

- `index.html` … 画面アプリ（単一HTML）
- `checker.py` … 更新チェッカー。GitHub Actions で1日4回（日本時間 0:05/6:05/12:05/18:05）自動実行
- `data/library.json` … 読了位置（アプリだけが書く）
- `data/latest.json` … 最新話・更新履歴（checker だけが書く）
- `data/thumbs/` … サムネイル画像（checker が保存）
