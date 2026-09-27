# 運用手順

## 起動と停止

同じ装置を **SiLA2（profile `sila2`）と LADS OPC UA（profile `lads`）** のどちらで配信するかを
compose の profile で選ぶ。`laboratory-model` は profile を持たず常に起動する。

```bash
docker compose --profile sila2 up -d                  # laboratory-model ＋ SiLA2 サーバー 5 台
docker compose --profile lads up -d                   # laboratory-model ＋ LADS サーバー 5 台
docker compose --profile sila2 --profile lads up -d   # 両方（同じ世界を共有）
docker compose --profile "*" down                     # 起動した profile に関係なく全部止める
docker compose --profile "*" ps
```

- **既定を決めたいときは `.env.example` を `.env` にコピーする**（中身は `COMPOSE_PROFILES=sila2`）。
  `.env` は git 管理外なので、チェックアウトごとに決めてよい。`.env` があれば素の `docker compose up -d` で
  SiLA2 構成が立つ（labcode の SiLA2 例の手順はこれを前提にしてよい）。シェルの `COMPOSE_PROFILES` や
  `--profile` は `.env` より優先される。
- **profile を何も指定しないと `laboratory-model` だけが起動する。** サーバーが立たないのはこのため。
- `down` / `ps` / `logs` は**有効な profile のサービスしか対象にしない**。起動時と違う profile で
  `down` すると止め残しが出るので、止めるときは `--profile "*"` を使う。
- 両方を同時に起動した場合、同時実行ガードはプロトコルごとに別なので、同じ装置を SiLA2 と LADS から
  同時に操作することは止められない。

各サーバーは `--insecure --verbose` で起動する。LADS 版の対応表と SiLA2 との差分は `docs/LADS_MAPPING.md`。

**以下の節のコマンドは、`.env`（または `COMPOSE_PROFILES`）で profile が決まっている前提で書いてある。**
決めていなければ各コマンドに `--profile sila2` などを付ける。

### ソースを変更したとき

```bash
docker compose build
docker compose up -d --force-recreate
```

> **`docker compose up -d --build` はソース変更を反映しないことがある。** 実際にコンテナ内の
> コードが古いままだった事例がある。確実に入れ替えるには上記のように `build` と `--force-recreate` を
> 明示する。反映されたかどうかは、たとえば次のように確認できる。
>
> ```bash
> docker compose exec sila2-server-1 python -c "import mock_instruments.centrifuge as m; print(m.__file__)"
> ```

## 起動確認

```bash
docker compose ps                              # 全サービスが Up
curl -sS http://localhost:8001/health          # {"status":"healthy"}
curl -sS http://localhost:8001/devices         # 宣言済み device の一覧
```

SiLA2 サーバー群への疎通は下記「直接接続の確認」で行う。

## 世界の状態を見る・戻す

`laboratory_model` は t=0 の世界を `config/laboratory_model.seed.yaml` から読む。**この API は運用者と
このリポジトリの `samples/` のためのものであり、ワークフロークライアントは触らない**
（`docs/RULES.md`「このリポジトリと利用側の関係」）。

```bash
curl -sS http://localhost:8001/state           # 全 device の spot と opaque state
curl -sS http://localhost:8001/devices/thermal-cycler/state
```

### シードとワークフローの boundary は同じ t=0 を記述している

**同期を保つ機構は無い。** ワークフロー側の boundary が入力に使う spot に、このシードが物を置いていなければ、
その run は最初の搬送で `source_empty` になる。**名前の対応はクライアントのスクリプトで翻訳できるが、
「物がそこにあるか」は翻訳では直らない** — 揃えるべきはこちらである（`docs/LABORATORY_MODEL.md`）。

シードは boundary と同じ形にしてあるので、正常系ではワークフローの boundary をそのまま写せる。
どちらかを編集したら、もう一方も見ること。

### run の合間に t=0 へ戻す

ワークフローを 1 回走らせると物は移動している。**次の run の前に世界を戻すのは運用者の仕事**である
（実機ラボで人がプレートを載せ直すのに対応する）。

```bash
curl -sS -X POST http://localhost:8001/reseed
```

`{"reseeded":true,"devices":6,"spots":7,"items":1}` のように、読み込んだ内容の要約が返る。
**シードファイルはディスクから読み直される**ので、run の合間に編集すればそれが効く（リビルド不要）。

コンテナを再起動しても同じ効果になる。

```bash
docker compose restart laboratory-model
```

### `/reset` と `/reseed` の違い

