# toshi — Claude が判断する日本株自動売買システム

株価データを API で取得 → **Claude が buy / sell / hold を判断** → リスク管理で検査 → 自動発注。
状況はブラウザのダッシュボードで管理できます。

```
data (yfinance) ─▶ indicators ─▶ Claude(strategy) ─▶ RiskManager ─▶ Broker(paper / kabuステーション)
                                       ▲                 │              │
                          portfolio ───┘     強制損切り/利確 ┘          ▼
                                                              SQLite ─▶ FastAPI ─▶ ダッシュボード
```

## クイックスタート

```bash
pip install -r requirements.txt
cp .env.example .env          # ANTHROPIC_API_KEY などを設定
python -m toshi               # http://127.0.0.1:8000 を開く
python -m pytest              # テスト
```

- 既定は **paper(仮想売買・資金300万円)**。実際のお金は動きません。
- `ANTHROPIC_API_KEY` 未設定でも、単純なルールベース戦略で動作します(ダッシュボードに表示)。
- 外部ネットワーク無しで試す場合: `TOSHI_DATA=synthetic`。

## 機能

| 領域 | 内容 |
|---|---|
| 戦略 | Claude に日足指標(SMA5/25/75, RSI, ATR, 20日高安, 出来高比)とポートフォリオを渡し、ツール呼び出しで構造化された売買判断(銘柄・単元数・確信度・理由)を取得。API失敗時はルールベースに自動フォールバック |
| リスク管理 | Claude より優先。損切り7% / トレーリング10% / 利確20% の**強制執行**、1銘柄上限30%、最大5銘柄、現金10%維持、日次損失3%で新規買い停止、1日最大注文数、空売り禁止、100株単位。確信度0.55未満の買いは見送り |
| ブローカー | `PaperBroker`(既定) / `KabuStationBroker`(auカブコム証券 kabuステーション API・現物成行) |
| スケジューラ | 立会時間(平日 9:00–11:30 / 12:30–15:30 JST)中に `TOSHI_INTERVAL_MIN` 分間隔で実行 |
| ダッシュボード | 総資産/損益、資産推移、保有、Claude の判断と理由ログ(却下理由つき)、注文履歴、**手動実行**、**キルスイッチ**(新規買い停止) |

## 実売買(live)に切り替える前に

1. 実売買には `TOSHI_MODE=live` **かつ** `TOSHI_LIVE_CONFIRM=yes` の両方が必要です。
2. `KabuStationBroker` は **実環境で未検証**です。kabuステーションの検証用ポート(`:18081`)で、まず動作確認してください。
3. 最低でも数週間 paper で成績・判断ログ・リスク挙動を確認してください。
4. 株価データ(yfinance)は遅延・欠損があり得ます。実運用では J-Quants や証券会社の板情報 API への差し替えを推奨します (`toshi/data.py` の `DataProvider` を実装)。
5. 祝日・年末年始の休場は未考慮です(データが更新されないだけで誤発注は起きにくい設計)。
6. ダッシュボードを `127.0.0.1` 以外に公開する場合は `TOSHI_DASH_TOKEN` が必須です(未設定だと起動しません)。
7. **本システムは利益を保証しません。投資判断・損失の責任は利用者にあります。**

## 構成

```
toshi/config.py    設定(.env)           toshi/risk.py      リスク管理・強制エグジット
toshi/data.py      株価データ取得        toshi/broker.py    Paper / kabuステーション
toshi/indicators.py 指標計算             toshi/engine.py    1サイクルの実行・スケジューラ
toshi/strategy.py  Claude戦略 + fallback toshi/web/        FastAPI + ダッシュボード(index.html)
```
