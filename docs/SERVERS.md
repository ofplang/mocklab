# サーバー実装メモ

## 概要

`sila2/servers/` 配下の各 SiLA2 サーバーは、生成コードを土台にした薄い feature 実装（adapter）を持ち、装置の振る舞いそのものは `instruments/`（`mock_instruments`）に置く（`docs/RULES.md`「プロトコルと装置の振る舞いの分離」）。実機制御そのものよりも、
**実機と同一の Feature 定義を保ったまま**、SiLA2 経由の接続・コマンド実行・状態遷移を確認しやすくすることを狙う。

**`sila2/specs/` の Feature XML は編集しない。** 実機と同一でなければ drop-in 置換テストにならないため、
多 spot 化などの改修もコマンド署名と Feature を変えずサーバー内部で行う。

## Ardea サーバー（実在機器のモック）

`sila2/servers/ardea_server/` は**実在する機器 Ardea のモック**であり、他の 4 台とは性格が違う。実機の
`ardea-sila2` は DENSO ロボット（ORiN b-CAP）と KEYENCE PLC（KV COM+）を同時に駆動し、
**Feature を 9 本公開する**。モックもその 9 本を配信する。

| Feature | 出自 | モックの実装 |
|---|---|---|
| `LabwareService` | Ardea 固有 | **`Transfer` を実装**。`LightIsOn` を実装。他 5 コマンドは未実装 |
| `CarriageService` | Ardea 固有 | `StationNames` / `CarriagePosition` を実装。`MoveCarriage` は未実装 |
| `RobotPoseService` | Ardea 固有 | 全 4 コマンド未実装 |
| `RobotOrientationService` | Ardea 固有 | 全 2 コマンド未実装 |
| `VariableService` / `TaskService` / `RobotService` | `bcap-sila2`（b-CAP プロバイダ） | 全コマンド未実装 |
| `DeviceService` / `ConnectionService` | `kvcomplus-sila2`（KV COM+ プロバイダ） | 全コマンド未実装 |

**Feature 定義の 2 つのコピー。** `sila2/specs/ardea_server/` にあるのは**ソース** XML（実機リポジトリからの
コピー・読むためのもの）。実際に配信されるのは `sila2/servers/ardea_server/ardea_server/generated/<feature>/`
にある codegen 正規化版で、これも実機の生成物からコピーしたものなので**実機が配信するバイト列と同一**である。
両者が同じ Feature を表しているかは `sila2/servers/ardea_server/tests/test_feature_definitions.py` が検査する。
**生成コードは再生成せずコピーする**（それが同一性を構造的に保証する唯一の方法）。出典と手順は
`sila2/specs/ardea_server/README.md`。

**未実装コマンドは `NotImplementedError`** を投げる。クライアントには
`UndefinedExecutionError: Method is not implemented by the server` が届く（**この文言は sila2 が用意するもので、
実装側のメッセージは破棄される**）。理由の説明はサーバーログに `INFO` で出るので、
`docker compose logs ardea-server-1` で読む。declared error を新設しないのは、そのために Feature XML を
編集すれば drop-in 置換が壊れるからである。
実装しないのは手抜きではなく方針で、これらのコマンドは世界モデルに対応物の無いハードウェアを動かすもの
（アーム姿勢・PLC アドレス・PacScript のタスク名）であり、成功したふりをすればワークフローが
**このラボでは通るが実機では通らない**依存を持ってしまう。「実装済みは Transfer だけ」を保つ検査は
上記の単体テストに入っている。

### station マップ（`ARDEA_STATIONS`）

**`Transfer` は実機と同じ station 名を取る。** 実機は station 名（`Base1`, `Base2`, ...）を motion 設定で
解決し、そこに「レール上の位置」と「到達する robot タスク」が書かれている。モックにはどちらも対応物が無いが、
**プレートが物理的にどこにあるか**は世界モデルにあるので、等価物は名前 → location の対応表だけになる。

環境変数 1 本で渡す（`instruments/mock_instruments/stations.py`）:

