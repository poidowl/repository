# 純粋 LLM リポジトリ標本: `poidowl/kai`

ヒトが書いたコード・データが **0 行**（複数 LLM がコード・台本・設定・合議ログ・運用書類を 100% 生成・編集）の実稼働リポジトリを、[keeper.py](../keeper.py) および構造・検証監査で実測した記録。

## 1. 標本諸元

| 項目 | 値 |
| :--- | :--- |
| 対象リポジトリ | `poidowl/kai` |
| 計測コミット | `8da1e0c53faff0a9afde9f88fdcacbefa53f14cb` (`2026-11-27 16:18:52 +0900`) |
| ヒトが書いたコード・データ | **0 行** |
| 追跡ファイル総数 (`tracked_files`) | **3,149**（`.md`: 1,042 / `.py`: 853 / `.txt`: 584 / `.json`: 305 / `.html`: 139 / `.js`: 110 / `.mjs`: 30 / 他: 86） |
| コードファイル総数 | **1,132**（非テスト **868** ファイル・**212,691** 行 / テスト **264** ファイル） |

## 2. `keeper.py diagnose` 実測比較

| 指標 (`BUDGET_KEYS`) | `poidowl/repository` | `poidowl/kai` | 意味 |
| :--- | ---: | ---: | :--- |
| `scc_count` | 0 | **6** | 循環依存クラスタ（強連結成分）数 |
| `scc_max_size` | 0 | **17** | 最大の循環依存塊のファイル数 |
| `scc_node_count` | 0 | **40** | 循環依存に巻き込まれた総ファイル数 |
| `hidden_import_count` | 0 | **642** | 関数内 `import` / 動的 `import()` 箇所数 |
| `sys_path_files` | 0 | **168** | `sys.path` を直接書き換えるファイル数 |
| `duplicate_exact_groups` | 0 | **7** | 50 行以上の完全一致コピー組数 |
| `duplicate_name_groups` | 0 | **21** | 50 行以上の同名ファイル並存組数（うち 14 組は内容分岐） |
| `code_in_docs_files` | 0 | **21** | `docs/`・`doc/` 配下に置かれた非テスト実行コード数 |
| `sprawl_dirs_count` | 0 | **3** | 直下ファイル数 80 以上のコードディレクトリ数（最大 **239**） |
| `stamped_code_files` | 0 | **149** | `_MMDD`・`_v2`・`_tameshi` 等の接尾辞付き非テストコード数 |
| `large_files_count` | 0 | **26** | 1,000 行以上の巨大ファイル数（最大 **14,916** 行） |
| `large_funcs_count` | 0 | **33** | 150 行以上の巨大関数数 |

## 3. 実測された問題点（LLM 特有の 6 病理）

### 3.1 正本パスの漂流と同名ファイルの分岐
- `AGENTS.md` と `slop/station/README.md` は `slop/station/` 直下を本番（Mac `honmaru/`）の正規写し、`slop/station/mac/` を過去の控え棚と宣言するが、50 行以上の非テストコードだけで **21 組**の同名ファイルが複数箇所に並存し、うち **14 組**はハッシュが分岐している。
  - 内容分岐の例: `slop/station/tools/yt_chat_mac.py`（1,442 行）vs `slop/station/mac/tools/yt_chat_mac.py`（1,406 行）、`slop/station/akapen.py`（1,218 行）vs `slop/station/deryou/akapen.py`（1,240 行）、`slop/station/gouin.py`（541 行）vs `slop/station/mac/gouin.py`（534 行）、`slop/station/yowami.py`（295 行）vs `slop/station/mac/yowami.py`（276 行）。
  - 完全重複の例: `slop/station/te.py` と `slop/station/mac/te/te.py`（各 1,106 行）、`slop/station/te/short/short_deryou.py` と `slop/station/mac/te/short/short_deryou.py`（各 394 行）。
- 控え棚と定義された `slop/station/mac/te/te.py` の方が被参照 **18** 件（`slop/station/te.py` は **2** 件）を持ち、`slop/station/mac/web/ita_fuda.js`・`slop/station/mac/te/me.py` は `slop/station/mac/` 側にしか存在しない。
- 逆に `slop/station/docs/ARCHITECTURE.md` が現行中核として記載する `hon.py`・`make_short_swap.py` はリポジトリ内に **0 件**であり、設計書のパス記述と実ツリーが乖離している。

### 3.2 `docs/` 内への実行コード配置と既存実装の再発明
- ドキュメント置き場 `docs/neon_v2/sugata/` の中に非テスト実行コード **21** ファイル（テスト込み **31** コードファイル、設定・データ込み **43** ファイル）が置かれ、本番側 `slop/station/jiritsu/sugata_lib.py` から `subprocess` で直接実行される逆転構造になっている。
- 既存の ES Module キャラ描画（`docs/neon_v2/sugata/chara.mjs` + `sugata.mjs`）があるにもかかわらず、同じキャラ描画がグローバル JS の `slop/station/web/robot_sugata.js` として一から書き直されている。`nukumi_core.js` も `slop/station/web/nukumi_opus/nukumi_core.js` と `slop/station/web/kinds/kit/nukumi_core.js` に分岐して並存する。