| 操作 | 効果 |
|---|---|
| `POST /reseed` | シードファイルを読み直して t=0 を再構築（トポロジ＋occupancy＋device state＋closed_spots） |
| `POST /reset` | **トポロジは維持**し、全 spot を空・accessible に、device state をクリア |

`/reset` は「空の世界」であって「無い世界」ではない。トポロジを消すと全 location が未知になり何も動かない。
`samples/` は各スクリプトの冒頭で `/reset` を使って白紙化している。

## コマンド所要時間のプロファイル切り替え

所要時間はビルド時にイメージへ焼き込まれる。**既定はどのコマンドも待たない**（既定プロファイルは空）。

```bash
# labcode のポーリング周期で遷移が観測できる秒〜数十秒のプロファイルへ
DURATIONS_FILE=command_durations.realistic.yaml docker compose build
docker compose up -d --force-recreate

# 既定へ戻す
docker compose build
docker compose up -d --force-recreate
```

焼き込まれた内容はコンテナ内で確認できる。

```bash
docker compose exec sila2-server-1 cat /app/command_durations.json
```

**realistic プロファイルでは `samples/` に `--timeout 120` を渡す**。既定の 10 秒では
`SpinCycle`（20 秒）などが必ずタイムアウトする。詳細は `docs/TIMING.md`。

## Ardea の station マップを変える

Ardea は station 名（`Base1`..`Base6`）で呼ばれ、環境変数 `ARDEA_STATIONS` が名前 → location を決める
（対応表と設計は `docs/SERVERS.md`）。**環境変数なのでリビルドは不要**。

```bash
# docker-compose.yml の ardea-server-1 の ARDEA_STATIONS を編集してから
docker compose --profile sila2 up -d --force-recreate ardea-server-1   # LADS 版は ardea-lads-server-1
docker compose logs --tail=20 ardea-server-1     # 不正なら起動時に落ちる
```

**右辺は seed が宣言した spot でなければならない**。マップと seed の両方を触ったときは、必ず
「直接接続の確認」を回すこと（`unknown_location` は使った瞬間に初めて出る）。

現在のマップはサーバーに聞ける。

```bash
uv run python -c "from sila2.client import SilaClient; print(SilaClient('127.0.0.1', 50057, insecure=True).CarriageService.StationNames.get())"
```

## 単体テスト

Docker を使わずコンポーネント単体を検証する pytest スイート。リポジトリルートで実行する。

```bash
uv run pytest
```

対象は 7 コンポーネント。

| 対象 | 内容 |
|---|---|
| `laboratory_model/tests/` | 世界モデルの規則・HTTP 契約・シード |
| `laboratory-client/tests/` | HTTP 転送層・環境変数からの設定読み取り |
| `instruments/tests/` | 装置の振る舞い（実行開始点・Status 遷移・エラー種別とメッセージ・世界への作用・待ち時間）と Ardea の station map |
| `protocols/lads/lads_common/tests/` | LADS 共通部品。スレッドと asyncio の橋渡し、および**実際の asyncua サーバーとクライアントを同一プロセスで立てて**、Program・FunctionalUnitState・StatusCode・TargetValue 書き込みの対応を確認する（NodeSet の読み込みで 1 サーバー約 6 秒かかるため、スイート全体の所要時間の大半はここ） |
| `protocols/lads/servers/tests/` | LADS 版 5 台（4 装置と Ardea）の実際の `build()` を 1 つの asyncua サーバーに載せ、公開する Program・property 名・Function・vendor 変数と、装置の規則（範囲・順序・thermal cycler の Stop まで Running・Ardea の未知 station）を確認する |
| `tools/tests/` | 所要時間の切り出し、**設定ファイルと装置実装の齟齬検出**、各サーバーの Dockerfile が自分の device を切り出しているか |
| `protocols/sila2/servers/ardea_server/tests/` | `protocols/sila2/specs/` の Feature 定義と**実際に配信される定義**の整合 |

in-process で動くので compose スタックの起動は不要。方針は `docs/RULES.md`「テスト方針」。

サーバー側のテストを追加する場合も同じ方針で `protocols/sila2/servers/<name>/tests/` に置き、ルート
`[tool.pytest.ini_options]` の `testpaths` に 1 行追加する（**そのテストが対象を import する場合だけ**
`pythonpath` にも足す）。現在は `protocols/sila2/servers/ardea_server/tests/` があり、Feature 定義をファイルとして
読むだけなので `testpaths` のみである。

## 静的チェック

