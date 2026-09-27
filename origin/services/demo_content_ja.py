"""Japanese demo content — the `"ja"` half of the bilingual demo dataset.

Pure data, and a strict structural mirror of `demo_content_en`. The two
modules are interchangeable to the seeding logic precisely because they
agree on shape; `assert_content_parity` in `demo_seeder` is that
agreement written down as an assertion, and `test_demo_bilingual` runs
it, so a drift between the tables fails CI rather than a visitor's
signin.

The structural contract, in the order it bites:

  * Same constants (`_CONTENT_KEYS`), same dict keys, same list lengths.
  * Same task ORDER. `parent_idx` indexes the task list *positionally*,
    so one extra or missing task here silently re-parents every task
    after it — a bug that produces a plausible-looking workspace with
    the wrong hierarchy.
  * `status` and `priority` are DB enum values, never translated:
    `Open` / `WIP` / `Pending` / `Closed`, `Critical` / `High` /
    `Medium` / `Low`.
  * `assignee`, `bot_index`, `parent_index`, `is_milestone_child` and
    `due_offset_days` are wiring, not prose. Copied, never adapted.

What differs from English on purpose:

  * **Bot names stay Latin.** `Alice` / `Bob` / `Carol` / `Dan` are
    looked up by exact username in the eval suites, and their emails are
    built from the given name, which must stay ASCII. Latin names in a
    Japanese workspace are unremarkable for a tech-company demo.
  * **Technical nouns and numerals stay Latin.** `framer-motion`,
    `WAAPI`, `Lighthouse`, `95`, `120KB`, `40KB`, `Dovetail`,
    `Plausible`, `design system v3` and friends are kept verbatim inside
    Japanese sentences — not for the evals (those assert against the
    English table) but because they are the concrete details that make
    the AI's answer sound like it read the team's history.
  * **Titles are fully Japanese, including the eval-pinned ones.** The
    retrieval suites do pin English title substrings, but they pin them
    against the EN table: `evals/fixture.py` calls
    `create_demo_environment` with no `lang`, so no eval ever seeds this
    module. Leaving English in a title to appease an eval would only show
    a Japanese visitor a half-translated workspace. Titles are still
    conceptually diffable against EN, entry for entry.
  * **Register is mixed by surface**, mirroring the English: chat and
    comments are casual 常体/口語; task, note and milestone bodies are
    簡潔な常体; the two notes that address the visitor directly (welcome,
    Spotlight tips) and the first two self-DM messages use です・ます.

`BOT_ROLES` and the note/todo LABELS live in `demo_seeder` alongside the
logic that consumes them. Everything with paragraphs in it lives here.
"""

from __future__ import annotations

from origin.services.demo_blocks import _body

WEBSITE_MILESTONE_BODY = _body(
    (
        "🎯 ゴール",
        [
            "v1.0 の一般公開に向けて、リニューアルしたマーケティングサイトを genos.app/ にリリースする。新しいページはアクセシビリティ監査を通過し、Lighthouse モバイルのパフォーマンス予算に収まっていることが条件。",
        ],
    ),
    (
        "✅ 成功条件",
        [
            (
                "bullet",
                "マーケティングページ 4 枚（home / pricing / about / contact）すべてが @genos/design-system v3 だけで描画される — Bootstrap の import は 1 つも残さない。",
            ),
            ("bullet", "各ページで Lighthouse モバイルスコアが 95 以上。"),
            ("bullet", "axe-core で Critical / Serious の違反が 0 件。"),
            (
                "bullet",
                "ヒーローの CTA、料金プランのトグル、問い合わせフォーム送信について Plausible のイベント計測が稼働している。",
            ),
        ],
    ),
    (
        "📦 対象範囲",
        [
            ("bullet", "Bootstrap のレイアウトプリミティブをデザインシステムの Grid / Stack に置き換える。"),
            ("bullet", "ヒーロー、フィーチャーグリッド、料金テーブルをゼロから作り直す。"),
            ("bullet", "Plausible を入れて、上記の CTA に名前付きイベントを設定する。"),
        ],
    ),
    (
        "🚫 対象外",
        [
            ("bullet", "ブログページのリニューアル（v1.1 に先送り）。"),
            ("bullet", "多言語対応 — ローンチ時点では英語のみ。"),
            ("bullet", "マーケティングサイトのダークモード（アプリ内のダークモードは別件）。"),
        ],
    ),
    (
        "⚠️ リスクと依存関係",
        [
            "Carol からのヒーローイラストのハンドオフがクリティカルパス。これが来ないとホームページの作り直しはブロックされる。代替案として、見出しの背後をプレースホルダーのグラデーションにしたまま出し、イラストは v1.0.1 で差し替える。",
        ],
    ),
)

ROADMAP_MILESTONE_BODY = _body(
    (
        "🎯 ゴール",
        [
            "顧客の声に裏づけられたロードマップ提案で Q2 のディスカバリーサイクルを締める。中身は、12 本のインタビュー文字起こしのテーマコーディング、Notion / Slack / Linear の競合ティアダウン、そして Q3 向けの投資テーマ上位 5 件の提言。",
        ],
    ),
    (
        "✅ 成功条件",
        [
            ("bullet", "顧客インタビューの文字起こし 12 本を Dovetail 上で 8 個以下のテーマにコーディング済み。"),
            (
                "bullet",
                "競合分析が Notion / Slack / Linear それぞれの機能・価格・オンボーディングを網羅している。",
            ),
            (
                "bullet",
                "ロードマップ提案ドキュメントが経営陣のレビューを通り、Q2 の期限内に着地する。",
            ),
            (
                "bullet",
                "Q3 提案の各投資テーマが、インタビューの引用か競合データを少なくとも 1 つ根拠に挙げている。",
            ),
        ],
    ),
    (
        "📦 対象範囲",
        [
            ("bullet", "顧客インタビューの分析まとめ（テーマコーディング＋クラスタごとのサマリ）。"),
            ("bullet", "競合プロダクトのティアダウン（競合 3 社、各社 3 領域）。"),
            ("bullet", "Q3 の投資テーマ上位 5 件のドキュメント（工数見積もり付き）。"),
        ],
    ),
    (
        "🚫 対象外",
        [
            ("bullet", "価格の実験（別ワークストリーム）。"),
            ("bullet", "エンジニアリングのキャパシティプランニング（EM チームが担当）。"),
        ],
    ),
    (
        "⚠️ リスクと依存関係",
        [
            "インタビューの対象が偏っている。12 セグメントのうちエンタープライズのユーザーは 7 件だけ。分析まとめがエンタープライズ寄りのテーマを示した場合、追加ラウンドを回さずに SMB へ一般化するのは慎重にいきたい。",
        ],
    ),
)


# Each entry is a project's full content blueprint. The seeder iterates
# in order and threads task IDs through subtasks / notes / pm threads.

