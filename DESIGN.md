# TemplateDiagramGenerator 設計案

Phase 1 · 2026-11-19

## 1. 全体アーキテクチャ

ローカルの CloudFormation テンプレートから、テンプレートで確認できるアーキテクチャを静的レポートにする。サービス間の経路を主図にし、構築時の依存、スタック階層、全リソース一覧にもそれぞれアクセスできる。優先順位は解析の正確性、関係の意味、情報の選別、配置、装飾の順とする。

```text
CloudFormation YAML / JSON + 解析設定
                 │
           Template Loader
                 │
          Template Resolver
                 │
        Resource / Reference Model
            ┌────┴────┐
       Semantic     Dependency
       Analyzer      Analyzer
            └────┬────┘
          Architecture Model ─────────── JSON
                 │
            View Generator
                 │
              View Model
                 │
              Renderer ───────────── DOT / SVG / HTML
```

依存解析は、参照と `DependsOn` を独立に集計する。スタック図には、親子関係とスタック間の参照を異なる線種で表示する。意味解析は AWS リソースの型、該当プロパティ、解決時の出典を照合する。各層の間にデータ契約を置くと、参照解決・意味判定・図の可読性を個別に検証でき、後から描画形式も追加できる。テンプレートから直接 DOT を生成する案は工程数を減らせるが、意味判定と描画が結合し、同じ解析結果の再利用が難しくなる。

## 2. 採用技術