```bash
uv run ruff check .
uv run ruff check --fix .
uv run mypy
```

設定はルートの `pyproject.toml`。`generated/` と各サーバーの `__main__.py` は生成コードなので対象外。
方針は `docs/RULES.md`「静的チェック方針」。

## CI

`.github/workflows/ci.yml` が push と PR で発火し、`test`（pytest）・`lint`（ruff）・`typecheck`（mypy）の
3 ジョブを回す。各ジョブは uv でインタプリタと依存を用意し、手元と同じコマンドをそのまま実行する。

**`samples/` は CI の対象外**。compose スタックを立てた実サービスに対する確認であり、手元運用に留める。

## 直接接続の確認

`sila-python` で各 SiLA2 サーバーへ直接接続する確認スクリプト。実サービスに対する確認であり、
単体テストとは目的が異なる（前者は規則、こちらは配備）。**失敗時は非ゼロ終了する。**

```bash
uv run python samples/run_all_smoke_tests.py     # SiLA2 の 5 台まとめて
uv run python samples/laboratory_model_smoke.py  # 世界モデル単体
uv run python samples/run_roundabout.py          # 装置を一周する統合確認（SiLA2）
```

正常時は `All smoke tests passed.` / `Laboratory model smoke test passed.` /
`Roundabout workflow passed.` と表示される。

LADS 版（profile `lads` で起動しておく）:

```bash
uv run python samples/run_all_smoke_tests.py --protocol lads   # LADS の 5 台まとめて（both で両方）
uv run python samples/run_lads_roundabout.py                    # 同じ一周を LADS で
```

**parity 検査**（両 profile を起動しておく）: 同じ一周を SiLA2 → LADS の順に流し、一周後の世界、
装置の値の変化、同じ失敗に対する理由の文言が一致することを確かめる。

```bash
uv run python samples/run_parity.py
```

**sample を並列に走らせてはいけない。** 各 sample は冒頭で世界をリセットするので、互いの準備を消し合う。

これらは**世界を破壊する**（冒頭で `/reset` する）ので、ワークフロー実行中のスタックに対しては走らせない。

realistic プロファイルでは `Transfer` が 30 秒かかるので、既定の 10 秒上限を超える。`--timeout 120` を付ける。

## Ardea の Feature 定義を実機と突き合わせる

Ardea は実在機器のモックであり、配信する Feature 定義が実機と同一であることが存在理由である
（`docs/SERVERS.md`）。**この同一性だけは自動テストで守れない**——実機リポジトリが手元に無いと比較できないため。
単体テストが見ているのは `protocols/sila2/specs/` と配信版が互いに整合しているかどうかまでである。

したがって**実機側（`ardea-sila2`）が更新されたときは手で突き合わせる**。実機の submodule は
`ardea-sila2` 側にあり、このリポジトリには無い:

```bash
cd ../ardea-sila2
git submodule update --init --recursive     # 初回のみ
sha256sum ardea_sila2/generated/*/*.sila.xml third_party/*/*/generated/*/*.sila.xml
cd -
sha256sum protocols/sila2/servers/ardea_server/ardea_server/generated/*/*.sila.xml
```

9 本のハッシュが一致していればよい。差分があれば**ソース XML（`protocols/sila2/specs/ardea_server/`）と生成物の両方を
コピーし直し**、`uv run pytest` と上記の直接接続確認を回す。実機のどのブランチから取ったかは
`protocols/sila2/specs/ardea_server/README.md` に記録してある。

## ログ確認

```bash
docker compose logs --tail=200
docker compose logs --tail=200 sila2-server-1
```

各サーバーの feature 実装はコマンド入口で `INFO` ログを出すので、`SpinCycle called` のような行で
呼び出しを追える。

## 注意事項

- ホストからは公開ポート `50052`〜`50055` と `50057` で各 SiLA2 サーバーへ、`8001` で `laboratory_model` へアクセスする。**50056 は空き**（station にサーバーが無いため）。
- コンテナ間はサービスのコンテナ名と**内部ポート** `50052`（SiLA2）／`8001`（laboratory-model）を使う。
  ホスト公開ポート `50053`〜`50055` と `50057` はホストからのアクセス用で、コンテナ内部からそのまま使う前提ではない。
- **Git Bash から `docker compose exec` にコンテナ内の絶対パスを渡すとパスが変換される**
  （`/app/...` が `C:/Program Files/Git/app/...` になる）。PowerShell を使うか `MSYS_NO_PATHCONV=1` を付ける。
