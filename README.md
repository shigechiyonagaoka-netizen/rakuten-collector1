# 楽天 毎朝100商品コレクター

楽天市場のリアルタイムランキングとROOMシード商品から、
「順位 × レビュー数 × 報酬率」のスコア上位100商品を選び、
ジャンル別・季節トレンド別に並べ替えてGoogleスプレッドシートへ書き出します。
毎朝6時(JST)にGitHub Actionsで自動実行されます。

## セットアップ(最初の1回だけ)

### 1. 楽天ウェブサービスの準備
1. https://webservice.rakuten.co.jp/ でアプリを登録する
2. 「アプリID」と「アクセスキー(pk_から始まる)」を控える
3. アプリ設定の「許可されたWebサイト」にURLを1つ登録する(例: `https://example.com/`)。
   このURLが `RAKUTEN_ORIGIN` になる
4. 楽天アフィリエイトのIDがあれば `RAKUTEN_AFFILIATE_ID` に使う(任意)

### 2. Googleスプレッドシートの準備
1. 空のスプレッドシートを作り、URLの `/d/` と `/edit` の間の文字列を控える(`SPREADSHEET_ID`)
2. Google Cloud Consoleでプロジェクトを作り、「Google Sheets API」を有効にする
3. サービスアカウントを作成し、JSONキーをダウンロードする
4. スプレッドシートの共有設定で、サービスアカウントのメールアドレスを「編集者」として追加する

### 3. GitHubの準備
1. このフォルダの中身を新しいリポジトリにアップロードする(privateを推奨)
2. Settings → Secrets and variables → Actions に、次の6つを登録する

| 名前 | 中身 |
|---|---|
| `RAKUTEN_APP_ID` | アプリID |
| `RAKUTEN_ACCESS_KEY` | アクセスキー |
| `RAKUTEN_ORIGIN` | 許可されたWebサイトのURL |
| `RAKUTEN_AFFILIATE_ID` | アフィリエイトID(なければ空でOK) |
| `SPREADSHEET_ID` | スプレッドシートID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSONキーファイルの中身を丸ごと貼る |

3. Actionsタブ → rakuten-daily → Run workflow で手動実行して動作確認する

## 出力されるシート

| シート | 内容 |
|---|---|
| `today_ジャンル別` | 今日の100商品。ジャンル → 季節タグ → スコア順 |
| `today_季節別` | 同じ100商品。季節タグ → ジャンル → スコア順 |
| `log` | 毎日の履歴。過去7日分の重複除外に使う |
| `room_seeds` | (自分で作る)ROOMで気になった商品のURLを貼る。A列=商品URL、B列=ジャンル名(任意) |

`room_seeds` には `https://item.rakuten.co.jp/ショップ名/商品名/` の形式のURLを貼ってください。

## 調整ポイント(collector.py の上部)

- `GENRES`: ジャンルIDと名前。**実行前に、楽天ランキングのURLでIDが合っているか確認してください**
- `W_RANK / W_REVIEW / W_RATE`: スコアの重み(いまは 0.4 / 0.3 / 0.3)
- `PER_GENRE_CAP`: 1ジャンルの上限数(偏り防止)
- `SEASON_TAGS`: 季節タグのキーワード辞書。商品名にキーワードが含まれるとタグが付く。足りない語は追加する
- `DEDUP_DAYS`: 重複除外の日数

## ローカルでの試運転

```bash
pip install -r requirements.txt
export RAKUTEN_APP_ID=... RAKUTEN_ACCESS_KEY=... RAKUTEN_ORIGIN=https://example.com/
python collector.py --dry-run
```

`--dry-run` はスプレッドシートに書き込まず、結果をコンソールに表示します。

## 注意

- ランキングAPIのURLは、2026年の新エンドポイント(検索APIと同じ並び)で組み立てています。
  403や404が出たら、公式ドキュメントで最新のURLを確認して `RANKING_URL` を直してください
- publicリポジトリは、60日間更新がないとスケジュール実行が自動停止されます。privateなら対象外です
- スケジュール実行は数分〜十数分遅れることがあります