| 項目 | 採用案と理由 | 代替案とトレードオフ |
| --- | --- | --- |
| 実行環境・配布 | Python 3.12 以上、`pyproject.toml`、`src` レイアウト。`pip` で導入し、コンソールスクリプトとして実行する | 単一バイナリは配布が簡単になる一方、OS 別のビルドと Graphviz の同梱管理が増える |
| CLI・モデル | 標準ライブラリの `argparse`、`dataclasses`、`enum`、`pathlib`、`json`。設定の型と未知キーは境界で検証する | Typer や Pydantic は機能追加時に便利だが、初期のコマンドとスキーマには依存が増える |
| YAML・JSON | PyYAML の安全なローダーに許可済み短縮タグの変換を追加。JSON は標準ライブラリで解析する | `ruamel.yaml` は位置情報やラウンドトリップに強い。初期版はテンプレートの書き戻しが不要で、行・列は取得可能な場合の補助情報とする。 [PyYAML 公式資料](https://pyyaml.org/wiki/PyYAMLDocumentation) |
| グラフ・描画 | Python の辞書と集合で索引とグラフを持ち、DOT を生成する。SVG・HTML の生成時に外部 `dot` を実行する | NetworkX は高度な分割・グラフ演算が必要になった段階で採用を再検討。Python の Graphviz ラッパーを導入しても、SVG には `dot` の配布が必要になる。 [Graphviz dot](https://graphviz.org/docs/layouts/dot/) |
| 品質管理 | pytest、Ruff、CI で Windows・Linux を確認。レンダリングの再現性を比較する CI では Python 依存、Graphviz、フォントを固定する | 標準の `unittest` でも検証可能だが、pytest のパラメータ化と一時ディレクトリにより多数の小さな入力ケースを読みやすく保てる |

## 3. ディレクトリ構成

```text
TemplateDiagramGenerator/
├── DESIGN.md
├── README.md
├── pyproject.toml
├── src/template_diagram_generator/
│   ├── cli.py
│   ├── config.py
│   ├── loader.py
│   ├── resolver.py
│   ├── models.py
│   ├── dependency.py
│   ├── semantics/
│   │   ├── analyzer.py
│   │   └── rules/             # CloudFront、API Gateway、イベント、ELB/ECS、ネットワーク
│   ├── views.py
│   ├── renderers/            # Graphviz DOT/SVG、静的 HTML
│   └── publication.py        # 公開用モデルへの変換と出力値の制限
├── tests/
│   ├── unit/
│   └── integration/
└── examples/                 # 架空のテンプレートと設定
```

コアはレイヤーごとにファイルを分け、サービスルールとレンダラーのみ初期からパッケージにする。一括配置よりテストと拡張の対象を探しやすく、最初から全層を細分化する構成よりファイル数を抑えられる。`DESIGN.md` 以外は実装フェーズで必要になった順に作成する。

## 4. レイヤーのインターフェース

| レイヤー | インターフェース案 | 責務 |
| --- | --- | --- |
| Loader | `load(path) → TemplateDocument` | ファイルの読込、構文と基本構造の確認、短縮タグの正規化、ソース位置の保持。重複キーは明示的にエラーにする |
| Resolver | `resolve(roots, options, loader) → ResourceGraph` | スタックの展開、式と条件の評価、出典の追跡、未解決事項の記録 |
| Dependency Analyzer | `analyze_dependencies(graph) → DependencyResult` | 直接参照と明示的な依存を索引化し、根拠付きの `DEPENDENCY` とスタック間参照を作る |
| Semantic Analyzer | `analyze_semantics(graph, rules) → SemanticResult` | 型とプロパティに基づくエッジ・包含・診断を返す。ルールから参照解決器の内部状態には触れない |
| Architecture Model | `build_architecture(graph, semantic, dependencies) → ArchitectureModel` | リソース、スタック、関係、所属、根拠、診断を集約する |
| View Generator | `build_views(public_model, options) → ViewBundle` | ビュー別にノードの選択、集約、図の分割を行う。基底データはモデルに保存される |
| Renderer | `render(views, formats, options) → Artifacts` | ビューからファイルを生成する。図の描画部分は `Diagram → DOT/SVG` の境界で交換できる |

通常の関数と型付きレコードで渡し、I/O の差し替えが必要な Loader と描画の境界は小さいインターフェースにする。全層をクラス階層にする場合より拡張箇所は限定されるが、呼び出し順とデータの所有範囲が明確になる。公開用 JSON は `ArchitectureModel` の構造を保つ安全な投影から生成する。同じ投影を HTML・DOT・SVG にも使う。

### 4.1 ロードと式の評価

- YAML の `!Ref`、`!GetAtt`、`!Sub`、`!Condition` などを長形式へ変換する。`Fn::GetAtt` のドット表記は最初のドットで分け、`Outputs.Name` などの属性名を保持する。未知の組み込み関数に相当するタグ・式は診断付きで保持する。YAML の自動型変換は意図しない日付・真偽値変換を抑えるよう調整し、CloudFormation の型が必要な場所で解釈する。
- ルートは明示値を優先し、次に宣言された既定値を使う。子は親の `Properties.Parameters` を優先し、未指定の項目に子の既定値を使う。明示してある式が未解決なら、その状態を子に引き継ぐ。宣言型に従って文字列や `CommaDelimitedList` を扱い、オフラインで値が得られない疑似パラメータは記号のまま保持する。
- 条件は `TRUE`、`FALSE`、`UNKNOWN` の三値。条件への参照と `Fn::Equals`、`Fn::And`、`Fn::Or`、`Fn::Not`、`Fn::If` を扱う。確定した `Fn::If` は選択分岐だけを有効にし、`AWS::NoValue` はプロパティまたは配列要素を省略する。未知の分岐は条件付きの参照を保持し、両分岐が同じ対象を示すことが確認できる場合に限り確定扱いにできる。`FALSE` のリソースは宣言として一覧と JSON に残す。 [CloudFormation 条件関数](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-conditions.html)
- `Ref` と `Fn::GetAtt` はリソースの識別子と属性を記号として保持する。`Fn::Sub` は変数マップを優先し、リテラルエスケープを扱う。`Fn::Join` はリテラル部分と記号部分を順序付きでつなぎ、`Fn::FindInMap` は参照先が静的に確定する場合だけ展開する。意味ルールはプロパティに合った形と属性を照合する。文字列のどこかにリソースが含まれている事実だけでは接続先を確定しない。 [Fn::Sub](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-sub.html) · [Fn::GetAtt](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-getatt.html)
- `AWS::LanguageExtensions` の `Fn::ForEach` は、許可されたセクションで静的に評価できるコレクションを展開する。入れ子、`${Identifier}` と `&{Identifier}`、展開後キー衝突を扱い、反復元の位置を保持する。動的コレクションや未対応の組み合わせは、該当するブロックを未展開として残す。言語拡張専用の組み込み関数も、対応表に入れた範囲を個別に検証する。 [Fn::ForEach](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-foreach.html) · [AWS::LanguageExtensions](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/transform-aws-languageextensions.html)
- スタック展開は祖先の正規化ファイルパスで循環を検出する。兄弟スタックで同じテンプレートを再利用する場合、ロード結果を共有し、スタックの評価結果をインスタンスごとに保持する。式の循環は、評価中の `(stack_id, kind, key)` を検出する。リソース参照だけの循環は依存診断として扱い、式評価の循環と混同しない。深さ、展開数、入力サイズは上限を設け、解析を中断した範囲を診断に残す。

### 4.2 ネストスタック

`AWS::CloudFormation::Stack` は、スタックインスタンス ID の明示マッピングを優先する。マッピングがない場合、解決済み `TemplateURL` がローカル相対パスなら、親テンプレートを基準に入力ベース内のファイルを探す。この相対パス規約はローカル解析専用とする。S3・HTTP(S) URL は対応するローカルファイルを設定でき、指定のない場合はスタック枠と未解決診断を保持する。シンボリックリンクなどを解決した後の実パスでも入力ベース内であることを確認する。親から子の `Parameters`、子から親の `GetAtt Child.Outputs.Name`、親を経由した兄弟スタック間の値の流れを追跡する。`Ref Child` は子スタック自体を指す。 [ネストスタックの利用](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-nested-stacks.html) · [AWS::CloudFormation::Stack](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-cloudformation-stack.html)

静的に確認できる範囲を診断付きで扱う方式は、AWS 上で処理済みテンプレートを取得する方式より解析範囲が狭い。ローカル入力と設定だけで再現でき、CI で認証やデプロイ済みスタックが不要になる点を優先する。

## 5. データ契約

| モデル | 主なフィールドと不変条件 |
| --- | --- |
| `TemplateDocument` / `SourceLocation` | 正規化済み AST、入力ベース相対のソース名、要素のパス、取得できる場合の行・列。プロパティパスは JSON Pointer 形式。展開済み要素は元の宣言位置と展開後パスを持つ |
| `ResourceGraph` / `Stack` | `stacks`、`resources`、`references`、`diagnostics`。スタック ID は `<root-id>/<nested-logical-id>/…`。スタックは `parent_id`、呼出元リソース、ソース、解析状態を持つ。ルート入力は複数指定できる |
| `Resource` | `id`（`<stack-id>#<logical-id>`）、`stack_id`、`logical_id`、`type`、`source`、`condition_state`、宣言時 `properties`、評価済みプロパティ。宣言時と評価済みの値は内部専用 |
| `ResolvedValue` | `literal`、`resource_attribute`、`composite`、`omitted`、`unknown`、`redacted` の種別。式の位置、条件、出典、未解決理由を持つ。単純な `Ref` も記号として型と対象を保持する。未確定の値を空文字へ変換しない |
| `Reference` | `id`、`source_entity`（リソース・出力・スタックパラメータなど）、`path`、`kind`（`REF`、`GET_ATT`、`SUB`、`DEPENDS_ON` など）、直接参照先、解決先、属性、`status`、`trace`。`trace` は子パラメータや出力経由の根拠を保持する。一つの式に複数参照があれば参照単位で記録する |
| `ArchitectureModel` | `schema_version`、`metadata`、`nodes`、`edges`、`containers`、`memberships`、`evidence`、`diagnostics`。内部ノードは `resource_id` を保持し、対応外・条件付き・無効な宣言も索引可能にする。エッジ端点はモデル内 ID を使う |
| `Edge` / `Evidence` | `source_id`、`target_id`、`kind`（`TRAFFIC`、`EVENT`、`DATA_ACCESS`、`CONTAINS`、`ASSOCIATION`、`DEPENDENCY`）、`status`（`confirmed`、`conditional`、`inactive`）、`rule_id`、`evidence_ids`。根拠にはソース位置・パス・参照 ID を持つ。端点を特定できない場合は診断と参照に残す |
| `Container` / `Membership` | `axis`（`stack`、`network`。将来 `account`、`region` を追加可能）、`kind`、`parent_ids`、`member_id`、状態と根拠。VPC とスタックの所属を独立に持ち、複数サブネットとの関連にも対応する |
| `ViewBundle` / `Diagram` | 表示用ノードとエッジ、グループ、スタイルの意味、分割ページ、一覧行、診断、出典。集約ノードは `member_ids`、集約エッジは元の `edge_ids`、ページ外スタブは実体への参照を持つ。図と一覧の ID はモデルと対応する |
| `Diagnostic` / `Metadata` | 診断には `code`、`severity`、`source`、`path`、対象 ID と固定メッセージを持つ。公開メタデータにはツールとスキーマの版、ルート ID、入力の相対ソース名、解析済み範囲を含める |

`confirmed` はテンプレートに基づき構成が特定できた状態を示す。サービスの稼働状態、IAM による実行可否、パケットの到達性は別の問題として扱う。エッジは関係の方向と両端を保持し、根拠が異なる複数経路はモデル上で区別する。`DEPENDENCY` の向きは参照元から参照先、`CONTAINS` の向きは包含側から所属側。公開 JSON の順序は ID とパスによる明示ソートで固定する。安定 ID にルート・スタックの識別子を含める方式は短い Logical ID 単体より長くなるが、ネスト間の同名リソースを区別できる。スキーマの破壊的変更は版を上げる。

## 6. Semantic Analyzer の拡張方式

サービスごとのルールを、`rule_id`、対象リソース型と評価関数を持つレジストリに登録する。例えば `apigatewayv2.lambda_integration` の評価関数は、リソース型の索引と解決済みの参照を読む。型付きの候補関係、必要なプロパティパス、条件の伝播結果、診断を返す。アナライザーが共通で端点、根拠、関係の状態を検証し、安定した順序と ID で集約する。ルールごとに正例と境界例を用意する。

初期版は同梱ルールを明示登録する。Python ライブラリから利用する場合はレジストリを引数で追加できる。一般利用者向けの設定はデータ項目に限定する。将来、独立配布が必要になった段階で、バージョン付きのインターフェースと利用者が明示有効化する entry point を検討する。宣言的なプロパティ対応表だけでも単純な関係は書ける一方、複数リソースをまたぐ Route や Target Group の照合には専用コードが適している。

## 7. 初期対応の AWS リソースと関係

| 構成 | 関係と判定根拠 | 代表的な境界例 |
| --- | --- | --- |
| CloudFront → S3 / ALB | `TRAFFIC`。`DefaultCacheBehavior` と `CacheBehaviors` の `TargetOriginId` を `Origins[].Id` に照合し、対象オリジンの `DomainName` をバケットまたは ALB の適切なドメイン属性に結び付ける | `Ref` で得たバケット名や任意のホスト名は単独では根拠に不足。オリジングループと VPC Origin は診断の対象。 [CloudFront Origin](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-cloudfront-distribution-origin.html) |
| API Gateway HTTP API → Lambda | `TRAFFIC`。`ApiGatewayV2::Api`、同じ API を指す `Route.ApiId` と `Integration.ApiId`、`Route.Target` の `integrations/<id>`、`AWS_PROXY` の `IntegrationUri` に含まれる明示的な Lambda ARN を照合する | API の所有者が不一致、ルート未割当、権限の宣言だけ、他サービス用 `IntegrationSubtype` などは確定経路にしない。 [API Gateway Route](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-apigatewayv2-route.html) · [Integration](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-apigatewayv2-integration.html) |
| ALB → Target Group → ECS | `TRAFFIC`。`Listener.LoadBalancerArn`、`DefaultActions` または `ListenerRule.Actions` の forward 先、`ECS::Service.LoadBalancers[].TargetGroupArn` を照合。加重転送は有効なターゲットを扱い、既知の重み 0 は通常経路から外す | redirect、fixed-response、`DependsOn` から転送経路を作らない。RDS は一覧と確認できるネットワーク所属に出す。 [ALB の転送設定](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/create-listener.html) · [ECS Service](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecs-service.html) |
| EventBridge → Lambda / SQS、SQS → Lambda | `EVENT`。`Events::Rule.Targets[].Arn` と `Lambda::EventSourceMapping` の `EventSourceArn`・`FunctionName` を型と照合。設定上無効なルールやマッピングは `inactive`、有効状態が解決不能なら条件付きとする | 権限、DLQ、イベントパターン中の参照から通常イベント経路を作らない。SQS から Lambda への矢印はイベントの流れを表す。 [EventBridge Rule](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-events-rule.html) · [Lambda EventSourceMapping](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lambda-eventsourcemapping.html) |
| VPC、Subnet、ネットワーク所属 | `CONTAINS` / `ASSOCIATION`。`Subnet.VpcId` は VPC の包含。ALB の `Subnets` / `SubnetMappings`、ECS の `AwsvpcConfiguration.Subnets`、RDS の `DBSubnetGroupName` と `DBSubnetGroup.SubnetIds` から関係をたどる。単一 VPC が全サブネットの根拠から判明すれば VPC 内に配置。単一サブネット確定時の配置は、そのサービスの所属の意味に従う | 複数サブネットへの関連は詳細図に残す。名前や `MapPublicIpOnLaunch` 単独から Public / Private を分類しない。Security Group は対象を特定できる明示参照を関連として表示し、許可設定から通信を推定しない。 [CloudFormation Subnet](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ec2-subnet.html) · [ECS Service](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecs-service.html) |

この組み合わせで、通信・イベント・複数リソースによる中継・ネットワーク所属を独立に検証できる。接続先の判定は直接参照と、対応ルールが構造を検証できる `Sub`／`Join`、ネストスタックを経由した値から始める。外部の ARN や URL は初期版では診断に集約し、リテラルの名前に一致するリソースを検索して線を付ける方式は採らない。

`DATA_ACCESS` は契約に用意する。初期の Lambda＋DynamoDB と ECS＋RDS では、IAM 権限・環境変数・ネットワーク設定だけでアプリの読書きを確定できないため、データアクセス線が出ないことを検証する。宣言されたサービス統合で操作と対象を特定できる事例を、次のルール候補にする。初期の型対応表には、描画対象、補助リソース、一覧のみ、型未対応を区別して載せる。NLB、API Gateway REST API、Route 53、SNS、任意の EC2 構成の意味解析は後続に回し、リソース情報は一覧に残す。

## 8. 表示ビューと Graphviz の可読性

| ビュー | 表示方針 |
| --- | --- |
| Architecture Overview | 通信・イベントの確定経路と、単体でも主要なサービスを表示。条件付き経路は明示オプションで表示する。中継リソースは、同じ確定経路を証明できる場合だけまとめ、詳細図と元の根拠へたどれるようにする |
| Architecture Detail | 中継、確認できた配置、構成上の関連を展開。通信・イベント・関連は線種と凡例で区別する。複数のサブネットに関わるノードは VPC の直下に配置し、サブネットとの関係を別途表現する |
| Stack Overview | 親子階層とスタック間参照を表示。複数の参照をスタック間の一本の線に集約し、件数と根拠を一覧に載せる。子テンプレート未解決のスタックも表示する |
| Resource Inventory | Logical ID、型、スタック、条件状態、型対応、出典を表にする。概要から省略したものや条件が偽のリソースも含める。HTML 内のローカル検索で絞り込める |

図が上限を超えたら、まず主要経路の連結成分で分け、次に明示された VPC・スタックのまとまりと主要経路を使って大きな成分を分ける。分割順と境界は安定 ID で決定する。別ページへの接続はスタブと接続先リストで保持する。実ノード上限は概要 20、詳細 40 を既定値とし、スタブ数には別の上限を設け、残りは件数とリンク一覧で確認できるようにする。同じ両端・関係種別の複数線は、表示上で根拠を束ねる。一枚の大きな図をズームする方式より、読解範囲を限定できる一方、図をまたぐ経路の追跡にはリンクが必要になる。

Graphviz `dot` の既定は左から右とし、主経路をランク配置に使う。関連線には `constraint=false` を使い、配置への影響を抑える。詳細図の cluster は確認できたネットワークの包含に使い、スタック階層は専用図で扱う。初期版はテキストラベル、線種と凡例を優先する。SVG が環境ごとに変わり得るため、同一 DOT からのバイト比較は Graphviz・フォント・OS を固定した環境でのみ保証対象とする。 [Graphviz の属性](https://graphviz.org/doc/info/attrs.html)

## 9. 設定ファイルと CLI

スキーマは `version: 1`。未知キー、型違い、ルート ID 重複、未対応フォーマットは設定エラーにする。ルート ID は区切り文字 `/` と `#` を含まない英数字・ハイフン・アンダースコアとする。パラメータはルートごとに与える。`nested_stack_paths` のキーはルート ID から始まるスタックインスタンス ID、値は入力ベースからの相対パス。未使用のマッピングは、無効な条件で省かれた場合と区別して診断する。

```yaml
version: 1
input:
  base_directory: .
  roots:
    - id: web
      template: templates/root.yaml
      parameters:
        Environment: dev
  nested_stack_paths:
    web/Application: templates/application.yaml
views:
  overview:
    max_nodes: 20
    direction: LR
    show_conditional: false
  detail:
    max_nodes: 40
    show_network_boundaries: true
output:
  directory: docs
  formats: [html, svg, json]
```

```text
template-diagram generate --config diagram.yaml
template-diagram generate --template templates/root.yaml --output docs --format json --format dot
template-diagram generate --config diagram.yaml --fail-on-warning
template-diagram --help
template-diagram --version
```

`--config` と `--template` は入力方式として排他的。`--template` は複数回指定でき、拡張子を除いたファイル名から有効かつ一意なルート ID が作れない場合は設定で明示する。設定の `base_directory` と出力先は設定ファイルを基準とし、設定内の入力テンプレートは入力ベースを基準とする。直接指定と CLI の出力先上書きは実行時の作業ディレクトリを基準にする。CLI の `--output` と繰返し可能な `--format` が設定の同項目に優先する。設定省略時の成果物は `diagram-output` 以下の HTML・SVG・JSON。JSON と DOT のみの生成は `dot` コマンドがない環境でも動作する。

HTML は `index.html` 一枚に概要、詳細、スタック、一覧、診断、入力メタデータを内包する。SVG・DOT は図ごとに `overview-001.svg` などの名前で生成し、JSON は `architecture.json` とする。成果物は一時領域で生成・検証した後に確定し、HTML は自己完結した状態で配置する。CI では実行専用の出力ディレクトリを指定できる。結果の要約を標準出力、診断を標準エラーに出す。終了コードは `0` が生成成功、`1` が入力・レンダリング・I/O の失敗、`2` が CLI・設定の誤り、`3` が `--fail-on-warning` による品質ゲート失敗。根となるファイルの読込・構文エラーは失敗とし、子テンプレートが手元にない場合や式の未解決は部分解析の警告とする。品質ゲートで失敗した実行も、診断を含む生成物を残す。

設定の許容範囲を絞ると入力ミスを検出しやすくなる。表示ラベル上書きや個別の表示ルールを導入する場合は、公開値の制約とスキーマの既定値を定めてから追加する。任意の Python や式は設定に持ち込まない。

### 9.1 公開用データと描画の安全性

- `NoEcho` と Secrets Manager／SSM の動的参照は評価時に伏せる。伏せた値を含む `Sub`、`Join`、子パラメータ、出力式、条件から値や分岐結果を公開しない。機密の可能性がある値を必要とする関係は、未解決として記録する。 [NoEcho の仕様](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/parameters-section-structure.html) · [動的参照](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references.html)
- 公開モデルへ渡すフィールドは許可リスト方式とする。識別子、型、状態、定型の関係、出典情報を採用し、生のプロパティ、パラメータ値、Outputs の値、Description、タグ、入力 AST、内部例外文字列は公開形式に渡さない。診断パスに任意のマップキーが入る場合は伏せる。Logical ID や相対ファイル名は公開情報として扱うため、ソースの命名自体に機密が含まれる運用は対象外とする。
- DOT には引用符付き文字列としてエスケープしたラベルと内部生成 ID を使う。テンプレート由来の HTML ラベルや URL 属性は生成しない。`dot` はシェルを介さず、タイムアウト付きで呼ぶ。SVG は許可した図形・テキスト・属性だけを通す XML 投影を経て、外部参照、イベント属性、スクリプトを排除する。HTML にはエスケープ済みテキストと、内部 ID を図ごとに名前空間化した SVG を埋め込む。検索に使う固定のローカルスクリプトにテンプレート文字列を連結しない。外部 CDN、画像、フォントへの依存は置かない。

## 10. テストと品質保証

単体テストでは各境界の入出力を検証する。Loader は YAML・JSON の等価性、短縮タグ、重複キー、構文エラー。Resolver は変数マップの優先、リテラルエスケープ、条件の三値、`NoValue`、マッピング、`ForEach`、ネスト・循環・上限を対象にする。意味解析は、関係の種別、方向、端点、根拠パスを直接 assert し、同種の参照が別プロパティに現れる負例を対にする。ビューは除外・集約・分割・ページ外リンクを検証し、入力モデルの不変性を確認する。

| 架空サンプル | 統合テストで固定する期待結果 |
| --- | --- |
| CloudFront ＋ S3 | 選択された有効オリジンへの `TRAFFIC`。未使用オリジンとバケット権限だけの構成からの経路は 0 件 |
| HTTP API ＋ Lambda ＋ DynamoDB | API から Lambda への `TRAFFIC` と Route／Integration の根拠。DynamoDB は一覧にあり、権限・環境変数だけの `DATA_ACCESS` は 0 件 |
| ALB ＋ ECS ＋ RDS | Listener と Target Group を経由する ECS への `TRAFFIC`、VPC／Subnet の所属。RDS への推測経路は 0 件 |
| EventBridge ＋ SQS ＋ Lambda | `Rule → Queue → Function` と直接 Lambda ターゲットの `EVENT`。無効なイベントマッピングは主図から除外 |
| VPC ＋ Public/Private と命名された Subnet | `VpcId` で確認できる包含。命名が逆でも同じ包含になり、命名だけで公開性を分類しない |
| ネストスタック | 親から子のパラメータ、子から親の出力、親を経由する兄弟間参照、同名 Logical ID の区別。外部 URL のローカルマッピングと循環の診断 |
| Parameters ＋ Conditions | パラメータ差で確定経路が切り替わる。未知条件は条件付き、偽条件の宣言は一覧で追跡可能 |
| 未解決参照・未対応型 | 診断にコードと位置があり、未対応ノードは一覧に残る。未解決の端点に向かう確定線は 0 件 |

固定入力を二回生成して JSON と DOT を比較し、Graphviz を固定したジョブで SVG と HTML を比較する。HTML はブラウザで全ビューと検索を確認し、悪意のある文字列や `NoEcho` テスト値が成果物・ログに漏れないことを確認する。CLI は Graphviz の有無、終了コード、出力先、および別の作業ディレクトリからの実行をテストする。初期の負荷評価は、架空の 1,000 リソースに対して分割の上限、参照の欠落、処理時間と最大メモリを測る。時間の合否閾値は CI 環境の初回計測後に決める。画像スナップショットだけで検証する場合より、根拠や条件の退行を直接見つけやすく、代表図の目視確認で過密さも評価できる。

## 11. 実装順序と完了条件

| フェーズ | 実装順序 | 完了条件 |
| --- | --- | --- |
| 1. 設計 | 本書で要件、型契約、初期ルール、公開仕様を決める | 設計案を提示し、対象範囲とトレードオフを確定できる状態にする |
| 2. 解析基盤 | Loader → 値・条件 → リソースと参照 → `ForEach` → ネストと循環 | YAML・JSON、ネストの Parameters／Outputs、条件、展開、ソース位置を単体テストで確認。未解決と失敗が診断で識別できる |
| 3. 意味解析 | 依存索引 → ルール登録 → 通信・イベント → ネットワーク所属 | 初期ルールの正例・負例で種別、方向、根拠、条件付き状態を検証。未対応型と依存線が識別できる |
| 4. ビュー | 一覧・スタック → 概要・詳細 → 集約と分割 | 全ビューの要素が元モデルへ対応し、上限超過でもページ外への参照と全件一覧が残る |
| 5. 描画 | DOT → SVG → HTML → 代表図の調整 | ファイルを静的に開ける。複数ビューと凡例・診断が見え、公開値のエスケープと SVG の制約を確認できる |
| 6. CLI・CI | パッケージ、設定解決、実行コマンド、終了コード、成果物配置 | `pip` で導入し `--help` を実行できる。無認証・非対話で JSON と DOT を生成でき、Graphviz がある環境では SVG と HTML も生成できる |
| 7. 総合検証 | 架空サンプル、負荷・再現性、Windows／Linux、README | 8 ケースの統合テストと CI が通り、代表図の目視確認と負荷測定結果を残す。README に導入・設定・制約・静的配置手順を記載する |

早期に CLI と描画を完成させる案はデモを早く出せるが、根拠や条件の契約に変更が入ると成果物を作り直しやすい。先に解析とモデルを固定し、各フェーズの完了条件を次のフェーズの入力にする。Phase 2 は設計確定後に着手する。

## 12. 技術リスクと初期対象範囲

| リスクまたは制約 | 初期の方針とトレードオフ |
| --- | --- |
| マクロ、SAM、`AWS::Include`、カスタム変換 | `AWS::LanguageExtensions` の明示した範囲のみローカル評価する。その他の Transform を含むスコープは原文の宣言を残し、変換を要する構成は確定図から外す。変換済みテンプレートをローカル入力とする選択肢を用意する |
| 別スタック・環境からの値 | ローカルに対応づけたネストのみ追跡する。`Fn::ImportValue`、クラウドのスタック出力取得、S3 の自動取得、疑似パラメータのクラウド問い合わせは後続の拡張候補。初期は未解決理由と参照位置を残す |
| ARN や名前を組み立てる式 | 正式な式の構造と属性を照合できる形から対応する。外部リテラル、任意文字列の名前一致、AWS が付与する物理名の復元を対象外とし、誤推定を抑える |
| 条件・変換・サブネット所属の曖昧さ | 条件付きの関係を確定関係と区別し、複数所属は保持する。複数環境の構成差分、ルートテーブルや SG を含む到達性解析、実行時の稼働検証は対象外 |
| 表示規模・環境差 | 意味モデル上の選別・集約・分割を優先し、Graphviz の版とフォントが異なる場合の配置差を許容する。Graphviz の不在は SVG／HTML 要求時に明示する |
| 推測を要するアプリ実装 | Lambda ソース、コンテナ内部、IAM ポリシーからの実通信推定は対象外。明示的に構成されたサービス統合を後から追加できる |
| 形式と拡張 | draw.io、公式 AWS アイコン、GUI、AWS への自動デプロイ、外部プラグインの自動読込は後続。公開 JSON と描画境界を保ち、必要になった機能から追加する |

この範囲では運用環境を完全に再現する精度に限界がある。テンプレートから確認できた経路、条件によって変わる経路、入力が足りない範囲を、成果物内の根拠と診断から追えることを初期版の品質とする。
