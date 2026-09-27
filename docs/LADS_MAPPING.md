# LADS OPC UA 版の対応表

## 目的

この文書は、各モック装置の SiLA2 インターフェイスが LADS OPC UA（OPC 30500、LADS 1.0.0）の
どこに対応するかと、**LADS 由来の差分**（LADS の規約に合わせたために SiLA2 版と挙動が異なる点）を記録する。
LADS 版を触るクライアント（labcode の LADS 対応など）はこの文書を基準にする。

## 前提

- **装置の振る舞いは両プロトコルで 1 つ**。LADS 版も SiLA2 版も `instruments/`（`mock_instruments`）の
  同じ装置オブジェクトを呼ぶので、状態・引数検証・前提条件・所要時間・世界モデルへの作用・エラー文言は
  同一である（`docs/RULES.md`「プロトコルと装置の振る舞いの分離」）。この文書に載っていない差は無い。
- 1 サーバー = 1 device = 1 FunctionalUnit。device は `Objects/DeviceSet/<Device>`（LADSDeviceType）。
- インスタンスの NodeId は vendor namespace（`http://ofplang.org/UA/MockLab/`）の文字列 NodeId で、
  ブラウズパスから作る（例: `PlateLoc.FunctionalUnitSet.Sealer.FunctionalUnitState`）。再起動しても変わらない。
- セキュリティは `--insecure`（SecurityPolicy None、匿名）のみ。SiLA2 版と同じ。
- 探索（discovery）は無い。接続先は compose が公開するポートで固定。

## 起動とポート

compose の profile で選ぶ（`docker-compose.yml` 冒頭のコメント、`docs/OPERATIONS.md`）。

| 装置 | SiLA2（profile `sila2`） | LADS（profile `lads`） | LADS の device / unit |
|---|---|---|---|
| Microplate Centrifuge | `sila2-server-1` : 50052 | `lads-server-1` : 4841 | `Centrifuge` / `Centrifuge` |
| PlateLoc | `sila2-server-2` : 50053 | `lads-server-2` : 4842 | `PlateLoc` / `Sealer` |
| Automated Plate Seal Remover | `sila2-server-3` : 50054 | `lads-server-3` : 4843 | `SealRemover` / `Peeler` |
| Automated Thermal Cycler | `sila2-server-4` : 50055 | `lads-server-4` : 4844 | `ThermalCycler` / `Cycler` |
| Ardea（搬送役） | `ardea-server-1` : 50057 | `ardea-lads-server-1` : 4847 | `Ardea` / `Transporter` |

両 profile を同時に起動すると、同じ世界モデルを共有する。ただし同時実行ガードはプロトコルごとに別なので、
同じ装置を SiLA2 と LADS から同時に操作することは止められない。

## 共通の対応

| SiLA2 | LADS |
|---|---|
| 処理を行う observable command（SpinCycle, StartCycle, Peel, StartRun, Transfer, ...） | **Program**: `FunctionalUnitState.StartProgram(templateId, properties, supervisoryJobId, supervisoryTaskId, samples)`。template id は SiLA2 のコマンド名 |
| コマンドの引数 | StartProgram の `properties`（KeyValueType[]、値は文字列）。キーは SiLA2 の引数名。**すべて必須で型付き**（SiLA2 と同じく、欠けていれば・数値でなければ開始前に拒否） |
| コマンドの応答 | `ProgramManager/ResultSet/<RunId>/Properties`。入力の properties、応答（SiLA2 の応答名）、`Outcome`（Completed / Failed）を持つ |
| 中間応答（Ardea の Phase） | `ProgramManager/ActiveProgram/CurrentStepName`・`CurrentStepNumber` |
| 実行の完了待ち | StartProgram は**コマンドが実行を開始した時点で** RunId を返す。完了は `FunctionalUnitState/CurrentState` と ResultSet への Result の出現で観測する。**Result が現れた時点で、状態と値（CycleCount など）は確定している** |
| `Status`（0 Not Connected / 1 Idle / 2 Running / 3 Error） | `FunctionalUnitState/CurrentState`: Error→**Aborted**、Running または Program 実行中→**Running**、それ以外→**Stopped**。装置の Status から導出するので SiLA2 版と食い違わない |
| Stop 系コマンド（StopCycle, StopRun, StopSpinCycle） | `FunctionalUnitState.Stop` |
| `Reset` | `FunctionalUnitState.Abort` ＋ `Clear`（装置が Error なら既に Aborted なので `Clear` だけ） |
| 蓋・扉の開閉 | CoverFunction の `CoverState.Open` / `Close`。CurrentState は Opening→Opened / Closing→Closed |
| setter（SetSealingTemperature 等）とその getter | ControlFunction の `TargetValue` への書き込みと読み出し。装置の setter が SiLA2 と同じ検証をし、拒否は書き込みの拒否になる |
| 測定値の読み出し（GetTapeLeft, CarriagePosition） | SensorFunction の `SensorValue` |
| LADS に対応物の無い値（CycleCount, Profiles, バージョン文字列 ...） | vendor 変数（`<Unit>/<名前>`） |
| エラーの文言 | `<Unit>/LastError`（vendor 変数）。失敗した呼び出し・Run の理由。成功で `""` に戻る |

