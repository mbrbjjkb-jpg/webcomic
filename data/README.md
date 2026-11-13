# data フォルダ

Webコミック管理のデータ置き場です（リポジトリ `webcomic` 内）。

- `library.json` … 画面アプリだけが書く。読了位置（works は detailUrl をキーに `{lastReadUrl, lastReadTitle}`）と、アプリから追加した作品URL（added）。
- `latest.json` … checker だけが書く。各作品の更新履歴・最新話・最終チェック日時。
- `thumbs/` … checker が保存するサムネイル画像。

書き手をファイルごとに分けることで、アプリと checker の同時書き込み競合を避けています。
