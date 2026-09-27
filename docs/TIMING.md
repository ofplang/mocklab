# コマンド所要時間と同時実行

## 概要

各モックコマンドが「どれだけ時間をかけるか」を、実装中のリテラルではなく**設定として**持つ仕組み。
目的は 2 つある。

1. ワークフロー実行系のポーリング周期で `dispatch -> running -> completed` の遷移が**観測できる**速度で
   ラボを動かすこと（実効周期は 10〜30 秒程度なので、コマンドは秒〜数十秒必要になる）。
2. **ワークフロー側の所要時間見積りと意図的にずらせる**こと。食い違いが replan や running-task margin を
   実地に叩く。一致させてしまうと二重の世界モデルを走らせる意味が薄れる。

## ファイルと経路

| ファイル | 役割 |
|---|---|
| `config/command_durations.yaml` | **既定プロファイル。`devices: {}` だけの空ファイル＝どのコマンドも待たない** |
| `config/command_durations.realistic.yaml` | 秒〜数十秒。ワークフロー実行系を回すときに使う |
| `tools/slice_durations.py` | ビルド時に device 1 台分を切り出す |
| `tools/tests/test_command_durations.py` | 設定ファイルと実装の齟齬を検出する |

```
config/command_durations.yaml   （ラボ全体・YAML・コメント可）
        │  docker build（builder ステージ）
        ▼
/app/command_durations.json     （その device 1 台分・JSON・イメージに焼き込み）
        │  装置（mock_instruments）の from_environment が読む
        ▼
instrument.sleep_for("OpenDoor") （各コマンドが自分の SiLA2 コマンド名で引く。LADS 版も同じキー）
```

- **入力が YAML なのは人が読んで書くため、出力が JSON なのはプログラムしか読まないため。**
  この分担のおかげで **PyYAML は builder ステージだけに入り、10 のランタイムイメージ（SiLA2 と LADS の各 5 台）には増えません**。
- **焼き込みなので、値を変えるにはリビルドが必要**です（マウントではありません）。
  焼き込まれた内容は `docker compose exec sila2-server-1 cat /app/command_durations.json` で確認できます。
- プロファイルの切り替えは build arg 1 つ:

```bash
DURATIONS_FILE=command_durations.realistic.yaml docker compose build
docker compose up -d --force-recreate
```

> `docker compose up -d --build` はソース変更を反映しないことがあります。確実に入れ替えるには
> `docker compose build` と `--force-recreate` を明示してください。

## 書式

```yaml
devices:
  centrifuge:
    commands:
      SpinCycle: { duration: 20 }
      OpenDoor:  { duration: 3 }
```

- device 名は `laboratory_model.seed.yaml` と一致させる。コマンド名は feature 接頭辞を除いた素の SiLA2 コマンド名。
- **各コマンドの値をスカラーではなく mapping にしてある**のは、将来 jitter のような設定を
  `{ duration: 3, jitter: 0.15 }` と**足すだけ**にするため。スカラー略記も許すと、両形式を受けるパーサを
  恒久的に抱えることになる。device 単位の設定は `commands:` の隣に置ける。
- **切り出しスクリプトは知らないキーをそのまま通す**ので、ラボ全体のファイルに設定を足せば
  スクリプトを教育しなくてもサーバーまで届く。

## 既定は「待たない」

**ファイルに記述が無いコマンドは一切待ちません。** device のキーが無ければそのサーバーの全コマンドが待ちません。
既定プロファイルは `devices: {}` だけなので、**既定の挙動は「ラボが可能な限り速く動く」**です。

**なぜ空が正しい既定なのか**: モックの待ち時間は、それを観測するものがある時にだけ意味を持ちます。
`samples/` はコマンド実行中の Status 遷移を観測しておらず、sample の実行時間は SiLA2 の接続と discovery が
支配的です（実測: 空プロファイルと 0.05 秒プロファイルで差が誤差の範囲）。**トークン的な待ち時間は何も買っていませんでした。**

**ファイルを消さずに空で残してある**のは、「可能な限り速く動く」が**意図した決定**であることを
ファイル自身に宣言させるためです。ファイルが無いと、後から見た人には「まだ書いていない」のか
「意図してゼロ」なのか区別できません。