```
ARDEA_STATIONS=Base1=station.slot1,Base2=station.slot2,Base3=seal-remover.stage,Base4=plateloc.stage,Base5=centrifuge.deck,Base6=thermal-cycler.block
```

実機のベンチと同じ構成である。各ステーションは **1 spot** を持つ。

| station | 実機 | location |
|---|---|---|
| `Base1` / `Base2` | プレート置き場 2 つ | `station.slot1` / `station.slot2` |
| `Base3` | peeler（シール剥がし） | `seal-remover.stage` |
| `Base4` | sealer（PlateLoc） | `plateloc.stage` |
| `Base5` | plate centrifuge | `centrifuge.deck` |
| `Base6` | thermal cycler | `thermal-cycler.block` |

**なぜファイルでなく環境変数か**: サーバー設定は環境変数で渡すのがこのリポジトリの規約（`docs/RULES.md`）で、
この対応表はまさに per-server の設定である。加えて**リビルド無しで変えられる**（`--force-recreate` のみ）ので、
同じく run の合間に編集できる seed と歩調が合う。YAML ファイルにすればコメントは書けるが、読むために
ランタイムイメージへ PyYAML が入る（所要時間を JSON に焼いているのはそれを避けるためである）。

**起動時に検証して落とす**。未設定・空・`name=device.spot` でない・同じ名前が 2 回・同じ spot に 2 つの名前、は
すべて起動失敗にする。laboratory_model 系の環境変数と違い**省略は運用モードではない** — station 名を解決できない
搬送機は存在意義が無い。検査内容は `instruments/tests/test_stations.py`。

**実装する 3 プロパティの導出**（世界モデルに素直に対応するものだけを実装した）:

| プロパティ | 導出 |
|---|---|
| `CarriageService.StationNames` | station マップの名前をソートしたもの。**`Transfer` が受け付ける集合と完全に一致する**。起動時に読むので実機の「サーバー生存中は固定」という約束も満たす |
| `CarriageService.CarriagePosition` | 合成レール位置 = 上記リスト内の index × 100 mm。**実機の座標ではない**（実機の実測値は motion 設定にあり、この世界はその幾何を持たない）。目的は「動かない間は安定し、動けば変わる」という観測可能性で、`Transfer` が到達のたびに publish する。実座標を入れたくなったら station マップが置き場所である |
| `LabwareService.LightIsOn` | `ardea` device の opaque state の `light` キー（`GET /devices/ardea/state`）。実機ではロボットコントローラの変数なので世界モデルに対応物が無く、seed が宣言し運用者が `PUT /devices/ardea/state/light` で変える |

**世界モデルの失敗は declared error に分かれない。** 未宣言 location・item 不在・扉が閉じている、は
どれも undefined execution error として届く（実機なら `NoStationAtPosition` / `GraspFailed` 等に分かれる）。
`laboratory-client` がエラーコードを機械可読な形で返さないため。既知のギャップである。

## 世界モデルとの連携

- 各サーバーが作用する location は **`LABORATORY_MODEL_LOCATION`（`device.spot` 形式）で環境変数から**受け取る。
  **シードが宣言した spot でなければならない**（未宣言なら `unknown_location` / 404 で失敗する）。
- HTTP アクセス（URL 組み立て・JSON・タイムアウト・エラー変換）と設定の読み取りは共有パッケージ
  `laboratory-client` に集約する。**ただし世界の意味づけ**（回転にはプレートが要る、開いた扉は到達可能を意味する）
  **は各サーバーの実装に残す** — world state の解釈は、それを行うコマンドに属する。
- 現在 item の存在を前提とするコマンド: `SpinCycle` / `StartCycle` / `Peel` / `StartRun`。
- 現在 access 状態を更新するコマンド: centrifuge の `OpenDoor` / `CloseDoor`、thermal cycler の `OpenLid` / `CloseLid`。
- `LabwareService.Transfer`（Ardea）は世界モデル上の **2 hop の `move`** として扱う。
  - **引数は station 名**（`Base1`..`Base6`）で、location へは station マップが変換する（上記）。
  - source から arm の spot（`ardea.gripper`）へ、続いて arm の spot から destination へ。
  - 保持中 item を表す独自の内部変数は持たない。搬送中のプレートが arm 上にあるのは実機でも実際の状態である。
  - **実機は 1 コマンドで経路全体を走る**（carriage を source へ → pick → destination へ → put）ので、
    モックも 1 コマンドで両 hop を行う。旧 trolley arm の `Pick` + `Place` の 2 呼び出しは無くなった。