### 失敗の見え方

SiLA2 では、実行開始（`begin_execution`）の前に検出した失敗はコマンドの拒否、開始後の失敗は実行エラーになる。
LADS でも**同じ分け目**を使う。

| いつ | SiLA2 | LADS |
|---|---|---|
| 実行開始前（引数・同時実行ガード・物が無い等） | コマンドがエラーで拒否 | メソッド呼び出しが Bad StatusCode を返す。**状態は何も変わらない** |
| 実行開始後（手順違反・世界モデルの失敗等） | 実行エラー（多くは Status が Error に） | Run が `Outcome=Failed`、装置が Error になれば unit は Aborted |

StatusCode の対応は次のとおり（理由の文章は LastError に入る。文言は SiLA2 のエラーメッセージと同一）。

| 失敗の種類 | SiLA2 でクライアントに見えるもの | LADS の StatusCode |
|---|---|---|
| 引数が不正 | `ValueError - ...` | `BadInvalidArgument` |
| 状態が不正（別コマンド実行中、手順違反、物が無い） | `RuntimeError - ...` | `BadInvalidState` |
| 世界モデルが作用を実行・確認できない | `RuntimeError - ...` | `BadInvalidState` |
| Ardea の未知の station | `InvalidStation`（defined error） | 開始後に判明するので Run が Failed |
| 存在しない program template | — | `BadNotFound` |
| 想定外の例外 | undefined execution error | `BadInternalError` |

## 装置ごとの対応

### Microplate Centrifuge（device `Centrifuge`、unit `Centrifuge`）

| SiLA2 | LADS |
|---|---|
| `OpenDoor(BucketNumber)` / `CloseDoor` | `FunctionSet/Door` の Open / Close（初期状態 Opened） |
| `SpinCycle`（15 引数） | program `SpinCycle`、同じ 15 property |
| `LoadPlate` / `UnloadPlate`（5 引数） | program `LoadPlate` / `UnloadPlate`、同じ 5 property |
| `Home` / `Park` | program `Home` / `Park` |
| `StopSpinCycle(BucketNumber)` | `Stop` |
| `Reset` | `Abort` ＋ `Clear` |
| `EnumerateProfiles` | vendor 変数 `Profiles` |
| `FirmwareVersion` / `HardwareVersion` | `Identification/SoftwareRevision` / `HardwareRevision` |
| `ActiveXVersion` / `CentrifugeActiveXVersion` / `CentrifugeHardwareVersion` | 同名の vendor 変数 |

### PlateLoc（device `PlateLoc`、unit `Sealer`）

| SiLA2 | LADS |
|---|---|
| `StartCycle` | program `StartCycle` |
| `StopCycle` | `Stop` |
| `Reset` | `Abort` ＋ `Clear` |
| `SetSealingTemperature` / `SealingTemperature` / `ActualTemperature` | `FunctionSet/SealingTemperature`（AnalogControlFunction、°C）の TargetValue / TargetValue / CurrentValue |
| `SetSealingTime` / `SealingTime` | `FunctionSet/SealingTime`（TimerControlFunction）の TargetValue、**ミリ秒** |
| `CycleCount` / `EnumerateProfiles` / `Version` | vendor 変数 `CycleCount` / `Profiles` / `Version` |
| `FirmwareVersion` | `Identification/SoftwareRevision` |

### Automated Plate Seal Remover（device `SealRemover`、unit `Peeler`）

| SiLA2 | LADS |
|---|---|
| `Peel(BeginPeelLocation, AdhesionTime)` → `InstrumentWarningMessage` | program `Peel`、Result の `InstrumentWarningMessage` |
| `ResetInstrument` | program `ResetInstrument` |
| `Reset` | `Abort` ＋ `Clear` |
| `GetTapeLeft` → `SupplySpoolRemaining` / `TakeUpSpoolRemaining` / `InstrumentWarningMessage` | `FunctionSet/SupplySpool`・`TakeUpSpool`（AnalogScalarSensorFunction）と vendor 変数 `InstrumentWarningMessage` |

### Automated Thermal Cycler（device `ThermalCycler`、unit `Cycler`）

