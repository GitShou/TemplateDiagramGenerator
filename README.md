# TemplateDiagramGenerator

CloudFormation の YAML・JSON と Terraform の `.tf` をローカルで解析し、AWS の構成を静的な図とリソース一覧にします。AWS アカウント、認証情報、デプロイ済み環境は不要です。

## 実行

Python 3.12 以上が必要です。HTML・SVG を生成する場合は、Graphviz を導入し `dot -V` が実行できるようにしてください。JSON と DOT は Python だけで生成できます。 [Graphviz の入手先](https://graphviz.org/download/)

プロジェクトのディレクトリで実行します。

```powershell
python -m pip install .
template-diagram generate --config examples/diagram.yaml --fail-on-warning
```

`examples/diagram-output/index.html` をブラウザーで開くと、概要・詳細・スタックの各図、検索できるリソース一覧、診断、入力元を確認できます。ファイルを直接開くか、そのまま静的ホスティングに配置できます。図は HTML 内に埋め込まれます。SVG・DOT の個別ファイルと `architecture.json` も、指定した形式に応じて同じディレクトリに出力されます。

独立スタック、表示の変更、利用者定義の関係をまとめて試す場合は、`template-diagram generate --config examples/extended/diagram.yaml --fail-on-warning` を実行します。生成先は `examples/extended/diagram-output/` です。

手元のルートテンプレートを一つ解析する場合も、複数の `--template` を指定する場合も、ファイル名の拡張子を除いた部分がスタック ID になります。オプション一覧は `template-diagram generate --help` で表示できます。

```powershell
template-diagram generate --template templates/root.yaml --output diagrams --format json --format dot
```

`--template` は作業ディレクトリからの相対パスと絶対パスを受け付けます。作業ディレクトリ外のテンプレートを指定し、それらが同じディレクトリにあれば、そのディレクトリを入力ベースにします。異なる場所にあるルートや兄弟ディレクトリの子スタックも読む場合は、共通の入力ベースを `--input-base` で指定します。ルートも子スタックも、実パスが入力ベース内に収まる必要があります。

### Terraform の静的解析

```powershell
python -m pip install '.[terraform]'
template-diagram generate --terraform path/to/module --locale ja
template-diagram generate --config examples/terraform/diagram.yaml --fail-on-warning
```

`--terraform` にディレクトリまたは `.tf` ファイルを指定します。ファイルを指定した場合も、同じディレクトリにある `.tf` を一つのモジュールとして読み込みます。

設定ファイルでは `input.roots` に `terraform` を指定します。CloudFormation の `template` を使う別のルートと同じレポートにまとめられます。

```yaml
version: 1
input:
  roots:
    - id: app
      terraform: infrastructure/terraform
      variables: {enabled: true}
views:
  locale: ja
```

- AWS の S3 / CloudFront / Route 53、HTTP API / Lambda、ALB / ECS、SQS のイベントソース、Scheduler、VPC / Subnet など、対応するプロパティを解析します。未対応の型も一覧に残します。
- 入力ベース内のローカルモジュール、変数の既定値・設定値、`locals`、モジュール出力、単純な参照と文字列補間を追跡します。表示設定や利用者定義の関係は `app#aws_s3_bucket.site` または `app/child#aws_s3_bucket.site` のような ID を使います。 [Terraform の設定ファイル](https://developer.hashicorp.com/terraform/language/files) · [Terraform の参照式](https://developer.hashicorp.com/terraform/language/expressions/references)
- CI の入力は `.tf` とツール設定です。リモートモジュール、データソースの実行、複雑な式、`.tfvars` の自動読込、`.tf.json`、上書きファイル、plan・state JSON は初期対応の範囲外です。`count`／`for_each` が実行時に決まる宣言はインスタンスに展開せず、診断に残します。

## 設定

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
  locale: ja
  overview:
    selection: auto
    max_nodes: 20
    direction: LR
    show_conditional: false
  detail:
    max_nodes: 40
    show_network_boundaries: true
    show_conditional: true
output:
  directory: diagram-output
  formats: [html, svg, json, dot]
  localize_json_diagnostics: true
```

- `input.base_directory` と `output.directory` は設定ファイルからの相対パスです。`template` と `nested_stack_paths` の値は入力ベースからの相対パスです。CLI で指定する `--template` と `--output` は作業ディレクトリを基準にします。
- `nested_stack_paths` は `ルートID/子スタックのLogical ID` を指定します。多段の子はさらに `/LogicalId` を続けます。マッピングがない場合、ローカル相対パスの `TemplateURL` を親テンプレートから解決します。S3 や HTTP の URL を使う子は、ローカルコピーをマッピングしてください。入力の実パスは入力ベース内に収めます。
- `--output` と複数指定できる `--format` は設定を上書きします。出力先には実行専用のディレクトリを使うと、ページ数が減った際に前回の SVG・DOT を静的公開物へ混ぜずに済みます。
- `views.locale` に `ja` を指定すると、HTML の見出し・検索・診断と、SVG・DOT の凡例に対応する図中ラベルを日本語で表示します。既定は `en` です。`--locale ja` または `--locale en` で設定を上書きできます。`output.localize_json_diagnostics: true` または `--localize-json` を指定すると、JSON の診断にも選択した言語の `message_localized` が加わります。既存の `code` と `message` は自動処理向けに維持します。
- 概要・詳細のページ上限は実リソース数です。別ページへの接続は概要・詳細・スタック図のリンク一覧で追えます。`direction` は `LR` または `TB` です。
- 概要の既定 `selection: auto` は、主要なサービスと通信・イベントなどの関係を表示します。確定した接続が1本以下の場合は、確認できたアタッチメント、包含、監視の関係を最大12本、追加ノードを最大12件補足します。表示枠がより小さい場合はその上限に合わせます。主要ノードも接続もない場合は、対応する補助リソースを同じ上限で表示します。`selection: primary` で主要ノードとフローに絞れます。`views.resources` で表示を調整できます。
- ルートのパラメータは `input.roots[].parameters` の設定値を優先し、なければテンプレートの `Default` を使います。未指定パラメータごとの影響は、HTML の診断欄と JSON の `metadata.missing_parameters` で確認できます。件数は、そのパラメータを直接参照するか、参照をたどると到達する未解決・条件付きの式の数です。

### 独立スタックの参照と図の調整

```yaml
version: 1
input:
  roots:
    - id: foundation
      template: templates/foundation.yaml
      account_id: '123456789012'
      region: ap-northeast-1
      stack_name: ExampleFoundation
    - id: orders
      template: templates/orders.yaml
      account_id: '123456789012'
      region: ap-northeast-1
views:
  resources:
    'orders#OrderFunction':
      label: 注文処理
      overview: primary
    'orders#SpareBucket':
      overview: hidden
relationships:
  - source: 'orders#OrderFunction'
    target: 'foundation#AuditTable'
    type: DATA_ACCESS
    evidence: アプリケーション設計書の注文監査処理
```

- `account_id` と `region` を両ルートに指定すると、一致する同一スコープの `Outputs.Export` を `Fn::ImportValue` から参照できます。エクスポート名が `${AWS::StackName}` を使う場合は、ルートに `stack_name` を指定します。未指定・未検出・曖昧な参照は診断に出ます。エクスポート元は設定に列挙したルートテンプレートで、子スタックも同じスコープのエクスポートを読み込めます。 [AWS の Fn::ImportValue 仕様](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-importvalue.html)
- `views.resources` のキーは `スタックID#LogicalId` です。`label` は概要・詳細の表示名に使います。`overview` は `default`、`primary`、`hidden` のいずれかです。`primary` は概要の主ノードに加えます。`hidden` は対象ノードとその線を概要から省き、詳細図・一覧・JSON で元の構成を追えます。
- `relationships` は `TRAFFIC`、`EVENT`、`DATA_ACCESS`、`ASSOCIATION` を受け付けます。エッジは図で `User-defined`、JSON の `origin` で `user_defined` と表示されます。`evidence` は HTML の根拠欄と JSON に掲載されるため、表示名と同様に公開できる文言を指定してください。対象 ID が見つからない表示指定・関係は診断に出ます。

## 解析範囲と成果物の読み方

| 構成 | 根拠として使う代表的な指定 |
| --- | --- |
| CloudFront → S3 / ALB | キャッシュビヘイビアが選んだオリジンと、対象の CloudFormation ドメイン属性 |
| CloudFront のオリジングループ | キャッシュビヘイビアが選んだグループ、メンバー順、HTTP フェイルオーバー条件。主オリジンとフェイルオーバー先を区別 |
| HTTP API → Lambda | 同じ API に属する Route と Integration、Lambda ARN を指す `AWS_PROXY` の URI |
| REST API → Lambda | 同じ REST API に属する Method と Resource、POST の Lambda 呼び出し URI |
| SAM の API → Lambda、SQS / S3 → Lambda | `AWS::Serverless::Function` の `Events` と、同じ入力内で参照された API・キュー・バケット |
| ALB → ECS | Listener、forward action、Target Group、ECS の `LoadBalancers` |
| EventBridge → SQS / Lambda、SQS → Lambda | Rule のターゲットと Event Source Mapping |
| EventBridge Scheduler → Lambda / SQS / Step Functions / SNS / EventBridge バス | `Target.Arn` と対応する入力内のリソース。無効なスケジュールの関係は JSON で `inactive` として保持 |
| Route 53 → CloudFront / ELBv2 / API Gateway カスタムドメイン | A／AAAA Alias の `DNSName` と一致する `HostedZoneId`。図は `DNS alias`、JSON は `DNS` と表示 |
| Step Functions → DynamoDB | インライン定義の `GetItem`、`PutItem`、`UpdateItem`、`DeleteItem` と特定できるテーブル。操作は `operation` に記録 |
| VPC / Subnet / セキュリティグループ | `VpcId`、ロードバランサーと ECS のサブネット、RDS サブネットグループ、Lambda の `VpcConfig`、EC2 のネットワーク設定 |
| CloudWatch アラーム | メトリクスの名前空間・ディメンションによる Lambda、EC2、SQS、RDS、DynamoDB との関連。アラーム状態に応じた SNS・Lambda のアクション |

`architecture.json` にはスキーマ版、リソース、根拠付きの関係、スタックとネットワークの所属、CloudFormation 参照、診断が含まれます。`confirmed` はテンプレート上で特定できた構成、`conditional` は条件付き、`inactive` は無効な構成です。`Ref`、`GetAtt`、`DependsOn` は構築上の依存として保持します。実際の通信にはサービス固有の根拠が必要です。

条件付きリソース、未知型、外部の子テンプレート、未対応 intrinsic は一覧や診断で確認できます。REST API は `AWS::ApiGateway::Method` とローカルの Lambda 関数をたどる形式が対象です。Step Functions は `Definition` と JSON の `DefinitionString` を扱い、DynamoDB のテーブルはリソース参照か同一スタック内で一意な明示 `TableName` から特定します。S3 にある定義、実行時に決まるテーブル、タスクの `Credentials` が指定されたデータアクセスは診断に出ます。 [REST API の Integration](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-apigateway-method-integration.html) · [Route 53 の AliasTarget](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-route53-recordset-aliastarget.html) · [Step Functions の DynamoDB 統合](https://docs.aws.amazon.com/step-functions/latest/dg/connect-ddb.html)

SAM テンプレートでは `Function`、`Api`、`HttpApi` と `Globals` の対応プロパティを静的に読み取ります。元のリソース型は JSON の `input_type` と HTML のリソース一覧で追えます。SAM が暗黙に作る API、IAM ロール、デプロイなどの生成リソースを含む構成には、変換済みテンプレートを使用してください。 [SAM の Globals](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-specification-template-anatomy-globals.html) · [SAM のイベントソース](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-function-eventsource.html)

`Fn::Cidr` は静的に確定する IPv4・IPv6 のアドレス範囲を分割し、`Fn::Select` で結果から選べます。明示された `CidrBlock` を持つ VPC の `GetAtt` も使えます。実行時に割り当てられる CIDR は未確定です。`Fn::If` は条件が未確定なら両分岐を条件付きとして記録します。図を確定した関係に絞るには `views.overview.show_conditional` と `views.detail.show_conditional` を `false` にします。 [Fn::Cidr の仕様](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-cidr.html) · [Fn::Select の仕様](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-select.html)

CloudFront のグループは既定のオリジン優先順位と、入力内で特定できる接続先を扱います。Scheduler の汎用 AWS SDK ターゲットや実環境由来の ARN は診断に残します。 [CloudFront のオリジンフェイルオーバー](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html) · [Scheduler のテンプレート化されたターゲット](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-targets-templated.html)

AWS API による解決、SAM やカスタムマクロの実行、NLB の Listener 経路、IAM やアプリコードからの通信推定、実環境の到達性検証は対象外です。テンプレートに含まれる Description、設定値、Outputs の値、自由記述のキーは公開出力から除外します。リソース ID と相対ファイル名には公開できる名前を使用してください。

| 終了コード | 意味 |
| --- | --- |
| `0` | 出力成功 |
| `1` | 解析開始に必要な入力、描画またはファイル書き込みに失敗 |
| `2` | CLI 引数または設定のエラー |
| `3` | `--fail-on-warning` で警告を検出。診断付きの結果は保存済み |

## 開発と CI

```powershell
python -m pip install '.[dev]'
ruff check .
ruff format --check .
python -m pytest
```

架空のサンプルでは静的サイト、HTTP API、ロードバランシング、イベント処理、VPC と Subnet、ネストスタック、条件の切り替え、未解決参照を検証します。JSON と DOT は同じ入力で繰返し生成して比較します。SVG・HTML のバイト比較は同じ Graphviz・フォント・OS の環境で行います。Graphviz のある環境では SVG と HTML の統合テストも実行されます。構成・レイヤー・拡張方式は [設計書](DESIGN.md) を参照してください。

0.1.0 の合成負荷テストでは、Windows、Python 3.12.14、Graphviz 14.1.0 で1,000リソースと999接続を概要50ページ、詳細25ページに分割しました。解析とビュー作成は `tracemalloc` 有効時に1.27秒、同区間の Python メモリ割り当てピークは5.99 MiB でした。全形式154ファイル分のレンダリングは36.66秒でした。値は当該環境の単回計測です。