### 罠: 所要時間を宣言するプロファイル側の書き忘れ

**待たないコマンドは Running の窓が潰れるので、ポーリングするクライアントから Status 遷移が
観測できなくなります。** realistic プロファイルでこれが起きると、書き忘れが静かに効きます。

そのため **`tools/tests/test_command_durations.py` が実装（AST）を読んで、所要時間を宣言する
プロファイルとコードの齟齬を双方向に検出します**。
- 待つのに未記述 → 失敗（観測性を失う）
- 記述があるのに待たない → 失敗（コマンド名の typo・不要になった項目）

既定プロファイルはこの検査の対象外です（意図的に何も宣言しないので）。代わりに
**「空であること」自体をテストで固定**しています。コマンドを追加・変更したら realistic 側を更新してください。

## 同時実行ガード

長い所要時間により、1 サーバーに 2 つのコマンドが重なることが現実的になります。モックの内部状態
（protocol loaded / validated、開いている bucket など）はロック無しの素の属性なので、重なると静かに壊れます。

そこで **`ExecutionGuard.executing()`（`mock_instruments.runtime`）が 1 コマンドの実行中は装置を占有し、後から来たコマンドを拒否します**。

```
CloseDoor cannot start because OpenDoor is still executing on this server
```

**判定は「コマンドが実行中か」であって Status ではありません。** ここが重要な点です。`StartRun` は
契約上 `StopRun` まで Status を Running のまま残すので、**Status で判定すると、その run を終わらせるための
コマンドが必ず拒否されます**。

- ガードは `one_at_a_time("<コマンド名>")` デコレータで装置モジュールの各コマンドに付ける（本体を `with` で囲まない。
  名前は SiLA2 のコマンド名を明示的に渡す。拒否メッセージがその名前を出すため）。
- **`Stop*` コマンドには付けない。** 止めるためのコマンドを、止めたい対象の完了まで待たせるのは本末転倒。
  なお現状のモックでは stop が実行中コマンドを実際に中断することはできない（別の制限であり、ガードとは無関係）。
- **Ardea には Status ベースのガードが無い**。Feature に `Status` プロパティが無いので、同時実行ガードが
  唯一のガードである（`Transfer` に付いている）。

## 観測可能コマンドのエラーは poll しないと見えない

ガードの拒否は**呼び出し時ではなく、コマンドインスタンスを poll したときに現れます**。
observable command の呼び出しはインスタンスを即座に返すので、`instance.get_responses()` まで
到達しないと `UndefinedExecutionError` は観測できません。手で確認するときの落とし穴です。

## realistic プロファイルを使うときの注意

`samples/common.py` の `DEFAULT_TIMEOUT_SECONDS` は 10 秒で、realistic の一部コマンドはこれを超えます。

```bash
uv run python samples/run_all_smoke_tests.py --timeout 120
```

既定プロファイルでは 10 秒のままで十分です（どのコマンドも待たないので、ハングを速く失敗として報告できる）。

## 未実装・先送り

- **jitter（ランダムなぶれ）は入っていません。** 書式は上記のとおり受け入れる準備ができています。
- **所要時間を宣言するプロファイルは realistic の 1 本だけ**です。別の速度が欲しくなったらファイルを足し、
  `tools/tests/` の `TIMED_PROFILES` に 1 行加えれば同じ網羅チェックが効きます。
- **`LabwareService.Transfer` は realistic プロファイルで 30 秒**（ラボで最長のコマンド）。実機のレールは
  2600 mm / 50 mm/s で、その上に pick/put の 4 タスクが乗るので、これは丸めた概算です。モックはこの 30 秒を
  **報告する phase の数（7）で等分して消費**するので、ポーリングするクライアントに経路の進行が見えます。
  既定プロファイルでは 0 秒＝待たないので、phase は一瞬で流れます。
- **station には所要時間を書けません**（書いても誰も読みません）。station は seed が spot を宣言するだけの
  device で、どちらのプロトコルのサーバーも無いためです。`tools/tests/` の
  `test_profiles_describe_only_devices_that_have_a_server` がこれを検査します
  — seed に宣言があるだけでは足りない、という点が seed ベースの検査との違いです。