PROJECT_BLUEPRINTS = [
    {
        "name": "Web サイトリニューアル",
        "tags": [
            ("フロントエンド", "#7c3aed", "#ffffff"),
            ("デザイン", "#ec4899", "#ffffff"),
            ("パフォーマンス", "#f59e0b", "#ffffff"),
            ("バグ", "#ef4444", "#ffffff"),
        ],
        "milestone": {
            "title": "v1.0 一般公開ローンチ",
            "body": WEBSITE_MILESTONE_BODY,
            "status": "Open",
            "priority": "High",
            "due_offset_days": 28,
        },
        # Tasks: index 0..N-1. `parent_idx` points to another task in
        # this list (its index) to indicate "subtask under that task";
        # otherwise None. `is_milestone_child` attaches the task to
        # the milestone (parent_task_id = backing.task_id, milestone
        # FK set). Subtasks override that — their parent_task_id is
        # the indicated task, not the backing.
        "tasks": [
            {
                "key": "spike_framer_motion",
                "title": "スパイク調査: リニューアルでの framer-motion と native CSS アニメーションの比較",
                "status": "Closed",
                "priority": "Low",
                "assignee": "bot2",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 9,
                "body": _body(
                    (
                        "❓ 論点",
                        [
                            "今回のリニューアルで、ヒーロー／ドロワー／モーダルのトランジションに framer-motion を使うか、それとも CSS アニメーションと Web Animations API で済ませるか。",
                        ],
                    ),
                    (
                        "💡 仮説",
                        [
                            "framer-motion のほうが書き味はいいが、gzip 後 40KB というサイズがマーケティングページのパフォーマンス予算と正面からぶつかる。実際に予定しているアニメーションなら CSS だけで足りる可能性がある。",
                        ],
                    ),
                    (
                        "🧭 進め方",
                        [
                            (
                                "bullet",
                                "予定しているアニメーション（ドロワーのスライド、モーダルのフェード、ヒーローのスタガーアニメーション）を洗い出し、それぞれ純粋な CSS で実現できるかを整理する。",
                            ),
                            (
                                "bullet",
                                "いちばん難しいもの（ヒーローのスタガーアニメーション）を両方のやり方でプロトタイプし、書き味とバンドルへの影響を比較する。",
                            ),
                            (
                                "bullet",
                                "@genos/design-system でリリースしている他チームのやり方も確認し、足並みを揃える。",
                            ),
                        ],
                    ),
                    (
                        "⏱ タイムボックス",
                        [
                            "2 日間。ドロワーのアニメーションが最初の実適用になるので、ナビゲーションの PR がマージされる前に結論を出す。",
                        ],
                    ),
                    (
                        "✅ 決定事項",
                        [
                            "マーケティングページでは framer-motion を使わない。ドロワー、モーダルのフェード、ヒーローのスタガーアニメーションはすべて CSS アニメーションで実装し、ドロワーの中断可能なスライドだけ Web Animations API を使う。",
                        ],
                    ),
                    (
                        "📌 調査結果",
                        [
                            (
                                "bullet",
                                "バンドルサイズ: framer-motion は gzip 後 40KB。WAAPI 版のドロワーのプロトタイプは 800 バイトだった。120KB という JS 予算全体の 3 分の 1 を、コンポーネント 1 つに使ってしまうことになる。",
                            ),
                            (
                                "bullet",
                                "予定していた 3 つのアニメーションはいずれも純粋な CSS で実現できた。ヒーローのスタガーアニメーションはスプリングではなく nth-child でディレイを段階的にずらす形になったが、この規模なら見た目上の差はない。",
                            ),
                            (
                                "bullet",
                                "CSS だけなら SSR も単純なままで済む。マーケティングページで hydration の不一致が起きない。",
                            ),
                            (
                                "bullet",
                                "アプリ内は別途見直す。アプリ側にマーケティングのパフォーマンス予算はないので、framer-motion の書き味が勝つ可能性は十分ある。別のスパイク調査として管理する。",
                            ),
                        ],
                    ),
                    (
                        "🚧 対象外",
                        [
                            (
                                "bullet",
                                "アプリ内のアニメーション（サイドバーのトランジションなど）。判断は別途行う。このスパイク調査はマーケティングページに限定する。",
                            ),
                            ("bullet", "GSAP。ライセンス上の理由ですでに見送り済み。"),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot2",
                        "マーケティング側は CSS だけで行く方向に傾いてる。純粋な CSS で本当に厄介なのはドロワーだけだし、それも Bob が Web Animations API のプロトタイプをもう作ってる。",
                    ),
                    (
                        "bot1",
                        "確認した。自分のプロトタイプは 800 バイト、framer-motion は 40KB。マーケティングページなら比べるまでもない。",
                    ),
                ],
            },

            {
                "key": "migrate_marketing_pages",
                "title": "マーケティングページを design system v3 へ移行",
                "status": "Open",
                "priority": "High",
                "assignee": "demo",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 14,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "Bootstrap ベースの旧マーケティングページを @genos/design-system v3 のプリミティブで置き換える。対象はホーム・料金・会社概要・お問い合わせの4ページ。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "現行ページは Bootstrap 4 の上に 2019 年当時の CSS 上書きを積み重ねた状態。axe-core では Critical が2件（コントラスト不足、landmark ロールの欠落）で落ちており、Bootstrap バンドルから配信される未使用 CSS のせいで Lighthouse モバイルは 71 が上限。さらに今回のリニューアルはローンチのブロッカーにもなっている — マーケティング側は新ページに先にトラフィックを流したい意向。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "4ページすべてが @genos/design-system v3 のプリミティブのみで描画されること。",
                            ),
                            ("bullet", "各ページで Lighthouse モバイルスコアが 95 以上。"),
                            ("bullet", "axe-core で Critical / Serious の違反が0件。"),
                            (
                                "bullet",
                                "ビジュアルリグレッションスイート（Chromatic）が desktop + mobile + iPad の各ブレークポイントで通ること。",
                            ),
                        ],
                    ),
                    (
                        "🎯 メモ・参考リンク",
                        [
                            "Carol からの Hero イラストのハンドオフは Figma ファイル「Marketing v3 hero」にある。下のサブタスクで監査フェーズと実装フェーズに分割済み。Plausible のアナリティクス対応（別タスク）は公開 DNS 切り替えより前に入るよう調整すること。",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "着手する。まずはどこがまだ Bootstrap を使っているか監査して、影響範囲を洗い出すところから。",
                    ),
                    (
                        "bot1",
                        "注意点。料金テーブルにカスタムの Bootstrap グリッド上書きが入っていて、ここ数ヶ月ずっと壊れやすい。あのパターンを引き継ぐより、料金コンポーネントはゼロから書き直した方がいいと思う。",
                    ),
                    (
                        "bot2",
                        "Hero のイラスト、Figma に上げた。ファイルは「Marketing v3 hero」。バリアントを2つ添付してある（グラデーションと写真）。パフォーマンス的にはグラデーションの方が良さそう。",
                    ),
                ],
            },
            {
                "key": "audit_marketing_pages",
                "title": "既存マーケティングページの棚卸し",
                "status": "Closed",
                "priority": "Normal",
                "assignee": "demo",
                "is_milestone_child": False,
                "parent_idx": 1,
                "due_offset_days": 3,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "全マーケティングページを棚卸しし、まだ Bootstrap に依存しているコンポーネントを列挙する。この結果を移行順の決定に使う。",
                        ],
                    ),
                    (
                        "📌 調査結果",
                        [
                            (
                                "bullet",
                                "ホームページ: hero、feature grid、testimonials — すべて Bootstrap の row 上に乗っている。",
                            ),
                            (
                                "bullet",
                                "料金: カスタムの Bootstrap グリッド上書き（壊れやすく、全面書き直しを推奨）。",
                            ),
                            (
                                "bullet",
                                "会社概要: ほぼ素の HTML で Bootstrap 依存は最小。移行コストは最も低い。",
                            ),
                            (
                                "bullet",
                                "お問い合わせ: フォームが Bootstrap の form-group クラスと jQuery バリデーションを使っている — design system の Form プリミティブへの置き換えが必要。",
                            ),
                        ],
                    ),
                    (
                        "🎯 メモ・参考リンク",
                        [
                            "推奨する移行順: 会社概要 → お問い合わせ → 料金 → ホームページ。会社概要が最もリスクが低く、ホームページはインパクト最大だが、小さいページでパターンを検証してから入れたい。",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "監査終わった。一番ひどいのは料金ページ — 既存マークアップを移植しようとせず、コンポーネントごと書き直すのを推奨する。",
                    ),
                    (
                        "bot1",
                        "賛成。あそこのグリッド上書きが、ずっと報告が来ているセント表記のズレのバグの原因でもある。きれいに書き直せば一石二鳥。",
                    ),
                ],
            },
            {
                "key": "build_hero_featuregrid",
                "title": "design system v3 で Hero + FeatureGrid を実装",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot2",
                "is_milestone_child": False,
                "parent_idx": 1,
                "due_offset_days": 7,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "@genos/design-system v3 に Hero と FeatureGrid コンポーネントを実装し、Storybook のカバレッジも揃える。この2つがホームページ再構築の土台で、料金ページと会社概要ページでも再利用する。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "Hero が画像・動画・イラストのバリアントを単一の `media` prop で切り替えられること。",
                            ),
                            (
                                "bullet",
                                "FeatureGrid が 1 / 2 / 3 カラムでレスポンシブに動き、コンポーネント個別のメディアクエリを持たないこと — design system のトークン経由で CSS コンテナクエリを使う。",
                            ),
                            (
                                "bullet",
                                "Storybook のストーリーが全バリアントとローディング状態・空状態をカバーしていること。",
                            ),
                            ("bullet", "すべての Storybook ストーリーで axe-core が通ること。"),
                        ],
                    ),
                    (
                        "🎯 メモ・参考リンク",
                        [
                            "Hero はほぼ完了 — Carol のイラストはチームのレビュー待ちなのでフィーチャーフラグの裏で繋いである。FeatureGrid は iPad 横向きのケース向けに 3カラム → 2カラムのコンテナクエリのブレークポイントを詰める作業が残っている。",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot2",
                        "Hero はだいたいできた。グラデーション版を指標と突き合わせて確認中なので、イラストはフィーチャーフラグの裏で繋いでおく。",
                    ),
                    (
                        "bot1",
                        "コンテナクエリのアプローチ、コードレビューがほしい — iPad 横向きのブレークポイントが怪しい気がする。Bob に振っておく。",
                    ),
                ],
            },

            {
                "key": "responsive_navigation",
                "title": "レスポンシブナビゲーションとモバイルドロワーを実装",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot1",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 10,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "既存のナビゲーションを単一のレスポンシブコンポーネントに置き換える。768px 未満ではハンバーガードロワー、それ以上では横並びのフルレイアウトに切り替える。キーボードのフォーカス順序とスクリーンリーダー向けの landmark も正しく揃えること。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "現状のナビゲーションはデスクトップ用とモバイル用で 2 つの別コンポーネントに分かれていて、常に片方だけ実装がずれていく。今四半期のバグ 6 件は、モバイルだけ／デスクトップだけで起きたナビゲーションのリグレッションだった。1 つのレスポンシブコンポーネントに統合すれば、このズレ自体がなくなる。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "単一の Nav コンポーネントが 320px / 768px / 1024px / 1440px のブレークポイントで正しく描画されること。",
                            ),
                            (
                                "bullet",
                                "ドロワーが開いている間はフォーカスをトラップし、閉じたときにトグルへフォーカスを戻すこと。",
                            ),
                            ("bullet", "Escape キーでドロワーが閉じること。"),
                            ("bullet", "skip-to-content リンクが最初のフォーカス可能要素であること。"),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot1",
                        "最初の PR 上げた — 320px 未満（かなり小さい画面）だとドロワーのアニメーションが気持ち悪い。spring の設定を詰める。",
                    ),
                    (
                        "demo",
                        "320px のケースは Storybook のストーリーを足しておいて、リグレッションしないようにしよう。viewport テストを回せば Cypress でも拾える。",
                    ),
                ],
            },
            {
                "key": "plausible_analytics",
                "title": "Plausible のアナリティクスと名前付きイベントを設定",
                "status": "Open",
                "priority": "Normal",
                "assignee": "bot1",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 17,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "新しいマーケティングページに Plausible Analytics を組み込み、今回のリニューアル公開の初日からトラフィック計測とページビュー数が取れる状態にする。その上でヒーローの CTA、料金プランのトグル、問い合わせフォーム送信に名前付きのカスタムイベントを仕込む。Plausible はプライバシーに配慮した設計で GDPR にも準拠している — Google Analytics はその理由で対象外。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "マーケティング側は、新ページで実際に何が効いているかを知るために訪問トラフィックを計測したい。どのページが一番ページビューを稼いでいるか、どこでトラフィックが落ちるか、ヒーローの CTA の文言のどれがサインアップにつながるか（A/B テストは v1.1 に積んである）。前四半期の GDPR レビュー以降、サイト解析とトラフィック計測は Plausible がチーム全体の標準になっている。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "サイト共通レイアウト経由で、新しいマーケティングページすべてに Plausible のスクリプトが読み込まれていること — ページビューと訪問セッションが自動で計測される。",
                            ),
                            (
                                "bullet",
                                "カスタムイベント: `hero_cta_click`、`pricing_toggle_monthly_to_annual`、`pricing_toggle_annual_to_monthly`、`contact_form_submit`。",
                            ),
                            (
                                "bullet",
                                "ステージングの Plausible ダッシュボードで、トラフィックの数値（ページビュー、ユニークビジター、直帰率）とカスタムイベントの両方が見えること。",
                            ),
                            ("bullet", "イベントのプロパティに PII（メールアドレス、氏名など）を含めないこと。"),
                        ],
                    ),
                    (
                        "🎯 メモ・参考リンク",
                        [
                            "Plausible のドメインはすでに用意済み: genos-marketing.plausible.io。トラフィックのダッシュボードはマーケティングに閲覧権限があり、スクリプトタグはエンジニアリング側の管理。",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot1",
                        "レイアウトのファイルでコンフリクトしたくないので、これはナビゲーションのマージ後に入れる。",
                    ),
                    (
                        "bot0",
                        "マーケティングから要望 — `pricing_card_hover` のイベントも足せる？どのプランが一番注目されてるか見たいらしい。",
                    ),
                    ("bot1", "OK、それは簡単に足せる。同じ PR に入れておく。"),
                ],
            },
            {
                "key": "accessibility_audit",
                "title": "アクセシビリティ監査と改修対応",
                "status": "Pending",
                "priority": "Normal",
                "assignee": "bot3",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 21,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "リニューアルしたマーケティングページ全体について、axe-core と手動のスクリーンリーダーテストで WCAG 2.1 AA の監査を実施する。Critical / Serious の指摘はローンチ前にすべてチケット化して修正する。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "既存ページには axe-core の Critical 違反が 2 件ある。セカンダリボタンのコントラスト不足（3.8:1 — 4.5:1 以上が必要）と、ナビゲーションの landmark ロールの欠落。どちらも 1 年以上放置されたままで、VPAT のドキュメント提出が条件になっているエンタープライズ調達の商談をブロックしている。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "マーケティングページ 4 ページすべてで、axe-core の Critical / Serious 違反が 0 件。",
                            ),
                            (
                                "bullet",
                                "すべてのインタラクティブ要素がキーボードだけで到達・操作できること。",
                            ),
                            (
                                "bullet",
                                "スクリーンリーダー（macOS Safari の VoiceOver、Windows Firefox の NVDA）で通して読み上げたとき、各ページが破綻のない一続きの流れとして理解できること。",
                            ),
                            (
                                "bullet",
                                "本文テキストはコントラスト比 4.5:1 以上、大きいテキストと UI コンポーネントは 3:1 以上。",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot3",
                        "実物の画面を対象にテストしたいので、FeatureGrid の PR が入ったら監査を始める。最新のトークンでセカンダリボタンのコントラストを Carol に確認してもらえれば、事前監査はもっと早く進む。",
                    ),
                    (
                        "bot2",
                        "セカンダリボタンは `--color-action-secondary` を使っていて、`--color-surface-default` に対して 4.6:1 ある。問題ないはずだけど、コントラストチェッカーで再確認しておく。",
                    ),
                ],
            },

            {
                "key": "slow_homepage_load",
                "title": "モバイル Safari でのホームページ読み込み遅延を調査",
                "status": "WIP",
                "priority": "Normal",
                "assignee": "bot1",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 5,
                "body": _body(
                    (
                        "🐞 概要",
                        [
                            "iOS Safari 16 / 17 に限って、ホームページの読み込みに 4〜6 秒かかるという報告が上がっている。同じネットワーク・同じ端末の Chrome なら約 1 秒で読み込める。",
                        ],
                    ),
                    (
                        "🔁 再現手順",
                        [
                            ("bullet", "iOS 16.5 以降の iPhone で Safari を開く。"),
                            ("bullet", "4G 回線で https://genos.app/ にアクセスする。"),
                            (
                                "bullet",
                                "Safari の Web インスペクタで time-to-interactive を計測する。",
                            ),
                        ],
                    ),
                    (
                        "🎯 期待される挙動",
                        [
                            "モバイルでもホームページが 2.5 秒以内に操作可能になること。同じ端末の Chrome と同等の水準。",
                        ],
                    ),
                    (
                        "💥 実際の挙動",
                        [
                            "time-to-interactive は 4.2〜6.1 秒。ウォーターフォールを見るとパース中に長いブロッキングタスクがあり、いまだに配信されている旧 Bootstrap CSS が原因と思われる。メモリ使用量もパフォーマンス予算を大きく超えてスパイクしている。",
                        ],
                    ),
                    (
                        "🧪 環境",
                        [
                            ("bullet", "iPhone 12 と iPhone 14 の Safari 16.5 / 17.0。"),
                            ("bullet", "Safari の開発者ツールで 4G にスロットリング。"),
                            ("bullet", "staging と production の両方で再現を確認済み。"),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot1",
                        "ざっとプロファイルを取った感じ、原因は Bootstrap CSS のバンドルっぽい。デザインシステム移行が入れば自然に解消しそうだけど、切り替え後にパフォーマンストレースで裏を取っておきたい。",
                    ),
                    (
                        "bot3",
                        "移行が入ったら QA 側で staging ビルドのトレースを取り直せる。",
                    ),
                    # ---- Sub-task-shaped follow-up. ----
                    # Demo surface for the agent's break-a-task-into-
                    # sub-tasks flow (create_task_plan with
                    # parent_task_id): the comments propose a concrete
                    # split of THIS task's work — the natural ask is
                    # "add these as sub-tasks of this task", never a new
                    # milestone (the milestone-from-a-thread demo lives
                    # in the group chat's mobile-speed thread instead).
                    (
                        "bot1",
                        "これ、直すところが一箇所じゃなさそう。トレースを見た感じだとこう分けたい: (1) デザインシステム切り替え後に実機でプロファイルを取り直す、(2) v3 と並行してまだ配信されている旧 Bootstrap CSS を削る、(3) この種の遅延が再発しないようリグレッションチェックを入れる。",
                    ),
                    (
                        "bot3",
                        "分け方に異論なし。実機での取り直しとリグレッションチェックは QA で引き取れる。",
                    ),
                    (
                        "demo",
                        "それでいこう。個別に追えるように、このタスクのサブタスクとして追加しよう。",
                    ),
                ],
            },
        ],
        "pm_messages": [
            (
                "demo",
                "Web サイトリニューアルのキックオフ！Sprint 1 ではマーケティングページの移行とレスポンシブナビゲーションをやる。詳細な計画はマイルストーンに書いた。",
            ),
            (
                "bot1",
                "レスポンシブナビゲーションと Plausible のセットアップは自分がやる。ドロワーのプロトタイプはもう PR #1284 に上げてあるので、見たい人はどうぞ。",
            ),
            (
                "bot2",
                "Hero のイラストは Figma でハンドオフ済み — “Marketing v3 hero”。バリアントは 2 つ。エンジニアチーム、不明点あれば声かけて。",
            ),
            (
                "bot0",
                "マーケから質問: ローンチ日はいつ伝えられる？プレス向けの告知を止めて待ってる状態。",
            ),
            (
                "demo",
                "マーケティングページ 4 本を移行した状態で、Sprint 2 末のソフトローンチを狙ってる。一般公開はその 1 スプリント後、アクセシビリティ監査が通ることが条件。",
            ),
            (
                "bot3",
                "FeatureGrid が入ったら監査を始める。セカンダリボタンのコントラストがトークン側で直らなければ、そこでブロックされる。",
            ),
            ("bot2", "セカンダリボタンのコントラストは v3.2 のトークンリリースに入ってる — 今日入る。"),
            (
                "demo",
                "いいね。Sprint 1 のデモにチェックポイントを追加: 移行済みの About ページを一緒に見て、他ページに展開する前にパターンを確認したい。",
            ),
            (
                "bot1",
                "了解。レビューするものが要るので、About の移行は金曜に出せる規模に抑える。",
            ),
            # --- 意思決定: 時間の制約下でのスコープ縮小。日付を動かすの
            # ではなく「受け入れ条件」を変えるのがポイント。「4ページを
            # 遅れて出す」と「3ページ+簡易版で予定どおり出す」は別の判断。
            (
                "bot1",
                "Sprint 2 の現実的な見込み。Pricing ページの比較表が design system にまだない。あのコンポーネントをちゃんと作ると、それだけで 3〜4 日かかる。",
            ),
            (
                "demo",
                "だと 4 ページは無理だね。ソフトローンチを止めるか、3 ページで出すか、どっちがましだと思う？",
            ),
            (
                "bot0",
                "3 ページでもマーケは動ける。ただし落とすページに Pricing が入らないこと。プレスの売り込みがリンクするのはそのページだから。",
            ),
            (
                "bot1",
                "まさにその Pricing が危ない。入れ替える: Pricing は簡易版の表で出す — 機能比較のマトリクスは入れず、プラン 3 つのカードだけ。マトリクスは別タスクに切る。",
            ),
            (
                "demo",
                "それでいこう。ソフトローンチの日付は維持、Pricing は簡易版の表で出す、比較マトリクスは Sprint 3 の別タスク。移行タスクの受け入れ条件を「3 ページ + 簡易版 Pricing」に変更しておく。",
            ),
            (
                "bot3",
                "監査の観点で1つ。簡易版でも表のヘッダーの関連付けはちゃんとやらないと、スクリーンリーダーには数字の並びとしか読まれない。ローンチ時に見つかるのは避けたい。",
            ),
            (
                "bot1",
                "了解 — 修正パスに残さず、簡易版を作る時点でヘッダーのマークアップまでやる。",
            ),
            # --- 意思決定: 明示的に「保留」にする。解除の条件も一緒に
            # 決める。ボード上では「ブロック中」と同じ見え方でも、判断と
            # しては別物。
            (
                "bot2",
                "別件。ヒーローの写真版、まだ要る？2 週間ほど止まったままで、ブランド撮影がまた延びると古くなる。",
            ),
            (
                "demo",
                "中途半端に抱えるのはやめて、きちんと保留にしよう。ローンチはグラデーション版でいく。Sprint 3 が終わる前に撮影が入れば見直す、入らなければローンチ後の変更で。",
            ),
            (
                "bot2",
                "それでいい。Figma で半端に生かしておくより、書き残してもらえる方が助かる。",
            ),
        ],
        "pm_thread": {
            "parent_index": 4,  # demo's "Targeting end of Sprint 2..." message
            "messages": [
                (
                    "bot0",
                    "Sprint 2 末のソフトローンチ、マーケ側はそれで問題ない。あとプレス日程も制約としてマイルストーンに書いておいてもらえる？埋もれると困るので。",
                ),
                (
                    "demo",
                    "入れた — リスクと依存関係のセクションに追記した。売り込みの時間が要るので、ソフトローンチと一般公開の間は営業日で最低 5 日空けたいとのこと。",
                ),
                ("bot0", "助かる。マーケのカレンダーにその前提で押さえておく。"),
                (
                    "bot1",
                    "自分の方は — ソフトローンチ後のアクセシビリティ監査で 4 ページのどれかに手直しが必要になった場合、その週がバッファになる。",
                ),
            ],
        },
    },

    {
        "name": "Q2 ロードマップ",
        "tags": [
            ("計画", "#3b82f6", "#ffffff"),
            ("リサーチ", "#10b981", "#ffffff"),
            ("顧客", "#f97316", "#ffffff"),
            ("仕様", "#8b5cf6", "#ffffff"),
        ],
        "milestone": {
            "title": "Q2 ディスカバリースプリント",
            "body": ROADMAP_MILESTONE_BODY,
            "status": "Open",
            "priority": "High",
            "due_offset_days": 24,
        },
        "tasks": [
            {
                "key": "synthesize_interviews",
                "title": "顧客インタビュー 12 件をテーマに分析まとめ",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot0",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 12,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "12 件の顧客インタビューの録音を Dovetail でテーマに分類する。成果物は逐語引用つきのクラスタサマリ ≤ 8 本で、そのままロードマップ提案の材料になる。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "コーディングした分析まとめがないと、最も代表的なパターンではなく声の大きかったインタビューに引っ張られたロードマップになる。コーディングすればシグナルの出現頻度を数えざるを得なくなる。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            ("bullet", "12 件の文字起こしすべてを Dovetail にアップロードし、タグ付けを完了している。"),
                            (
                                "bullet",
                                "テーマごとに裏付けとなる引用を 1 つ以上つけてコーディングしている。",
                            ),
                            ("bullet", "クラスタサマリを ≤ 8 本作成、各サマリ ≤ 200 語。"),
                            (
                                "bullet",
                                "カバレッジを明示: どのセグメントがどのテーマに寄与したか（エンタープライズと SMB）。",
                            ),
                        ],
                    ),
                    (
                        "🎯 メモ・参考リンク",
                        [
                            "インタビューのセグメント構成: エンタープライズ 7、SMB 4、プロシューマー 1。偏りは意図的だが、SMB だけへの一般化には使えない。サマリのカバレッジ表記で経営層にもこの点が伝わるようにする。",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot0",
                        "コーディング開始した。早めのシグナルとして、通知と検索が必ずセットで出てくる。みんな「見落としたものをアシスタントが出してほしい」と言ってる。",
                    ),
                    (
                        "demo",
                        "それは面白い。「プロアクティブな提示」とリアクティブな検索は別テーマとして立てる価値がある。早い段階でまとめてしまわないようにしよう。",
                    ),
                ],
            },
            {
                "key": "schedule_interviews",
                "title": "残り 5 件のインタビューを日程調整",
                "status": "Closed",
                "priority": "Normal",
                "assignee": "bot0",
                "is_milestone_child": False,
                "parent_idx": 0,
                "due_offset_days": 2,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "最後の 5 件の顧客インタビューを確定させる — エンタープライズ管理者 2 名、SMB オーナー 2 名、プロシューマーのヘビーユーザー 1 名。",
                        ],
                    ),
                    (
                        "📌 調査結果",
                        [
                            (
                                "bullet",
                                "5 件すべて予約完了。Calendly のリンク、録音の同意、事前資料を送付済み。",
                            ),
                            (
                                "bullet",
                                "エンタープライズ管理者 1 名から $100 のお礼ギフトカードの希望あり — ops のキューに追加した。",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot0",
                        "全部予約取れた。最後は木曜の午後。録音の同意は先にもらってあるので、分析まとめはすぐ進められる。",
                    ),
                ],
            },
            {
                "key": "code_recordings",
                "title": "録音をテーマ別にコーディング（Dovetail）",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot0",
                "is_milestone_child": False,
                "parent_idx": 0,
                "due_offset_days": 10,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "Dovetail の自動文字起こしを使い、そのあと合意済みのタクソノミー（ジョブ、ペイン、現行のワークアラウンド、決め手になる不満点）で各文字起こしを人手でコーディングする。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            ("bullet", "12 件の文字起こしすべてを 4 軸のタクソノミーでコーディング済み。"),
                            (
                                "bullet",
                                "2 件の文字起こしで demo を二人目のコーダーに立てて、コーダー間の一致度をチェック。",
                            ),
                            ("bullet", "コードブックを確定させ、チームに共有済み。"),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot0",
                        "今のところ 12 件中 4 件をコーディング済み。自動文字起こしはまあまあだけど、1 段落おきに抜き取りチェックしてる。訛りと発言の被りはまだ拾いきれてない。",
                    ),
                ],
            },
            {
                "key": "competitor_analysis",
                "title": "競合分析: Notion / Slack / Linear",
                "status": "Open",
                "priority": "Normal",
                "assignee": "demo",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 17,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "競合 3 製品を機能・価格・オンボーディングの面から分解する。成果物は比較マトリクスと、どこが遅れていて、同等で、勝っているかをまとめた文章。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "エンタープライズ案件の失注のうち 40% は Linear、25% は Notion に取られている。セールスからは明確なトークポイントが欲しいと言われているし、分解資料がないと場当たり的な比較に頼って、直近の競合の変更を見落とす。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "競合 3 社を並べた機能マトリクスを、25-30 行のケイパビリティで作成。",
                            ),
                            (
                                "bullet",
                                "価格マトリクスにシート単価、請求サイクル、エンタープライズの最低契約条件を含める。",
                            ),
                            ("bullet", "各社のオンボーディングのウォークスルーのスクリーンショット。"),
                            (
                                "bullet",
                                "劣っている点をまとめた文章と、ギャップごとの推奨アクション。",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "Linear が先月 Cycles → Sprints のリネームを出した。裏側のモデルまで変わったのか、名前の入れ替えだけなのかは見ておきたい。",
                    ),
                    ("bot1", "changelog を見た限りリネームだけっぽい。モデルは変わってない。"),
                ],
            },

            {
                "key": "roadmap_proposal",
                "title": "ロードマップ提案 v1 — Q3 の投資テーマ上位 5 件",
                "status": "Open",
                "priority": "High",
                "assignee": "demo",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 22,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "v1 のロードマップ提案を書く。Q3 向けの具体的な投資テーマ 5 件について、期待する成果・根拠となるリンク・工数のざっくり見積もりをそれぞれ付ける。経営陣のレビューと優先順位付けの議論にそのまま使える形を優先する。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            ("bullet", "投資テーマは 5 件、各 ≤ 300 語。"),
                            (
                                "bullet",
                                "各投資テーマに顧客インタビューの引用を最低 1 件、かつ競合のデータポイントを最低 1 件引く。",
                            ),
                            (
                                "bullet",
                                "投資テーマごとに概算規模（S / M / L / XL）と主担当チームを明記する。",
                            ),
                            ("bullet", "60 分のワーキングセッションで経営陣とレビューする。"),
                        ],
                    ),
                    (
                        "🎯 メモ・参考リンク",
                        [
                            "ここまでの有力候補: (1) プロアクティブな提示を行うアシスタント、(2) Linear のティアダウンを踏まえたスプリントボードの磨き込み、(3) オンボーディングの全面刷新、(4) エンタープライズ向けの管理機能 / SSO の成熟度、(5) 公開 API。分析まとめが出れば順序は変わる。",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "エビデンス欄が空のまま出したくないので、分析まとめが出るまで提案のドラフトは止めてる。",
                    ),
                    (
                        "bot0",
                        "分析まとめは木曜目標。連休をドラフト書く時間に充てられるはず。",
                    ),
                ],
            },
            {
                "key": "onboarding_funnel",
                "title": "オンボーディングファネルの分解分析",
                "status": "Pending",
                "priority": "Normal",
                "assignee": "bot3",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 19,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "サインアップから最初のタスク作成までの現行のオンボーディングファネルを一通り通し、各ステップの離脱を計測できるようにする。成果物は Sankey ダイアグラムと、離脱の多い上位 3 ステップに対する改善提案。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "初日アクティベーション率が全体で 34% だというのは分かっているが、残りの 66% がどこで抜けているかは分かっていない。ステップ単位で見えないままでは、オンボーディングをどう作り直しても当て推量になる。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            (
                                "bullet",
                                "Plausible でファネルを計測できている（サインアップ → チーム作成 → 最初のプロジェクト → 最初のタスク）。",
                            ),
                            ("bullet", "2 週間分のトラフィックの Sankey ダイアグラム。"),
                            (
                                "bullet",
                                "離脱の多い上位 3 ステップを特定し、検証すべき仮説をセットで出す。",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot3",
                        "Plausible のカスタムイベントを仕込むのにエンジニアの手を借りたい。マーケティングページの Plausible タスクと一緒に片付けられない?",
                    ),
                    ("bot1", "うん、余裕。アプリ内のイベントも同じ PR に入れる。"),
                ],
            },

            {
                "key": "q3_okrs",
                "title": "Q3 OKRs のドラフトを定義",
                "status": "Open",
                "priority": "High",
                "assignee": "demo",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 26,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "ロードマップ提案から出てくる 5 件の投資テーマを軸に、Q3 OKRs のドラフトを書く。投資テーマごとに Objective を 1 つ、Key Result を 2 つ置く。",
                        ],
                    ),
                    (
                        "✅ 受け入れ条件",
                        [
                            ("bullet", "Objective 5 件、Key Result は合計 10 件。"),
                            ("bullet", "KR はすべて計測可能にする（数値 / パーセント / 達成・未達）。"),
                            ("bullet", "提出前に各主担当チームとレビューする。"),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "ドラフトはロードマップ提案の後に回す。投資テーマが固まる前に OKR をコミットしても意味ない。",
                    ),
                ],
            },
            {
                "key": "stakeholder_map",
                "title": "社内ステークホルダーマップを更新",
                "status": "Open",
                "priority": "Low",
                "assignee": "bot0",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 30,
                "body": _body(
                    (
                        "🧾 概要",
                        [
                            "社内のステークホルダーマップを最新化する。どの投資テーマをだれに、どの頻度で共有するかを整理する。昨年の Q4 から更新が止まっている。",
                        ],
                    ),
                    (
                        "🪜 背景",
                        [
                            "マーケティング・サポート・セールス・財務のそれぞれを、Q3 の投資テーマに対して適切な頻度と深さで巻き込む必要がある。整理されたマップがないと、共有しすぎ（会議過多）か共有不足（ステークホルダーが後から驚く）のどちらかになる。",
                        ],
                    ),
                ),
                "comments": [
                    ("bot0", "ロードマップ提案が着地したら手を入れる。"),
                ],
            },
        ],
        "pm_messages": [
            (
                "demo",
                "Q2 Discovery スタート。Alice、インタビューの分析まとめはお願い — 対象に偏りがあるので、サマリでそこは明記しておきたい。",
            ),
            (
                "bot0",
                "了解。12 件のうち 5 件はまだ日程調整中だけど、calendly のリンクは今日全部送る。",
            ),
            (
                "bot0",
                "最初の 4 本の文字起こしから早めのシグナル: 通知と検索が必ずセットで出てくる。単独でテーマを立てる価値あり。",
            ),
            (
                "demo",
                "面白い。1 テーマか 2 テーマかの判断は、少なくとも 8 本コーディングするまで保留で。序盤のコーディングでバイアスがかかるのは本当にある。",
            ),
            (
                "bot3",
                "こっちは — オンボーディングの分解分析が Plausible のイベント待ちでブロックされてる。マーケティングのアナリティクス対応の一部として入れられるよう Bob と調整中。",
            ),
            ("bot1", "アプリ内のイベントも同じ PR に入れる。Sprint 1 末には入るはず。"),
            (
                "bot0",
                "分析まとめの目標は Sprint 1 末。テーマと競合の分解分析が揃ったら、Sprint 2 でロードマップのドラフトに入る。",
            ),
            (
                "demo",
                "経営陣レビューから逆算すると、提案は 3 週間で着地させる必要がある。きついけど、分析まとめが予定どおりなら行ける。",
            ),
            ("bot0", "うん。インタビューがずれてスケジュールに響きそうなら即あげる。"),
            # --- 決定: 優先度の「反転」。一度決めたことを、新しい事実が
            # 出てきたので覆す。デモに欠けていたのはこの形 — 「X に決めた」
            # ではなく「X に決めたが、Y が出てきて考えを変えた。Y とはこれだ」。
            (
                "bot0",
                "インタビュー 2 件が来週にずれた — どちらもエンタープライズ。完全なカバレッジを待つか、12 件のうち 10 件で分析まとめをしてエンタープライズ層を欠損として扱うか、どっちかになる。",
            ),
            (
                "demo",
                "手元の 10 件はどっちに偏ってる？",
            ),
            (
                "bot0",
                "小規模チーム。10 件のうち 8 件が 20 席未満。これが問題で、Q3 で投資する可能性が一番高い施策こそ、エンタープライズが反対しそうなものなんだよね。",
            ),
            (
                "demo",
                "なら 1 週間待つ価値がある。分析まとめの期日を後ろにずらして、空いた分は競合の分解分析を前倒しで埋める — 提案が遅れて正しいほうが、期日どおりで小規模チームに偏ってるよりいい。",
            ),
            (
                "bot3",
                "分解分析を前倒しすると、今スプリントのオンボーディングファネルの作業は落ちる。もともと Plausible のイベント待ちだったから、どっちにしてもきつかったけど。",
            ),
            (
                "demo",
                "承知 — ファネルの分解分析は Sprint 3 に送る。これは実際のコストだから、黙ってリスケじゃなくて記録として残したい。",
            ),
            # --- 決定: 既存の決定を覆すのではなく、「条件を追加する」。
            # Q3 の施策は 5 本のまま。変わるのは、1 本として数えるために
            # 何が成り立っていなければならないか。
            (
                "bot0",
                "提案のフォーマットについてもう一点: 前四半期はコストを付けずに施策を並べたせいで、レビューが「そもそもこれ全部やれるのか」という議論になった。",
            ),
            (
                "demo",
                "確かに。提案に条件を追加する: 各施策に週単位のざっくりした工数と、それを実行するために「やめること」を書く。施策は 5 本のままでいい — ただ、トレードオフを明示していない施策は施策じゃなくて願望だから。",
            ),
            (
                "bot1",
                "この段階だとオーダー感の見積もりしか出せない。半スプリント、1 スプリント、複数スプリント、くらいの粒度。",
            ),
            (
                "demo",
                "その粒度でいい。ここで精度を出しても、どうせ嘘の精度になる。",
            ),
        ],
        "pm_thread": {
            "parent_index": 2,  # bot0's "notifications and search" message
            "messages": [
                (
                    "demo",
                    "実際の言い回しは？“見落としたものをアシスタントが教えてほしい” と言ってるなら、それはプロアクティブな提示で、検索とは別物。",
                ),
                (
                    "bot0",
                    "引用いくつか: “自分で検索したくない — 向こうから見つけてきてほしい” と “月曜でいちばんつらいのは、週末のあいだに何を見落としたか把握する作業”。",
                ),
                (
                    "demo",
                    "それなら完全にプロアクティブな提示の話だな。検索改善にまとめてしまわないように。",
                ),
                (
                    "bot0",
                    "了解。別テーマのまま維持して、成立するかどうかは分析まとめで見せる。",
                ),
            ],
        },
        # ---- Deliberately DISORGANIZED milestone. ----
        # Demo surface for the agent's "Organize tasks with AI" flow
        # (list_tasks + list_task_dependencies → ONE update_tasks_bulk):
        # priorities and due dates contradict the dependency order on
        # purpose — the plans/entitlements matrix gates almost everything
        # yet sits at Low priority with a nearly-last due date, tasks
        # downstream of it are due BEFORE it, one dependent is already
        # WIP while its blocker is untouched, and an independent cosmetic
        # task carries an inflated Critical. Keep the contradictions if
        # you edit — a tidy milestone gives the agent nothing to fix.
        # (Topic chosen for keyword disjointness from the eval suites:
        # billing/payment/invoice appear nowhere in cases.yaml /
        # retrieval_cases.yaml, so the extra rows can't shift retrieval
        # rankings.)
        "extra_milestones": [

            {
                "milestone": {
                    "title": "請求とプランの刷新",
                    "status": "Open",
                    "priority": "High",
                    "due_offset_days": 25,
                    "body": _body(
                        (
                            "🎯 ゴール",
                            [
                                "単一のフラットプランから、階層型のプラン＋上限モデルへ移行する。プランを定義し、使用量を計測して上限と突き合わせ、サポートへの問い合わせなしで顧客自身がプランを変更できるようにする。",
                            ],
                        ),
                        (
                            "⚠️ このマイルストーンの状態",
                            [
                                "価格のオフサイト直後に急いで書き出しただけのタスク群。優先度と期日は、依存関係の順序に照らしてまだ整理できていない。",
                            ],
                        ),
                    ),
                },
                "tasks": [
                    {
                        "key": "billing_entitlements_matrix",
                        "title": "請求プランとエンタイトルメントのマトリクスを定義",
                        "status": "Open",
                        "priority": "Low",
                        "assignee": "bot0",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 24,
                        "body": _body(
                            (
                                "🧾 概要",
                                [
                                    "プランの階層（Free / Pro / Max）、それぞれに紐づく機能と上限、アップグレード／ダウングレードのルールを決める。刷新の他のすべての作業が、このマトリクスを唯一の正として参照する。",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot1",
                                "これ、支払いサンドボックスをつなぐ前に終わらせるべきじゃない？まだ決まってないプラン名に対して組み込むのはしんどい。",
                            ),
                        ],
                    },
                    {
                        "key": "payment_provider_sandbox",
                        "title": "支払いプロバイダのサンドボックスを組み込む",
                        "status": "WIP",
                        "priority": "Critical",
                        "assignee": "bot1",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 6,
                        "body": _body(
                            (
                                "🧾 概要",
                                [
                                    "支払いプロバイダのサンドボックスを立ち上げる。チェックアウトセッション、支払いイベント用の Webhook、カスタマーポータルへのハンドオフまで。",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot1",
                                "土台の実装は始めた。ただエンタイトルメントのマトリクスが固まるまでは、推測のプラン名に対して書いてる状態。",
                            ),
                        ],
                    },
                    {
                        "key": "upgrade_downgrade_flow",
                        "title": "サブスクリプションのアップグレード・ダウングレードフローを実装",
                        "status": "Open",
                        "priority": "Minimal",
                        "assignee": "bot2",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 10,
                        "body": _body(
                            (
                                "🧾 概要",
                                [
                                    "アプリ内でプランを切り替えるフロー。日割り計算のプレビュー、確認ステップ、そして使用量が移行先プランの上限を超えている場合のダウングレードのガードレール。",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot0",
                                "これは権限マトリクスが承認されるまで一旦保留にします。各プランに実際に何が含まれるのかを決める前にプラン切替フローを作ると、二度作ることになる。",
                            ),
                            (
                                "bot2",
                                "それで構わないけど、何が解除条件なのかは明示しておきたい: マトリクスの承認と、上限を超えている状態でのダウングレードを許すかどうかの判断。後者はデザインの話ではなくプロダクトの判断です。",
                            ),
                            (
                                "bot0",
                                "確かに。それは暗黙のままにせず、マイルストーン側の条件として追加しておく。上限超過時のダウングレードは、放っておくとサポートの問い合わせで初めて気づく類の話だから。",
                            ),
                            (
                                "demo",
                                "保留に同意。ただ Minimal のままにして、放棄したように見えないようにしてください — 落としたのではなく順番をつけただけなので。",
                            ),
                        ],
                    },
                    {
                        "key": "usage_metering",
                        "title": "プラン上限のための使用量計測パイプライン",
                        "status": "Open",
                        "priority": "Normal",
                        "assignee": "bot1",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 4,
                        "body": _body(
                            (
                                "🧾 概要",
                                [
                                    "ワークスペースごと・日ごとに計測対象アクションを集計し、プラン上限のチェックから参照できるようにする。階層ごとに「どの」アクションが計測対象になるかを知るには、マトリクスが必要。",
                                ],
                            ),
                        ),
                        "comments": [],
                    },
                    {
                        "key": "billing_admin_dashboard",
                        "title": "サポート向けの請求管理ダッシュボード",
                        "status": "Open",
                        "priority": "High",
                        "assignee": "bot2",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 5,
                        "body": _body(
                            (
                                "🧾 概要",
                                [
                                    "サポート用の社内画面。顧客の現在のプラン、上限に対する使用量、直近の支払いイベント、そして手動でのプラン変更オーバーライド。",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot1",
                                "これが High なのは理由が違うと思う。サポートが強く要望したから High になっているだけで、ローンチに必要だからではない。最初の 1 ヶ月は DB クエリと手順書で回せる。",
                            ),
                            (
                                "demo",
                                "それはそうですが、その手順書はスキーマを知っている 3 人のことですよね。全員が寝ている最初の週に、顧客が二重課金されたらどうなりますか。",
                            ),
                            (
                                "bot1",
                                "なら、読み取り専用の半分がローンチのブロッカーで、手動オーバーライドはブロッカーじゃない。分けましょう: プラン・使用量・支払いイベントはローンチ、オーバーライドは後。そう切ってくれるなら反対はしません。",
                            ),
                            (
                                "demo",
                                "決まり — High のまま、オーバーライドは v1 のスコープ外と明記します。ボブ、次の人が戻さないようにタスクに書いておいてください。",
                            ),
                        ],
                    },
                    {
                        "key": "invoice_pdf_templates",
                        "title": "請求書 PDF と領収メールのテンプレート",
                        "status": "Open",
                        "priority": "Critical",
                        "assignee": "bot3",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 26,
                        "body": _body(
                            (
                                "🧾 概要",
                                [
                                    "ブランドを反映した請求書 PDF と領収メール。見た目だけの作業で他のトラックから独立しているので、ローンチ前ならいつ出してもいい。",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot0",
                                "なぜこれが Critical なんでしょう。説明には見た目だけの作業でいつでも出せると書いてある。何か設定を間違えていませんか。",
                            ),
                            (
                                "bot3",
                                "間違いではありません — 法務から、適格な請求書を発行せずに課金することはできないと回答がありました。見た目の作業に見えても、それでローンチのブロッカーになります。",
                            ),
                            (
                                "bot0",
                                "だとすると間違っているのは優先度ではなく説明文の方ですね。Critical のままにして概要を書き直します — 本文と優先度が矛盾しているタスクは、こうやって何度も議論をやり直す原因になるので。",
                            ),
                        ],
                    },
                ],
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# DM blueprints — three multi-turn conversations + one thread each.
# Sender keys: "demo" = the demo user, "bot" = the bot in this DM.
# ---------------------------------------------------------------------------

DM_BLUEPRINTS = [
    {
        "bot_index": 0,  # Alice (PM)
        "messages": [
            ("demo", "おはよう。ようやくこっちのセットアップ終わった。"),
            ("bot", "お、デモワークスペースへようこそ。何が用意されてるか、ざっと案内する？"),
            ("demo", "お願い。まずどこを見ればいい？"),
            (
                "bot",
                "サイドバーにプロジェクトが2つ。Web サイトリニューアル（実装中）と Q2 ロードマップ（計画中）。Sprint 1 はどっちも進行中。",
            ),
            ("demo", "了解。で、いま一番大きい未決の論点は？"),
            (
                "bot",
                "Q3 のロードマップの投資テーマを、プロアクティブな提示（見落としをアシスタントが教えてくれるやつ）にするか、検索の改善だけに絞るか。インタビューの感触はプロアクティブ寄りだけど、その前に文字起こしのコーディングをもっと進めたい。",
            ),
            ("demo", "分析まとめがどっちとも言えない結果だったら、どう判断する？"),
            (
                "bot",
                "小さい方の投資テーマ、つまり検索の改善に倒す。プロアクティブは Q4 のプロトタイプとして後追いでキューに入れる。根拠が弱いうちにコミットしすぎたくない。",
            ),
            ("demo", "なるほど。で、いまロードマップ提案をブロックしてるのは？"),
            (
                "bot",
                "分析まとめ。コーディングが 12 本中 4 本まで、目標は Sprint 1 の終わり。そこまで行けば1週間で下書きできる。",
            ),
            ("demo", "了解。遅れそうならすぐ言って、ブロッカー外すの手伝える。"),
            (
                "bot",
                "あと1つだけ。2つのプロジェクトでスプリントの区切りをどう揃えるか、まだ決めてない。リニューアルは週次で出していけるけど、ロードマップ側は調査だから1週間には収まらない。",
            ),
            ("demo", "どっちがいいと思う？"),
            (
                "bot",
                "リニューアルはスプリントの周期のまま、ロードマップ側はマイルストーンの日付で走らせたい。調査を無理にスプリントに押し込むと、毎週金曜に見せかけのキャリーオーバーが出るだけ。",
            ),
            (
                "demo",
                "賛成。スプリントの規律は効くところだけにして、あとはマイルストーンの日付で。",
            ),
            (
                "bot",
                "じゃあ終わってない調査チケットをスプリント間で動かすのはやめる。分析まとめが実際に終わるまで Q2 ディスカバリースプリントのマイルストーンに置いたままにする。",
            ),
            (
                "demo",
                "それでいこう。最後に1つ、ロードマップ提案の下書きができたら誰が承認する？",
            ),
            (
                "bot",
                "スコープはあなたと私、実現可能性は Bob。大きなレビュー会議にはしたくない。実際にそれを背負う3人で十分。",
            ),
        ],
        "thread": {
            "parent_index": 5,
            "messages": [
                (
                    "demo",
                    "「プロアクティブな提示」って、日次ダイジェストの話？それとも作業中に割り込んでくる系？",
                ),
                (
                    "bot",
                    "どっちかというと日次ダイジェスト寄りだと思う。インタビューでの言い方は「月曜の朝のキャッチアップ」で、「集中してるときに割り込んでほしい」じゃない。",
                ),
                (
                    "demo",
                    "OK、それならスコープはだいぶ切りやすい。通知疲れの懸念もかなり小さくなる。",
                ),
                (
                    "bot",
                    "そう。あと、もう出してる Spotlight の Cmd-K とうまく噛み合う。裏の検索は同じで、出し口が違うだけ。",
                ),
            ],
        },
    },
    {
        "bot_index": 1,  # Bob (Engineer)
        "messages": [
            ("demo", "ナビの実装、進んでる？"),
            (
                "bot",
                "共有。今日レスポンシブナビゲーションの PR 出す。ドロワーのアニメーションは純粋な CSS だけで組んだから、framer-motion を依存に抱えずに済む。",
            ),
            ("demo", "いいね。最終的に framer-motion がボツになった理由は？"),
            (
                "bot",
                "主にバンドルサイズ。マーケティングページに 40KB gzip は、合意したパフォーマンス予算に対して重すぎる。ドロワーは Web Animations API で十分いける。",
            ),
            ("demo", "今後、この判断を覆しそうなアニメーションの予定は他にある？"),
            (
                "bot",
                "モーダルのフェードとヒーローのスタガーアニメーション。どっちも CSS でいける。今週中にプロトタイプ出す。",
            ),
            ("demo", "アプリ内のアニメーションはどう？判断は別になる？"),
            (
                "bot",
                "たぶん別。マーケティングページにはパフォーマンス予算があるけど、アプリ側に同じ制約はない。書きやすさのメリットが実際に効くなら、アプリ内向けには framer-motion を再検討してもいい。",
            ),
            ("demo", "2つをごっちゃにしないように、それは別のスパイク調査として立てといて。"),
            (
                "bot",
                "了解。いまは Web サイトリニューアルのプロジェクトに入れとくけど、本来はアプリ側のプロジェクトができたらそっちに移すべきもの。",
            ),
            ("demo", "いい指摘。そのスパイク調査はローンチ後でいい、急がなくて大丈夫。"),
        ],
        "thread": {
            "parent_index": 3,
            "messages": [
                (
                    "demo",
                    "合意したパフォーマンス予算、具体的な数字は？ちゃんと守れてるか確認したい。",
                ),
                (
                    "bot",
                    "マーケティングページで Lighthouse モバイル ≥ 95、JS バンドル ≤ 120KB gzip。",
                ),
                (
                    "demo",
                    "なるほど。framer-motion の 40KB だけで、その 120KB 予算の3分の1を食うことになる。判断は簡単だね。",
                ),
                (
                    "bot",
                    "うん。CSS だけなら SSR も楽で、ハイドレーションの不一致も気にしなくていい。",
                ),
            ],
        },
    },
    {
        "bot_index": 2,  # Carol (Designer)
        "messages": [
            ("demo", "ヒーローのビジュアル、どう？"),
            (
                "bot",
                "ヒーローのイラスト、Figma に上げた。ファイルは「Marketing v3 hero」。グラデーション版と写真版の2パターンある。",
            ),
            ("demo", "どっち推し？"),
            (
                "bot",
                "グラデーション。写真は読み込みが遅いし、v1.1 で予定してる A/B テストの自由度も落ちる。",
            ),
            (
                "demo",
                "説得力ある。ホームページの作り直しが入る前に、他に知っておくべきことは？",
            ),
            (
                "bot",
                "セカンダリボタンのコントラストは v3.2 のトークンで 4.6:1、監査は通る。前の 3.8:1 はレイヤーの重ね方のバグで、トークンの問題じゃなかった。",
            ),
            ("demo", "把握。じゃあアクセシビリティ監査ではもう出てこないはず？"),
            (
                "bot",
                "そう。もう一つの Critical 違反、landmark role の欠落はマークアップ側の修正で、デザインの問題じゃない。Bob がナビゲーションの作り直しと一緒に対応してる。",
            ),
            ("demo", "そっち側でブロックされてることある？"),
            (
                "bot",
                "料金テーブルのコンポーネントの仕様がまだない。プランが何段階か＋トグルの挙動が決まらないと、レイアウトを確定できない。",
            ),
            (
                "demo",
                "マーケからは 3 プラン、月額／年額のトグルって聞いてる。文言は木曜までに渡す。",
            ),
        ],
        "thread": {
            "parent_index": 5,
            "messages": [
                ("demo", "待って、3.8 と 4.6 の差がトークンじゃなくてレイヤーの問題だったのはどういうこと？"),
                (
                    "bot",
                    "ホバー時にセカンダリボタンの上へ不透明度 50% のオーバーレイを重ねてて、それで実効コントラストが落ちてた。トークン自体はずっと問題なかった。",
                ),
                (
                    "demo",
                    "把握。なら、あのオーバーレイを復活させない限り大丈夫ってことか。デグレ防止に Storybook のストーリーを1本作っとく価値はある。",
                ),
                ("bot", "もう入れた。「SecondaryButton / Hover / Contrast」。"),
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Group chat (GM) blueprint — team-wide standup chatter + decisions.
# Sender keys reference indices into all_members (0=demo, 1..4 = bots).
# ---------------------------------------------------------------------------

GM_BLUEPRINT = {
    "messages": [
        (
            0,
            "おはよう — スタンドアップのスレッド立てた。自分の分はここに書くので、みんなも下に追加して。",
        ),
        (
            0,
            "ロードマップ提案のアウトラインを書いてる。顧客インタビューの分析まとめ待ちでブロックされてる（Alice）。",
        ),
        (
            1,
            "分析まとめは文字起こし 12 本中 4 本のコーディングが済んだところ。Sprint 1 の終わりには出せる見込み。インタビューがずれそうならすぐ知らせる。",
        ),
        (
            2,
            "レスポンシブナビゲーションの PR 出した。ドロワーのアニメーションは純粋な CSS だけ、framer-motion は使ってない。レビュー歓迎。",
        ),
        (
            3,
            "ヒーローのイラストは Figma に上げた — “Marketing v3 hero”。パフォーマンスと A/B の柔軟性を考えるとグラデーション版を推す。",
        ),
        (
            4,
            "アクセシビリティ監査を始めるには FeatureGrid の PR がマージされるのを待つ必要があって、そこでブロックされてる。その間にテスト計画を用意しておく。",
        ),
        (
            1,
            "あと — ここまでのインタビューの感触だと、プロアクティブな提示は思ってたより Q3 の投資テーマとして有望。コーディングが終わったらまとめて書く。",
        ),
        (
            0,
            "ありがとう。今週決めたいことが2つ: (1) ヒーローのどのバリアントで出すか、(2) プロアクティブな提示を Q3 のスコープに入れるか Q4 に回すか。",
        ),
        (
            3,
            "ヒーローについて — 強くグラデーション推し。バンドルサイズが小さいし、コピーの A/B テストもやりやすい。",
        ),
        (2, "グラデーションに賛成 — 写真アセットだとパフォーマンス予算を超える。"),
        (
            1,
            "プロアクティブな提示の件、文字起こしのコーディングが 8 本以上になるまで判断を待てない？4 本の感触でコミットするのは避けたい。",
        ),
        (
            0,
            "了解 — 判断は来週金曜に持ち越し。そこまでには分析まとめも文字起こし 10 本超えてるはず。",
        ),
        (
            4,
            "後回しにしてた質問 — ローンチに合わせてキーボードショートカットのドキュメントは作る？Spotlight の Cmd-K は普通に使えるけど、その存在をユーザーに伝えるものが何もない。",
        ),
        (
            0,
            "いい指摘、Dan。ローンチ向けに “What's new” モーダルを追加して、Cmd-K と今回のリニューアルを目立たせる。このスレッドからタスクを切り出しておく。",
        ),
        (
            0,
            "締める前に別の話 — モバイルの速度。バグが出るたびに個別で消火してるだけで、直近の iOS のレポートも最新の症状のひとつにすぎない。モバイルのパフォーマンス強化をちゃんとスコープを切ってやりたい。下のスレッドで — 形にしよう。",
        ),
    ],
    "thread": {
        "parent_index": 13,  # Dan's keyboard-shortcuts question
        "messages": [
            (
                1,
                "あと、Cmd-K はもう単なる画面移動じゃないってことも言っておきたい — AI 側はチャット・タスク・ノートを横断して質問に答える。そこが見落とされがち。",
            ),
            (
                4,
                "ほんとそれ！TechRising の知り合いにデモしたとき、スレッドを要約できることを全然知らなかった。",
            ),
            (
                0,
                "なるほど — なら “What's new” モーダルは AI 側を前面に出そう。Carol、Cmd-K のショートカットで回答がストリーミングで返ってくる様子を示す小さいイラスト、お願いできる？",
            ),
            (3, "了解 — 明日モックを作る。"),
            (2, "モーダルはローンチバナーと一緒に組み込める。PR 1本で両方の導線をカバーできる。"),
        ],
    },
    # ---- Plan-shaped thread. ----
    # Demo surface for the agent's plan-from-a-chat-thread flow
    # (create_task_plan): a proper CHAT thread (not task comments)
    # scoping a new workstream with an implied breakdown, ordering
    # ("first / after the pipeline"), owners, a deadline, and the home
    # project named explicitly so the agent can resolve it. Nothing in
    # it exists as tasks yet, so the created milestone is clearly new.
    # Keyword note: deliberately avoids "Safari" / "Lighthouse" /
    # "marketing site" so it can't outrank those retrieval-eval targets.
    "extra_threads": [
        {
            "parent_index": 14,  # demo's "mobile speed" scoping message
            "messages": [
                (
                    2,
                    "具体的には2つに分けたい: (1) 画像最適化パイプライン — レスポンシブなサイズ展開に加えて AVIF/WebP、ファーストビューより下は遅延読み込み。(2) ルート単位のコード分割で、ランディングページがアプリのコードを配信しないようにする。コード分割は画像パイプラインの後。順番が逆だと、動いてるベースラインに対して計測することになる。",
                ),
                (
                    3,
                    "その2つの前にサードパーティスクリプトの監査を入れたい — 直近のトレースだと、アナリティクスのタグとチャットウィジェットがメインスレッドの時間をかなり食ってる。先に監査をやれば、残り2つの作業自体が減る可能性もある。",
                ),
                (
                    4,
                    "何を出すにしても、CI で PR ごとに速度チェックを自動で回したい — ランディングページが遅くなったらビルドを落とす。自分が持てるけど、ゲートに使う数値を合わせる必要があるので、先に監視ダッシュボードができてることが前提。",
                ),
                (
                    2,
                    "監視ダッシュボードとアラートは自分が持てる。ただ画像パイプラインが先に終わってないと、グラフが既知の重いアセットに支配される。",
                ),
                (
                    1,
                    "担当はこうしよう: スクリプト監査は Carol、画像パイプラインと監視は Bob、CI ゲートは Dan。監査とパイプラインは優先度を高めに、残りは普通で — 期限は来月末を目標に。",
                ),
                (
                    0,
                    "完璧、筋が通ってる。これは Web サイトリニューアルのプロジェクトに入るもの — 誰か手が空いたら、このスレッドをタスク付きのマイルストーンにしてほしい。",
                ),
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Note blueprints — rich content that gives Spotlight AI substance.
# ---------------------------------------------------------------------------

NOTE_WELCOME_BODY = _body(
    (
        "👋 デモワークスペースへようこそ",
        [
            "ここは、このアプリを実際の仕事で使うとどう見えるのかを、データを入れた状態でまるごとお見せするサンプルです。プロジェクトが 2 つ、それぞれにマイルストーン、サブタスクまで入ったタスク、スレッド形式のチャット、そして書き下ろしのノートが数本入っています。ここにあるものは、すべて検索できます。",
        ],
    ),
    (
        "🧭 着手すべきところ",
        [
            (
                "bullet",
                "Cmd-K（Windows / Linux では Ctrl-K）を押して、Spotlight を開いてみてください。AI アシスタントが、このワークスペースのチャット・タスク・ノートを横断して検索します。",
            ),
            (
                "bullet",
                "タスクのタブを開くと、スプリントボード、マイルストーン、タスクごとにノートを書ける機能を試せます。",
            ),
            (
                "bullet",
                "チャットのサイドバーには、3 人のボット（Alice、Bob、Carol）との DM と、チーム共通の「全体」チャンネルが並んでいます。",
            ),
        ],
    ),
    (
        "🤖 こんな質問で Spotlight を試してみてください",
        [
            ("bullet", "「マーケティングサイトのパフォーマンス予算は？」"),
            ("bullet", "「framer-motion を見送ったのはなぜ？」"),
            ("bullet", "「これまでの顧客インタビューで見えてきたことをまとめて」"),
            ("bullet", "「ロードマップ提案のブロッカーは？」"),
        ],
    ),
    (
        "⏳ データの保存について",
        [
            "デモデータは、サインアウトすると自動的に削除されます。また、遅くともデモセッションの作成から 24 時間後には削除されます。ここにあるデータが、ほかのユーザーやチームとつながることはありません。",
        ],
    ),
)

NOTE_SPOTLIGHT_TIPS_BODY = _body(
    (
        "🤖 Spotlight (Cmd-K) を使いこなすコツ",
        [
            "Spotlight は、AI による検索と質問応答のオーバーレイです。名前をあいまい検索するだけのものではありません。このワークスペースのチャットメッセージ、タスクの説明、コメント、ノートをすべてインデックスしたうえで、ふだん話すような言葉のままの質問を LLM に渡し、実際に参照した出典を示しながら答えます。",
        ],
    ),
    (
        "✨ うまくいくパターン",
        [
            (
                "bullet",
                "意思決定の経緯を聞く：「framer-motion ではなく CSS アニメーションを選んだ理由は？」— Bob との DM と、スパイク調査のタスクを引いて答えます。",
            ),
            (
                "bullet",
                "いまの状況を聞く：「ホームページ再構築のブロッカーは？」— タスクのステータスとコメントスレッドを引いてきます。",
            ),
            (
                "bullet",
                "まとめてもらう：「顧客インタビューで見えてきたことをまとめて」— Alice との DM、PM チャンネルのスレッド、分析まとめのタスクを引いてきます。",
            ),
            (
                "bullet",
                "続けて聞く：回答ごとに会話の文脈が引き継がれるので、「では次のステップは？」と聞けば、その「次のステップ」が何を指しているかを踏まえて答えます。",
            ),
        ],
    ),
    (
        "🎯 うまくいかないパターン",
        [
            (
                "bullet",
                "このワークスペースの外のこと（ほかの会社、一般のウェブ、最近のニュースなど）を聞く — Spotlight が知っているのは、ここにある情報だけです。",
            ),
            (
                "bullet",
                "計算やコード生成など、「探して要約する」に収まらない依頼。",
            ),
        ],
    ),
    (
        "⌨️ ショートカット",
        [
            ("bullet", "Cmd-K / Ctrl-K — Spotlight を開きます。"),
            ("bullet", "Escape — 閉じます（会話の内容は残るので、あとから続けられます）。"),
            (
                "bullet",
                "Enter — 質問を AI に送信します。上に表示されるリアルタイムの検索結果は、矢印キーで移動できます。",
            ),
        ],
    ),
)

NOTE_WEEKLY_PRIORITIES_BODY = _body(
    (
        "🎯 今週の注力ポイント",
        [
            "優先順に3つ。それ以外は全部ノイズ。",
        ],
    ),
    (
        "1. レスポンシブナビゲーションを完了させる",
        [
            ("bullet", "PR #1284 のレビューを水曜までに。"),
            ("bullet", "320px ブレークポイントのリグレッションテストを Storybook に追加。"),
            ("bullet", "Drawer のフォーカストラップをスクリーンリーダーで検証（Dan）。"),
        ],
    ),
    (
        "2. 顧客インタビューの分析まとめのブロッカーを解消する",
        [
            ("bullet", "文字起こし2件でコーダー間の一致度を Alice とペアで確認。"),
            (
                "bullet",
                "残りのコーディングに入る前に「プロアクティブな提示 vs 検索」の切り分けを確定させる。",
            ),
        ],
    ),
    (
        "3. ロードマップ提案の骨組みを作る",
        [
            (
                "bullet",
                "根拠が仮置きでもいいので、先にドキュメントの骨格を作る。ゼロから書き始めるのではなく、セクションを埋めていく状態にしたい。",
            ),
            ("bullet", "ゴール: 3週間後に経営陣レビュー。"),
        ],
    ),
    (
        "⏸ 意図的に先送り",
        [
            ("bullet", "Q3 OKR のドラフト — ロードマップ提案待ち。"),
            ("bullet", "オンボーディングファネルの分解分析 — まず Plausible のイベント計測をリリースしてから。"),
            ("bullet", "アプリ内アニメーションフレームワークの選定 — ローンチ後。"),
        ],
    ),
)

NOTE_DESIGN_SYSTEM_INVENTORY_BODY = _body(
    (
        "📚 design system の棚卸し（v3.2）",
        [
            "@genos/design-system のどのプリミティブが実装済みで、どこで使われているかのスナップショット。今は手動メンテ — Storybook の docgen に取り込むのは Q3 の候補。",
        ],
    ),
    (
        "✅ 安定プリミティブ（どこでも利用可）",
        [
            ("bullet", "Stack, Grid, Box — レイアウトプリミティブ。コンテナクエリ対応。"),
            ("bullet", "Button, IconButton — セカンダリボタンのコントラストは v3.2 で修正済み。"),
            (
                "bullet",
                "Input, Textarea, Select — フォームプリミティブ。エラー / ヒント用のスロットを内蔵。",
            ),
            ("bullet", "Heading, Text — セマンティックなレベルを持つタイポグラフィ。"),
            ("bullet", "Modal, Drawer — フォーカストラップと escape 処理を内蔵。"),
        ],
    ),
    (
        "🚧 実験的（利用は自己責任で）",
        [
            (
                "bullet",
                "Hero — 存在はするが「media」プロパティがまだ流動的。依存するなら v3.2 に固定すること。",
            ),
            (
                "bullet",
                "FeatureGrid — iPad 横向きのコンテナクエリのブレークポイントが未確定。",
            ),
            (
                "bullet",
                "PricingTable — まだ存在しない。Carol がデザイン中で、仕様はマーケティング側のプラン階層の確定待ち。",
            ),
        ],
    ),
    (
        "📦 バンドルへの影響",
        [
            (
                "bullet",
                "design-system 全体を import すると gzip で約78KB（Bootstrap 4 は約52KB だが JS なし）。",
            ),
            ("bullet", "Tree-shaking は効く — Hero 単体なら約6KB。"),
            (
                "bullet",
                "コンテナクエリの polyfill は追加で約3KB。必要なブラウザにのみ読み込まれる。",
            ),
        ],
    ),
    (
        "🔗 詳しい情報の場所",
        [
            ("bullet", "Storybook: storybook.genos.app/design-system"),
            ("bullet", "トークンリファレンス: design-tokens.genos.app"),
            ("bullet", "Slack チャンネル: 質問は #design-system へ"),
        ],
    ),
)

TASK_NOTE_SYNTHESIS_METHODOLOGY_BODY = _body(
    (
        "📚 顧客インタビューの分析まとめ — 手法",
        [
            "今後の分析まとめでも同じやり方を再現できるよう、進め方を残しておく。この形に落ち着くまで前四半期に 2 周かかった。手間は先払いになるが、アウトプットの質は明確に上がる。",
        ],
    ),
    (
        "1. 文字起こしとクリーニング",
        [
            (
                "bullet",
                "Dovetail の自動文字起こしを使い、5 段落ごとに人手で確認する（訛りと発言の被りは精度が出ない）。",
            ),
            (
                "bullet",
                "フィラー（「えーと」「なんか」「まあ」）は落とす — テーマの検出を邪魔する。",
            ),
            (
                "bullet",
                "同意書に沿って匿名化する。会社名は業種に置き換える。",
            ),
        ],
    ),
    (
        "2. 4 軸のタクソノミーでコーディング",
        [
            ("bullet", "ジョブ — ユーザーは何を成し遂げようとしているのか？"),
            ("bullet", "ペイン — 今のワークフローのどこが苦しいのか？"),
            ("bullet", "ワークアラウンド — しのぐために何を自前で組んでいるのか？"),
            ("bullet", "決め手になる不満点 — どうなったら他製品に乗り換えるのか？"),
        ],
    ),
    (
        "3. クラスタリングと執筆",
        [
            ("bullet", "コードをテーマにまとめる — 認知負荷を抑えるため 8 個以下を目指す。"),
            (
                "bullet",
                "テーマごとに逐語引用 1 つと、根拠になった発言箇所の件数を付ける。",
            ),
            (
                "bullet",
                "カバレッジは明示する: エンタープライズ / SMB / プロシューマーの内訳。これがないと経営陣は声の大きいセグメントに引っ張られる。",
            ),
        ],
    ),
    (
        "4. コーダー間の一致度チェック",
        [
            (
                "bullet",
                "2 人のコーダーが文字起こし 2 件を独立にコーディングする。テーマの一致率が 80% を下回ったらタクソノミーを見直す。",
            ),
            (
                "bullet",
                "食い違いは記録する — 食い違い自体が、テーマが曖昧であることのシグナル。",
            ),
        ],
    ),
    (
        "📝 よくある落とし穴",
        [
            (
                "bullet",
                "テーマを早すぎる段階でまとめてしまう — 「通知 + 検索」は、根拠が示されるまでは 1 つではなく 2 つのテーマ。",
            ),
            (
                "bullet",
                "引用のつまみ食い — テーマには代表的な引用だけでなく、必ず件数を付ける。",
            ),
            (
                "bullet",
                "直近バイアス — 新しいインタビューから先にコーディングすると、その重みが過大になる。実施順にコーディングする。",
            ),
        ],
    ),
)

CHAT_NOTE_GM_KICKOFF_RECAP_BODY = _body(
    (
        "📝 スタンドアップ議事メモ — Sprint 1 キックオフ",
        [
            "チームのスタンドアップチャットから起こしたもの。あとでスレッドを全部読み返さなくても決定事項とブロッカーを見つけられるよう、ここに残しておく。",
        ],
    ),
    (
        "✅ 決定事項",
        [
            (
                "bullet",
                "ヒーローイラスト: グラデーション版で出す。理由はパフォーマンスが良いことと、v1.1 のコピーテストで A/B の自由度が高いこと。",
            ),
            (
                "bullet",
                "プロアクティブな提示 vs 検索: 判断は先送り。顧客インタビューの文字起こしを 8 本以上コーディングしてから再検討する（目標: 来週の金曜）。",
            ),
            (
                "bullet",
                "マーケティングページのアニメーションは CSS のみとし、framer-motion は使わないことに決定した。ドロワー、モーダルのフェード、ヒーローのスタガーアニメーションはすべて CSS アニメーションで実装し、ドロワーの中断可能なスライドだけ Web Animations API を使う。理由はバンドルサイズ: framer-motion は gzip 後 40KB で、JS 予算 120KB と Lighthouse モバイル 95 以上という目標に対して重すぎる。一方 WAAPI 版のプロトタイプは 800 バイトだった。",
            ),
            (
                "bullet",
                "「What's new」のローンチモーダル: やる。Cmd-K と今回のリニューアルを打ち出す。イラストは Carol、モーダルの実装は Bob。",
            ),
        ],
    ),
    (
        "🚧 ブロッカーとリスク",
        [
            (
                "bullet",
                "FeatureGrid の PR がマージされるまで Dan はアクセシビリティ監査に着手できない。遅延に注意。",
            ),
            (
                "bullet",
                "分析まとめはロードマップ提案のクリティカルパス。Sprint 1 末を過ぎると提案もそのままずれる。",
            ),
            (
                "bullet",
                "料金コンポーネントのデザインは、マーケティング側がプラン階層のコピーを木曜までに確定させることが前提。",
            ),
        ],
    ),
    (
        "📅 次のチェックポイント",
        [
            ("bullet", "金曜 — 週末のデモ。About ページ移行のウォークスルー。"),
            (
                "bullet",
                "来週の金曜 — プロアクティブな提示 vs 検索の判断（文字起こし 8 本以上をコーディング済みの状態で）。",
            ),
            (
                "bullet",
                "Sprint 1 末 — マーケティングの PR 群と Plausible イベントの PR がどちらもマージ済み。",
            ),
        ],
    ),
)


# ---------------------------------------------------------------------------
# Single-string labels — note titles, folder names, todo text, channel name.
#
# These are not bodies, so they never went through `_body`; they lived as
# literals at their use sites until the demo went bilingual. The English
# spellings of two of them are eval-pinned, but only against the EN table —
# the eval fixture seeds with no `lang`, so it never reads this module —
# which is why they are translated here and kept English there.
# ---------------------------------------------------------------------------

CHANNEL_TITLES = {
    # `{team_name}` is `.format()`-ed by the seeder with the generated team
    # name (`Demo Team <short>`), which is an identifier, not prose — it is
    # kept verbatim, and dropping or renaming the placeholder raises
    # KeyError at seed time. The label itself IS translated: nothing keys
    # off the literal `general`, and the welcome note names this channel in
    # Japanese, so the two must agree.
    "gm": "全体 · {team_name}",
}

NOTE_TITLES = {
    "folder_getting_started": "はじめに",
    "folder_product": "プロダクト",
    "folder_design": "デザインシステム",
    # `Spotlight` is a product name and stays Latin. The English spelling
    # of this note's title is pinned by `retrieval_cases.yaml`, but only
    # against the EN table: the eval fixture calls
    # `create_demo_environment` with no `lang`, so it never seeds this
    # module. Translating it fully is therefore safe, and a Japanese
    # visitor should not be greeted in English.
    "welcome": "デモワークスペースへようこそ",
    "spotlight_tips": "Spotlight (Cmd-K) を使いこなすコツ",
    "weekly_priorities": "今週の優先事項",
    "design_system_inventory": "デザインシステム棚卸し (v3.2)",
    "task_synthesis": "顧客インタビューの分析まとめ手法",
    "chat_kickoff_recap": "Sprint 1 キックオフの議事メモ",
}

# Todo content, keyed so `_create_todos` reads as a calendar layout rather
# than a wall of strings. `cat_*` are the three tag lanes; each day is a
# list of (key, title) with bodies under `notes`.
TODO_CONTENT = {
    "cat_web": "Web サイトリニューアル",
    "cat_q2": "Q2 ロードマップ",
    "cat_personal": "個人",
    "hero_parent": "ホームページのヒーローイラストのハンドオフを仕上げる",
    "hero_parent_notes": (
        "Carol が昨日ヒーローの SVG の v3 を上げてくれた。最終バリアントを決めて、"
        "BlockNote のイントロセクションに組み込み、alt テキストを Alice に確認する。"
    ),
    "hero_sub1": "Carol の v3 バッチから最終 SVG バリアントを選ぶ",
    "hero_sub2": "ヒーローを BlockNote のイントロセクションに組み込む",
    "spike_review": "framer-motion と CSS アニメーションのスパイク調査メモをレビューする",
    "spike_review_notes": (
        "Bob がスパイク調査のまとめをレスポンシブナビゲーションのスレッドに上げている。読んで推奨案を承認し、"
        "決定内容を Q2 ロードマップのドキュメントからリンクする。"
    ),
    "synthesize": "残り 3 件の顧客インタビューを Dovetail で分析まとめに落とす",
    "reply_alice": "Q3 の投資テーマのドラフトについて Alice に返信する",
    "reply_alice_notes": "「プロアクティブな提示 vs 検索」のどちらを取るか、yes/no で答えが欲しいらしい。",
    "offer_letter": "QA の新メンバーのオファーレターにサインする",
    "water": "今日が終わるまでに水を 2L 飲む",
    "storybook": "レスポンシブナビゲーションの 320px Storybook ストーリーを確認する",
    "perf_budget": "Bob とパフォーマンス予算のしきい値を承認する (Lighthouse 95 / JS 120KB)",
    "synthesis_outline": "顧客インタビュー分析まとめのアウトラインを書く",
    "spike_ticket": "アプリ内アニメーションフレームワークのスパイク調査チケットを作る",
    "spike_ticket_notes": (
        "アプリ内アニメーションの判断はローンチ後まで先送りする、と Bob と合意した。"
        "スパイク調査チケットではローンチ後の評価を追う。"
    ),
    "plausible": "マーケティングサイトに Plausible のイベント計測を設定する",
    "sprint_kickoff": "Alice と Bob とスプリントキックオフをやる",
    "review_milestones": "ロードマップのドキュメントで Q2 のマイルストーンを確認する",
}

# Self-DM "notes to self", oldest first. POSITIONAL: the seeder derives each
# message's `seq` from the tuple index, and `assert_content_parity` compares
# the count across tables — so never add, drop or reorder one.
SELF_DM_MESSAGES = (
    "ようこそ — ここはあなた専用のメモ用チャットです。思いついたこと、リンク、リマインダーを気軽に書き込んでください。"
    "ヘッダーの ✓ アイコンを押すと、To-Do リストの表示を切り替えられます"
    "（Spotlight のエージェントが `list_today_todos` で読むのと同じデータです）。",
    "Spotlight のコツ: Cmd-K を押して「今日の To-Do リストは？」と聞くと、右側の ToDoPane から読み取って答えます。"
    "「To-Do を追加する: …」と頼めば、承認をはさんだ書き込みも試せます。",
    "リマインダー — レスポンシブナビゲーションのマージを承認する前に、Bob の framer-motion と CSS のスパイク調査メモを読む。"
    "パフォーマンス予算の数値 (Lighthouse 95、JS 120KB) はレスポンシブナビゲーションのタスク本文にある。",
    "Q3 の投資テーマのドラフトは Alice のところ。「プロアクティブな提示 vs 検索」のどちらを取るか、EOW までに yes/no が欲しいらしい。"
    "顧客インタビューの分析まとめはどちらも支持できる内容なので、判断の分かれ目は根拠ではなく、どこに力を注ぐかだ。",
    "Plausible のダッシュボードの URL: イベントの仕様は 3 日前の To-Do "
    "*マーケティングサイトに Plausible のイベント計測を設定する* を見る。",
    "Carol のヒーローイラストの v3 が Figma に上がっている。最終バリアントを決めて、BlockNote のイントロセクションに組み込み、"
    "alt テキストを Alice に確認する。今日の To-Do のサブ項目で追いかけている。",
)

# Sprint display name; `{n}` is the 1-based sequence number.
#
# Deliberately Latin, unlike almost everything else in this module. The
# workspace's own prose cites these sprints by name 14 times across six
# surfaces — chat ("Sprint 1 はどっちも進行中"), task bodies, the standup
# recap note — and every one of those says `Sprint 1`, because that is how
# a Japanese engineering team writes it. A row in the sprint picker reading
# `スプリント 1` would be the only place in the workspace using the other
# spelling, so the visitor would see two names for one thing.
SPRINT_NAME = "Sprint {n}"

# PM task-card field labels. Taken from the shipped Japanese UI
# (`genos-frontend/src/i18n/locales/ja/index.ts`: ステータス / 優先度 /
# 労力 / 担当者 / 報告者 / 締切 / 未割り当て) rather than translated
# afresh, so a seeded card reads the same as the task pane next to it.
CARD_LABELS = {
    "status": "ステータス: ",
    "priority": "優先度: ",
    "effort": "労力: ",
    "assignee": "担当者: ",
    "reporter": "報告者: ",
    "due": "締切: ",
    "unassigned": "未割り当て",
}