## コマンド所要時間と同時実行

- 各コマンドの待ち時間は装置モジュール（`mock_instruments.<装置>`）が `sleep_for(<SiLA2 コマンド名>)` で引く。値はビルド時にイメージへ焼き込まれた
  `/app/command_durations.json`（ラボ全体の `config/command_durations.yaml` から切り出したもの）にある。
  **記述が無いコマンドは待たない。**
- **1 台で 2 つのコマンドが同時に実行されることは `ExecutionGuard`（`mock_instruments.runtime`）が拒否する**
  （`one_at_a_time("<コマンド名>")` デコレータ）。判定は Status ではなく「実行中か」で行う。`Stop*` には付けない。
- 詳細と理由は `docs/TIMING.md`。

## Status の扱い

**2 つの異なる enum があるので混同しない。**

| | 値 |
|---|---|
| `Status` プロパティ（**Ardea 以外の 5 台**） | 0=Not Connected, 1=Idle, 2=Running, 3=Error |
| `InstrumentState`（thermal cycler の `GetInstrumentState` 応答のみ） | 0=IDLE, 1=STANDBY, 2=RUNNING, 3=ERROR, 4=DIAGNOSTICS |

**Ardea の Feature には `Status` プロパティが無い**（実機の Feature 定義にそもそも無く、編集もしない）。
進捗は `Transfer` の intermediate response と `progress` で報告する。Idle/Running のブラケットは持たない。

各コマンドの遷移は **Feature XML の記述が契約**である。

- actuating コマンドは実行中 Running、正常終了で Idle、失敗で Error。
- read / set / stop / reset は開始時の status を維持する。
- **thermal cycler の `OpenLid` / `CloseLid` は Idle を維持する**（同系の centrifuge の `OpenDoor` / `CloseDoor` は
  Running になる）。これは XML 側の意図的な feature 固有の例外であり、モックのバグではない。
- `StartRun` は Running にしたまま返り、`StopRun` まで Running が続く。

## その他の実装上の前提

- observable command の終了処理は `sila2` 側の manager に任せ、feature 実装で `instance.complete()` は呼ばない。
- PlateLoc の sealing temperature や cycle count などは内部状態として保持し、getter 経由で参照する。
- `LabwareService.Transfer` は intermediate response で phase を報告する。所要時間を phase 数で等分し、
  1 phase ごとに待つので、ポーリングするクライアントには経路の進行が見える。
  **`progress` も phase 単位で更新する**（最後の phase で 1.0 になる）。

## ログ方針

- feature 実装はコマンド入口で `logging` の `INFO` ログを出す。
- 引数を持つコマンドは、確認に必要な主要パラメータをログへ含める。
- Docker Compose では各サーバーを `--verbose` 付きで起動し、これらの `INFO` ログが見えるようにする。

## 確認方法

- `samples/` に各サーバーへ直接接続する確認スクリプトを置く。`samples/run_all_smoke_tests.py` が 5 台分を束ねる。
- `samples/run_roundabout.py` は `station.slot1` の item を Ardea で
  `seal-remover.stage -> plateloc.stage -> thermal-cycler.block -> centrifuge.deck -> station.slot1` と一周させる。
  1 区間 = `Transfer` 1 回。初期化と最終確認には世界モデルを使うが、装置間の移動そのものは SiLA2 サーバーを
  直接呼び出して行う。
- `samples/ardea_server_smoke.py` は Transfer に加えて**未実装コマンドが即座に拒否されること**も確認する
  （`MoveCarriage`）。無応答で待たされるのではなくエラーで返るのが仕様である。
- 手順は `docs/OPERATIONS.md`。
