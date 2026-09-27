# プロジェクト概要

このリポジトリは、**モック SiLA2 機器サーバー群と、それらが共有する世界状態サービス `laboratory_model` から成る
仮想ラボ**である。用途は、ワークフロー実行系（labcode 等）を実機の前に、実機と同じ SiLA2 インターフェイスで
検証すること。

## 依存の向き

**依存は一方向**（利用側 → このリポジトリ）。**このリポジトリは利用側を一切知らない。**
利用側が依存するのは「一般的な SiLA2 サービスが複数立ち上がっている」という点のみである。
`laboratory_model` は実機ラボの物理世界に相当し、**ワークフロークライアントは触ってはならない**
（実機に対応するインターフェイスが存在しないため）。詳細は `docs/RULES.md`「このリポジトリと利用側の関係」。

## 構成

| ディレクトリ | 内容 |
|---|---|
| `protocols/sila2/servers/` | モック SiLA2 サーバー 5 台（Microplate Centrifuge / PlateLoc / Automated Plate Seal Remover / Automated Thermal Cycler の 4 装置＋搬送役 Ardea）。生成コードと、`instruments/` へ写すだけの薄い feature 実装（adapter）。**Ardea だけは実在する機器のモック**で、実機の Feature 定義 9 本をそのまま配信する（`docs/SERVERS.md`）。**station（ワークフローの入口・出口）にサーバーは無い** — ラックには commandable なものが無く、seed が spot を宣言すれば足りる |
| `instruments/` | **プロトコル非依存の装置の振る舞い**（パッケージ `mock_instruments`）。状態・規則・所要時間・Status 遷移・世界への作用を 1 か所に持ち、SiLA2 版と LADS OPC UA 版（導入中）の両方がこれを呼ぶ（`docs/RULES.md`「プロトコルと装置の振る舞いの分離」） |
| `protocols/lads/lads_common/` | LADS OPC UA サーバーの共通部品（導入中）。LADS 1.0.0 と依存 NodeSet の同梱・型の実体化・状態機械・装置コマンドをスレッドで走らせる橋渡し・FunctionalUnit（Program と Result）・Function・StatusCode への対応づけ・サーバー起動。**FunctionalUnitState は独自の規則を持たず、装置の Status から導く** |
| `protocols/lads/servers/` | LADS OPC UA サーバー 5 台（centrifuge / PlateLoc / seal remover / thermal cycler / Ardea）。各パッケージは装置のどのコマンドを LADS のどのノードとして見せるかだけを決める。compose では `lads` profile 付きの `lads-server-1..4` と `ardea-lads-server-1`（ホスト 4841〜4844・4847） |
| `laboratory_model/` | 共有の世界状態サービス |
| `laboratory-client/` | サーバーが世界モデルに到達するための共有パッケージ（HTTP 転送層と環境変数からの設定読み取り）。**世界の意味づけは共有せず各サーバーに残す** |
| `config/` | 世界のシードと、コマンド所要時間のプロファイル |
| `tools/` | ビルド時ヘルパ（所要時間の切り出し） |
| `samples/` | 実サービスに直接接続する確認スクリプト |
| `protocols/sila2/specs/` | 各サーバーの SiLA Feature 定義 XML（**編集しない**。実機と同一でなければ drop-in 置換テストにならない） |
| `external/` | 直接の開発対象ではない参考用の外部実装 |

ローカル実行の基本形は `docker-compose.yml`。手順は `docs/OPERATIONS.md`。

## 世界モデル

**device 中心**の世界で、各 device が固定の **spot** 集合（item の有無・access 状態）と、
**無解釈の state**（key-value）を持つ。location は常に `device.spot`。
**トポロジはシードが宣言し、未宣言の場所への操作は 404 になる** — ワークフローが物理的に存在しない場所へ
物を動かそうとしたとき、その場で誤りが露見するようにするため。詳細は `docs/LABORATORY_MODEL.md`。

## コマンド所要時間

各モックコマンドが何秒かかるかは、実装中のリテラルではなく**設定**として持つ。
`config/command_durations.yaml` にラボ全体を記述し、ビルド時に device ごとに切り出してイメージへ焼き込む。
既定プロファイルは待ち時間ゼロ（`samples/` が速いまま保たれる）、realistic プロファイルは
ポーリングで状態遷移が観測できる秒〜数十秒。1 サーバーで 2 つのコマンドが同時に実行されることは拒否される。
詳細は `docs/TIMING.md`。

## テスト

- **単体テスト**（`uv run pytest`）= docker 非依存・CI 対象。`laboratory_model` / `laboratory-client` / `tools` を対象とする。
- **`samples/`** = compose の実サービス相手・手元運用・CI 非対象。失敗時は非ゼロ終了する。
  単体の smoke に加えて `samples/run_roundabout.py` が `station.slot1` から各装置を一周して戻る統合確認を行う。

方針は `docs/RULES.md`「テスト方針」。

## ログ

各 SiLA2 サーバーは `--verbose` 付きで起動し、コマンド呼び出し時の `INFO` ログを `docker compose logs` で確認できる。