| SiLA2 | LADS |
|---|---|
| `Load(ProtocolFileData)` | program `Load`、property `ProtocolFileData`（**文字列**。UTF-8 のバイト列として装置へ渡す） |
| `Validate(MaxSampleVolume)` | program `Validate` |
| `StartRun` / `StopRun` | program `StartRun` / `Stop` |
| `OpenLid` / `CloseLid` | `FunctionSet/Lid` の Open / Close（初期状態 Opened） |
| `Reset` | `Abort` ＋ `Clear` |
| `GetInstrumentState` | vendor 変数 `InstrumentState`（0 IDLE, 1 STANDBY, 2 RUNNING, 3 ERROR, 4 DIAGNOSTICS） |
| `ElapsedTime` / `RemainingTime` | 同名の vendor 変数（変化のたびに更新） |

Load → Validate → StartRun の順序は装置が強制する。順序を破ったときの失敗は SiLA2 と同じ。

### Ardea（device `Ardea`、unit `Transporter`）

| SiLA2 | LADS |
|---|---|
| `LabwareService.Transfer(SourceStation, DestinationStation)` → `CarriagePosition`, `AtRetractPose` | program `Transfer`、Result の `CarriagePosition` / `AtRetractPose`。7 phase は CurrentStepName |
| `CarriageService.CarriagePosition` | `FunctionSet/Carriage`（AnalogScalarSensorFunction、mm）。搬送中の移動も即時反映 |
| `CarriageService.StationNames` | vendor 変数 `StationNames` |
| `LabwareService.LightIsOn` | vendor 変数 `LightIsOn`。世界モデルを 0.5 秒ごとに読み直す（読めないときは Bad StatusCode） |
| その他 22 コマンド（SiLA2 版は拒否） | **載せない** |

station 名（`Base1`〜`Base6`）と station map（`ARDEA_STATIONS`）は SiLA2 版と同じ。

## LADS 由来の差分

LADS の規約・推奨に合わせたために SiLA2 版と挙動が異なる点。**これ以外に差は無い**（振る舞いは共有しているため）。

1. **StartProgram は Stopped のときだけ受け付ける。** SiLA2 のコマンドは「別のコマンドが実行中」の
   ときだけ拒否され、Status が Error でも、thermal cycler の StartRun 後の Running 中でも受け付ける。
   LADS では Aborted（Error）なら先に `Clear`、Running なら `Stop` か完了を待つ必要がある。
2. **Reset は Abort ＋ Clear。** Clear は Aborted からしか受け付けない（LADS の状態機械）。
3. **Stop・Clear・Cover の Open/Close・StartProgram は、装置コマンドの開始時点で戻る。** 完了は状態
   （Stopping→、Clearing→、Opening→Opened など）で観測する。SiLA2 の observable command を完了まで
   待つのに相当するのは、状態の遷移待ちである。
   （理由: asyncua のサーバーは 1 接続の要求を順に処理するので、完了まで待つメソッドはクライアントの
   死活監視を詰まらせて切断を招く。LADS の状態遷移の報告の仕方としても自然。）
4. **扉の Open と StopSpinCycle は bucket 1 固定。** LADS の Cover.Open と FunctionalUnitState.Stop は
   引数を取らない。モックは扉を開いた bucket を記録するだけで読み返さないので、クライアントからは区別できない。
5. **SealingTime はミリ秒。** OPC UA の Duration は ms の Double。SiLA2 版は秒。
6. **ProtocolFileData は文字列。** KeyValueType の値が文字列のため。装置へは UTF-8 のバイト列として渡す。
7. **thermal cycler の StartRun は Program としては完了し、unit は Stop まで Running のまま。**
   SiLA2 の Status 契約（StartRun 後も StopRun まで Running）をそのまま導出状態で見せているため。
8. **エラー文言は LastError で読む。** OPC UA のメソッド呼び出しは StatusCode しか返さない。
9. **Ardea の拒否 22 コマンドは存在しない。** SiLA2 版が拒否コマンドを並べるのは実機の Feature 定義と
   同一に保つため。LADS の実機は無いので揃える対象が無い。
10. **DI の Lock（LockingServices）は BadNotSupported。** LADS FunctionalUnit では Mandatory だが、
    モックはクライアントのロックを実装しない。

## 置き場所

| 対象 | 場所 |
|---|---|
| LADS 共通部品（NodeSet の同梱・状態機械・Program・Function・StatusCode 対応・サーバー起動） | `protocols/lads/lads_common/` |
| 各サーバー（どのコマンドをどのノードにするかだけを決める） | `protocols/lads/servers/<装置>_lads/` |
| 同梱 NodeSet（DI 1.04.0 / AMB 1.01.0 / Machinery 1.03.0 / LADS 1.0.0、原本のまま） | `protocols/lads/lads_common/lads_common/nodesets/` |
| smoke テストと roundabout、LADS クライアントの最小実装 | `samples/lads_*.py`、`samples/run_lads_roundabout.py`、`samples/lads_client.py` |
| SiLA2 版と LADS 版の挙動一致の検査 | `samples/run_parity.py` |