### 3.3 フラットディレクトリ肥大と日付・版番サフィックス増殖
- `git` 履歴で既存ファイルを更新する代わりに別名ファイルを新規追加するため、非テストコード **149** ファイルが接尾辞付きで堆積している（`_MMDD` 日付付き **133** 件、`_tameshi` **8** 件、`_v2`〜`_v6` **6** 件、`_mihon` **2** 件）。
- 単一ディレクトリ直下のファイル数が `slop/station` **239**（`.py` 167 + 作業報告 `.md` 64 等）、`slop/station/tools` **171**、`slop/station/web` **141** に膨張している一方、しまい場 `slop/kura/` は **7** ファイルしか使われていない。
- サーバー側で `/kasane` が `204` 空応答に切り替えられた後も、`slop/station/web/kasane.js` が削除されず残存している。

### 3.4 関数内遅延 `import` と `sys.path` 書き換えによる循環依存の隠蔽
- パッケージ境界を作らず `sys.path` を直接書き換える非テストコードが **168** ファイル、トップレベル循環エラーを回避するための関数内 `import` / 動的 `import()` が **642** 箇所ある。
- 結果として依存の輪っかが解消されず潜在化し、`slop/station/chaos_lib.py`・`skit_lib.py`・`fan_slot.py`・`shikai.py` 等の **17** ファイル塊を筆頭に **6** クラスタ・計 **40** ファイルの強連結成分と、`slop/station/web/sekai_danbou.js`（**14,916** 行）・`slop/station/kome_mondo.py`（**8,616** 行）等の巨大ファイル群を生んでいる。

### 3.5 多世代・異種ドメイン同居と一時パス `/tmp/` 漏出
- 合議ログ（`inbox/`・`sessions/`）、本番局コード（`slop/`）、短尺動画素材 **854** ファイル（`shorts/`）、および自律配信の 3 世代（`haishin/`、`zerobase/`、`slop/station/jiritsu/` + `docs/neon_v2/`）が単一リポジトリに同居している。
- 作業セッションの一時ディレクトリやホスト固有絶対パスがそのままコミットされており、非テストコード **24** ファイルに `/Users/`・`/home/`・`C:\` の直書きがあるほか、`docs/neon_v2/sugata/tools/check_demo_page.mjs`（`import { chromium } from "/tmp/va/node_modules/playwright/index.mjs"`）や `docs/neon_v2/sugata/check/at.mjs`（`/tmp/sugata/...`）、`docs/neon_v2/sugata/tokiko_vis.mjs`・`butai.mjs` の `/tmp/tee_avatar/kuda_nama.mjs` フォールバックにより、クリーン環境で `tokiko_vis.test.mjs`・`portrait_bouzu.test.mjs` が `ENOENT` で落ちる。テスト入口も `tools/ci_check.py` と `tools/run_all_tests.sh` に分裂している。

### 3.6 既存検証道具の放置とモック単体テストによる描画盲目
- 「配信画面に映してみるまで描画してるものの問題が見えない」事象の実測原因：リポジトリ内には既にオフエアで絵を数値検査する道具が一式存在していた。
  - `docs/neon_v2/sugata/raster.mjs` (`rasterize`: SVG→PNG ラスタライズ)
  - `slop/station/mac/te/me.py` (`png_alpha_hako`: PNG 不透明画素の外接矩形計測)
  - `slop/station/web/layout2.js` (`GEO.A.robot` 枠定義・`?l2dbg` 枠線オーバレイ)
  - `slop/station/mac/web/ita_fuda.js` (`window.__ITA_FUDA`: DOM はみ出し・重なり検査)
  - `slop/station/web/mirror/robot2d.html` (`color-scheme:dark` による透過 iframe 合成)
- しかし新規追加された `slop/station/web/robot_sugata.js` のテスト（`slop/station/jiritsu/tests/test_sugata_live_step1_html.py` 等）は、ピクセルを一切描かない空モック `fakeCtx` のメソッド呼び出し確認のみで書かれていた。
- そのため `slop/station/web/robot_sugata.js` の Canvas2D 腕回転角の符号反転（`ctx.rotate((deg * Math.PI) / 180)` により両腕が胴体裏へ折れ込み、`png_alpha_hako` 実測幅が頭幅と同じ `208px` のまま腕の張り出しが `0px` になる不具合）が、配信画面に映すまで検出されなかった。新しい道具を書き足さず既存道具を既存テストに繋ぐだけでオフエア検出・解消できた（`poidowl/kai` PR #997）。
