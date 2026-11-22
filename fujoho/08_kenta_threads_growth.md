# Threads 最初の100人 調査メモ(けんた / 2026-11-22)

## 調査の限界(先に読む)
- WebSearchの要約結果のみ。各ページ本文の精読はできていない(buffer.comは取得不可・ネットワーク許可外)。よって下の「確認済み」は「検索結果が示した記述」であり、一次情報での再確認は未実施。
- 数字(最適時間・頻度の回数など)は出典の調査条件が不明なため、断定しない。
- 夫婦・離婚・恋愛ジャンルのThreadsアカウント分析は、今回の検索では該当情報が出なかった。

## 【確認済み(出典URL付き・検索結果の記述)】

### (3) おすすめ表示の仕組み(公式・準公式)
- 2024年11月、Instagram責任者Adam Mosseriが「フォローしている人の投稿を優先し、フォローしていないアカウントのおすすめを減らす」方向へリバランスすると発表(Threads上の投稿)。「work in progress」とも述べた。
  - https://techcrunch.com/2024/11/21/threads-adjusts-its-algorithm-to-show-you-more-content-from-accounts-you-follow
  - https://thewrap.com/threads-newsfeed-changes-adam-mosseri
  - https://www.tubefilter.com/2024/11/26/threads-feed-accounts-followers-adam-mosseri/
- Mosseriは低品質なおすすめが多いことを謝罪し、改善に取り組むと述べた。
  - https://www.engadget.com/instagram-head-adam-mosseri-says-sorry-for-all-those-trashy-threads-recommendations-193001655.html
- 新規アカウント個別についてのMosseri発言は、今回は見つからなかった。

### (6) トピックタグ
- 2023年12月に導入。1投稿につき付けられるタグは1つだけ。「#」記号は投稿上では表示されず青いリンクになる。スペースを含む語も使える。タグの画面がそのまま検索画面になる。Mosseriは、タグ乱用(大量付与)を避け、エンゲージメント稼ぎよりコミュニティ重視にする意図を示した。
  - https://www.techno-edge.net/article/2023/12/08/2420.html
  - https://www.watch.impress.co.jp/docs/news/1553009.html
  - https://www.androidcentral.com/apps-software/meta-threads-adds-hashtags
  - https://martech.org/marketers-can-now-use-topic-tags-on-threads-to-boost-engagement/
- 注意: 上記はすべて導入時(2023年末)の記事。現行仕様はアプリ内・公式ヘルプで要確認。

### (5) スパム・制限
- 2023年7月、Mosseriがスパム増加を理由にレート制限(同種操作の回数上限)を導入と説明。正規ユーザーが誤判定される可能性にも言及。
  - https://techcrunch.com/2023/07/17/the-spam-bots-have-now-found-threads-as-company-announces-its-own-rate-limits
  - https://www.gizmodo.jp/article/restrictions-on-the-use-of-threads/
- フォロー・返信の一括操作に関する具体的な上限値・現行規約は、今回の検索では確認できなかった。

### (2) 返信・時間帯(二次情報)
- 評価の中心は「いいね」より返信と会話の継続、とする国内記事がある。
  - https://addness.co.jp/media/threads-algorithms/
- コムニコの記事に「インプレッションの半分は返信由来」との見出し。根拠の詳細は未確認。
  - https://www.comnico.jp/we-love-social/sns-trend-summit-threads
- 時間帯: 複数調査をまとめた記事では平日午前8時〜正午(特に火〜木)が比較的一貫。ただし調査ごとにデータ・方法・タイムゾーンが異なり数字は一致しない。Bufferは推奨時間帯を約2週間試して自分のデータで判断することを勧める。
  - https://www.eclincher.com/articles/best-time-to-post-on-threads-in-2026
  - https://buffer.com/resources/the-best-time-to-post-on-threads/
- 頻度: 頻度が高いほど1投稿あたり表示が増える傾向、目安は週2〜5回〜毎日1回とする記事がある(出典の検証条件は未確認)。
  - https://addness.co.jp/media/threads-algorithms/
- トピック設定済み投稿は未設定より成績が良いと「公式データでも裏付け」と書く記事がある(公式原典は未確認)。
  - https://addness.co.jp/media/threads-algorithms/

### (1) 立ち上げの一般論(個人・メディア記事。再現性は保証されない)
- 同ジャンルの投稿に返信し会話に加わる、人として振る舞う、が共通して挙がる。
  - https://buffer.com/resources/threads-growth-plan/
- 誰の何の悩みを解決するかを一文で定義する。
  - https://app.notion.com/p/The-Threads-Starter-Kit-3893512ad2a380a48cf0c69cabd2c4a7
- Xの投稿の使い回しは避け、読者層を理解する。
  - https://blog.hubspot.com/marketing/how-brands-excel-on-threads

## 【仮説(要検証)】
1. 新規は閲覧が少ない前提で、最初の2週間は「投稿」より「同ジャンル投稿への返信」の比重を上げるほうが露出に繋がる。(出典は二次情報のみ。自アカウントで検証)
2. 悩みの当事者が反応しやすいのは「状況+感情の一言」型の冒頭。(ジャンル別データ未確認)
3. 返信は、相手の悩みに寄り添う内容に限定し、プロフ誘導・LINE誘導は入れないほうがスパム判定・信頼低下を避けられる。
4. 2週間は同じ時間帯(例: 朝と夜)で固定投稿し、保存・返信・プロフ遷移の数でA/B比較する。
5. トピックタグは1投稿1つ、悩みに近い語を選ぶ(候補はアプリ内の他者作成タグ)。
6. 夜間に当事者が見る可能性があるため、夜の時間帯も検証枠に入れる。
7. 100人到達までの日数は、今回の調査では目安を示せない。

## 【不明】
- 新規アカウントの閲覧が少ない時期の公式な説明、その期間の長さ。
- 返信・フォロー・投稿頻度の具体的な数値効果(一次データ)。
- 夫婦・離婚・恋愛ジャンルで伸びるアカウントの特徴(今回の検索では該当情報なし。追加調査が必要)。個人を特定する調査はしない。
- フォロー・返信の現行の具体的な制限値と、スパム判定の基準。
- 現行のトピックタグ仕様と、タグ有無の効果の一次情報。
- Mosseriが返信の価値について述べたとされる発言(二次情報のみ、原典未確認)。

## 避けるべき行為(確認できた範囲+注意)
- 確認済み: レート制限が存在し、スパム対策で誤判定もありうる(上記TechCrunch)。
- 仮説: 短時間の大量フォロー、同文面の返信連投、無関係な投稿へのリンク付き返信、タグの乱用は制限・低評価のリスク。具体的な閾値は不明。公式のコミュニティガイドライン・利用規約で要確認。
- 本アカウント固有の禁止: 他者の不倫相手・個人の特定や晒しに繋がる返信、不倫の勧誘・美化、法律の断定、PR表記なしの広告(CLAUDE.md準拠)。

## さくらへの引き継ぎ(5行以内)
1. 最初の2週間は「投稿+同ジャンルへの返信」を毎日セットで設計。返信は共感のみ、リンク・誘導なし。
2. 投稿時間は朝・夜など2〜3枠に固定し、2週間で保存・返信・プロフ遷移を比較。時間帯の正解は未確定。
3. トピックタグは1投稿1つ。現行仕様はアプリ内で確認してから運用ルール化。
4. 一括フォロー・同文返信連投は禁止ルールに入れる。具体的上限は不明のため少なめで運用。
5. このジャンルの伸びる型は未調査。追加調査が必要(さくらの判断で依頼を)。
