# repository

> ヒトがプログラミングやコードを書いていた時代はいざ知らず、LLM がコードもドキュメントも次々書き込む今の時代に、リポジトリがどんな状態であるべきかの見本であり続ける。
>
> このリポジトリ自体が成果物。ゆえにリポジトリ名が `repository`。
>
> リポジトリ of the リポジトリ、by the リポジトリ、for the リポジトリ。

## 原則

1. **姿で語る** — 説明書を積まず、最小構成と実測の数字そのもので見本を示す。
2. **自らを最初に縛る** — このリポジトリ自身が [keeper.py](keeper.py) の天井（[keeper_budget.json](keeper_budget.json)）を常に満たし、テストのたびに自分で自分を検問する。
3. **診て処方し、壊さない** — [keeper.py](keeper.py) は読み取り専用。直すのは対象リポジトリの大工の仕事。
4. **同じ物差し、ゼロ依存** — Python / JS / MJS / TS / HTML を共通の指標で測る。Python 標準ライブラリのみ。

## コマンド

```bash
# 1. 診断（依存の輪っか・隠れ読み込み・sys.path・コピー重複・巨大ファイル/関数）
python3 keeper.py diagnose /path/to/repo
python3 keeper.py diagnose /path/to/repo --json

# 2. 処方（切断すると輪っかが最もほどける import 辺の順位・重複の正本候補）
python3 keeper.py prescribe /path/to/repo --top 10

# 3. 予防（現状の数値を天井として保存し、悪化したら exit 1 で止める）
python3 keeper.py budget init /path/to/repo -o /path/to/repo/keeper_budget.json
python3 keeper.py budget check /path/to/repo -b /path/to/repo/keeper_budget.json

# 改善後に天井を下げる（ratchet）
python3 keeper.py budget init /path/to/repo -o /path/to/repo/keeper_budget.json --ratchet
```

## 相手リポジトリへの導入（最小 CI）

`keeper.py` と `keeper_budget.json` を置いて、ワークフローで 1 行実行するだけ：

```yaml
- run: python3 keeper.py budget check .
```

## テスト（セルフ検問を含む）

```bash
python3 -m unittest -v test_keeper.py
```
