# 选择性 JSON 事实披露服务（Selective JSON Disclosure）

提交一份嵌套 JSON 文档，服务会把所有 **叶事实**（标量、`null`、空对象 `{}`、空数组 `[]`）
按 [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) 路径展开，用「长度前缀 + 路径 +
规范 JSON 值」计算每个叶哈希，按路径排序构建可复现的 Merkle 根。之后可以只披露其中任意
若干条事实，并附带 Merkle 包含证明；验证方**完全不需要原始文档**，仅凭「根 + 所选值 +
证明」即可校验，且任何对路径、值或兄弟哈希的篡改都会导致根不匹配，错误带有精确定位。

## 一条命令启动

```bash
docker compose up --build
```

启动时容器内会自动执行 `pip install -r requirements.txt`（**依赖随启动安装**），
随后在 `0.0.0.0:8000` 启动 uvicorn。

- API 地址与端口：<http://localhost:8000>（容器内固定监听 `8000`）
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>

如需改用宿主机其他端口：

```bash
HOST_PORT=9000 docker compose up --build      # => http://localhost:9000
```

停止：`Ctrl+C` 或 `docker compose down`。

## 必要配置

所有配置通过环境变量提供，均为可选项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT`（仅 Compose） | `8000` | 宿主机发布端口，容器内始终是 `8000` |
| `APP_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `APP_PORT` | `8000` | uvicorn 监听端口 |
| `MAX_BODY_BYTES` | `10485760`（10 MiB） | 请求体大小上限，超限返回 `413` |
| `DEFAULT_PROOF_MODE` | `standard` | `/commit` 未显式传 `proofMode` 时的默认格式，可选 `standard` / `compact` |
| `SKIP_PIP_INSTALL` | `0` | 设为 `1` 可跳过启动时的依赖安装 |
| `REQUIREMENTS_FILE` | `/app/requirements.txt` | 启动时安装的依赖清单路径 |

可复制 `.env.example` 为 `.env` 后按需修改。

## 密码学构造（完整规范）

哈希算法：**SHA-256**（32 字节），输出均以小写十六进制（64 字符）传输。

### 1. 叶展开

- 标量（字符串、数字、布尔、`null`）是叶子；
- 空对象 `{}` 与空数组 `[]` 也是叶子；
- 非空对象/数组递归展开，不出现在叶子集合中；
- 每个叶子的路径是 RFC 6901 指针字符串（如 `/subject/age`、`/a/0`、
  根标量为 `""`；键中的 `~` 编码为 `~0`、`/` 编码为 `~1`）。

### 2. 规范 JSON 值

同一逻辑值必须有唯一字节表示，采用如下规范形式：

- UTF-8 编码、无多余空白；
- 对象成员按键名的 Unicode 码点序排列（递归）；
- 整数最短十进制；浮点使用最短往返表示（Python `repr`）；
  拒绝 `NaN` / `Infinity`（不是合法 JSON 值）；
- 字符串仅做必要转义（`"`、`\`、控制字符，及 `\b \f \n \r \t`）；
- 空容器的规范值就是 `{}` 或 `[]`。

### 3. 叶哈希（长度前缀编码）

```
leaf_digest = SHA256(
    0x00                                  # 叶子域分隔标签
 || u32be(len(pointer_utf8)) || pointer_utf8
 || u32be(len(canonical_value_utf8)) || canonical_value_utf8
)
```

长度均为 4 字节大端无符号整数。标签 `0x00`/`0x01` 做域分隔，
防止叶子与内部节点作为哈希原像互相混淆。

### 4. 可复现 Merkle 根

1. 全部叶哈希按**路径字符串排序**（Unicode 码点序）；
2. 自左向右两两配对：`parent = SHA256(0x01 || left || right)`；
3. 某层节点数为**奇数时复制末节点**再配对（如 3 个叶子 →
   `H(L0,L1)` 与 `H(L2,L2)`，再向上配对）；
4. 恰有一个叶子时，根即为该叶哈希本身（不存在配对层）；
5. 根只取决于文档内容，与披露哪些路径无关（同一文档两次提交根相同）。

### 5. 包含证明（标准格式）

每个被披露叶子附带从叶到根的兄弟节点序列，每步为
`{"side": "left"|"right", "sibling": "<hex32>"}`：

- `side="right"`：`h = H(h, sibling)`；
- `side="left"`：`h = H(sibling, h)`；
- 单叶树证明为空数组 `[]`。

验证时由「提交的路径 + 提交的值」**重新计算叶哈希**，再按证明逐层折叠，
与提交的根比较。因此：

- 篡改**值** → 叶哈希变 → 根不匹配；
- 篡改**路径** → 叶哈希变（路径在原像中）→ 根不匹配；
- 篡改/替换任意**兄弟哈希**或其方向 → 折叠结果变 → 根不匹配；
- 篡改**根** → 与重算结果不相等。

验证接口不接收也无法接触原始文档。

### 6. 紧凑多叶证明（`proofMode: "compact"`）

多路径披露时，标准格式会在各叶的 `proof` 里**重复携带相同的上层兄弟节点**
（相邻叶子还会把彼此当作 level 0 的兄弟）。紧凑格式把每个非披露树节点
按其树坐标**全局唯一化**：

- `/commit` 请求加 `"proofMode": "compact"`；
- 响应顶层给出 `leafCount`（建树时的叶子总数，验证方据此知道每层宽度）；
- 每个被披露叶只有 `{"path", "index", "value"}`，`index` 是该叶在
  **按路径排序后的叶集合中的下标**（即建树下标）；
- 兄弟节点集中放在顶层 `proofNodes`，每项
  `{"level": <int>, "index": <int>, "hash": "<hex32>"}`，
  以 `(level, index)` 唯一标识并按 level、再按 index 排序；
  **同一节点只出现一次**，不再为各叶重复；
- `level` 从叶层起算为 0；根所在的最后一层不会出现在 `proofNodes` 中；
- 奇数层末节点与自身配对（`H(L,L)`），该节点**不需要**也**不允许**
  出现在 `proofNodes`；
- 披露全部叶子时 `proofNodes` 为空数组；单叶文档同样为空。

验证方仅凭「根 + 披露值 + 紧凑证明」**自底向上**重建根：每层已知节点
（披露叶、proof 节点、或由两个已知子节点折出的父节点）两两配对，
奇数层复制末节点，逐层向上直到根。结构校验失败返回 `400 BAD_PROOF` 并带
精确 `loc`：

| 结构错误 | `loc` 示例 |
| --- | --- |
| 叶下标越界 / 重复 | `leaves[1].index` |
| 节点层级越界（含给出根层） | `proofNodes[2].level` |
| 节点层内下标越界 | `proofNodes[2].index` |
| 奇数层末节点被携带 | `proofNodes[2]` |
| 节点与披露叶重合 | `proofNodes[2]` |
| `(level,index)` 重复 | `proofNodes[2]` |
| 缺少必需节点 | `proofNodes`（`detail.missing` 列出全部缺失项） |
| 携带多余节点 | `proofNodes[2]`（`detail.extraneous` 列出全部多余项） |
| 哈希不是 64 位十六进制 | `proofNodes[0].hash` |

结构合法但折叠出的根 ≠ 提交根（篡改路径、值或节点哈希）仍返回
`422 ROOT_MISMATCH`。`/verify` 根据请求体**自动识别**两种格式：
含 `proofNodes`/`leafCount` 即为紧凑格式，否则按标准格式逐叶验证。

### 7. 叶级差异（`POST /diff`）

`/diff` 对 `before`、`after` 两份文档分别执行同一套叶展开：标量、`null`、
空对象 `{}`、空数组 `[]` 都是叶子，根标量/根空容器使用空指针 `""`。随后按
RFC 6901 路径字符串排序并对齐两侧叶子：

- 仅 `after` 有 → `added`；
- 仅 `before` 有 → `removed`；
- 两侧都有但**规范 JSON 字节不同** → `changed`（例如 `1` 与 `1.0`、
  `true` 与 `1` 均视为不同）；
- 每个条目包含 `path`、`beforeValue`、`afterValue`、
  `beforeFingerprint`、`afterFingerprint`；
- 缺失侧的值与指纹均为 JSON `null`；实际 JSON `null` 值不缺失，其指纹是
  规范值 `null` 的 SHA-256；
- 指纹为 `SHA256(canonical_value_bytes)` 的小写十六进制；
- `added`、`removed`、`changed` 三个数组分别按 `path` 稳定排序。

可选 `paths` 过滤器只接受两侧叶路径并集中的**精确叶路径**：路径在任一侧是叶即可
（用于报告 added/removed；即使另一侧同一路径被替换成非空容器也允许）；若两侧都不是
叶子，且在任一侧解析为非空容器，则返回 `PATH_NOT_LEAF`；两侧都不存在返回
`PATH_NOT_FOUND`；重复项返回 `DUPLICATE_PATH`（`loc` 指向后出现的
`paths[i]`）。省略 `paths` 比较所有叶；显式传 `[]` 表示不选择任何路径。

### 8. RFC 6902 风格补丁（`POST /patch`）

`/patch` 接收一份 `document` 与按数组顺序排列的 `operations`，在文档的**深拷贝**上逐项
执行 [RFC 6902](https://www.rfc-editor.org/rfc/rfc6902) 风格操作，支持三种 `op`：

| `op` | 对象成员 | 数组元素 |
| --- | --- | --- |
| `add` | 成员不存在则新增，**已存在则替换其值** | 在指定下标前插入；`-` 或下标 `length` 表示末尾追加 |
| `remove` | 删除已存在的成员 | 删除已存在的元素，后续元素前移 |
| `replace` | 覆盖**已存在**成员的值 | 覆盖**已存在**下标的元素 |

规则细节：

- `path` 是 RFC 6901 指针（`~0`/`~1` 转义规则相同）；数组下标必须是无前导零的十进制
  整数（`"0"` 合法，`"01"` 非法）；
- `-` 是 RFC 6902 的数组末尾记号，**仅允许用于 `add`**；`remove`/`replace` 使用 `-`
  或越界下标都返回 `PATH_NOT_FOUND`；
- `add`/`replace` 必须携带 `value`（可以是任意 JSON 值，含嵌套容器，会经过与其他接口
  相同的 JSON 值校验）；`remove` 不接受 `value`（多余字段会被忽略）；
- 空指针 `""` 表示整个文档：允许 `add`/`replace`（替换根值）；`remove` 根文档无意义，
  返回 `INVALID_REQUEST`；
- 操作严格按顺序执行——后一操作的路径是否合法，取决于前一操作执行后的文档状态；
- **先完整校验、再整体生效**：所有操作的结构（类型、`op`、指针语法、必填字段）先于
  任何修改完成校验；执行阶段任一项的路径、父容器或数组下标非法，整份请求失败，
  错误统一为 `{error: {code, message, loc, detail?}}`，其中 `loc` 精确定位到
  `operations[i]`（结构字段错误为 `operations[i].op` / `.path` / `.value`），
  响应不包含 `document`/`changedPaths`，输入文档不被修改。

成功响应包含补丁后的 `document` 与 `changedPaths`：后者按与 `/commit`、`/diff` 相同的
叶模型计算两侧叶路径的差异——被删除的叶、新增的叶、两侧都存在但**规范 JSON 值不同**
的叶（`1`→`1.0` 视为变化）——并按 RFC 6901 指针字符串排序。补丁在规范意义上没有改变
文档时（例如以相同值 `replace`），`changedPaths` 为空数组；`operations` 为空数组
时原样返回文档且 `changedPaths` 为 `[]`。

### 9. 确定性叶路径脱敏（`POST /redact`）

`/redact` 接收一份 `document` 与一组 `rules`，在文档的**深拷贝**上按规则对
**RFC 6901 叶路径**（与 `/commit`、`/diff` 相同的叶模型：标量、`null`、
空对象 `{}`、空数组 `[]` 都是叶子，因此**空对象和空数组也可以被脱敏**）执行
确定性脱敏：

```json
{ "pattern": "/subject/name", "action": "mask", "value": "***" }
{ "pattern": "/emails/*",      "action": "drop" }
{ "pattern": "/subject/**",    "action": "mask", "value": "[redacted]" }
```

`pattern` 是 RFC 6901 片段（`~0`/`~1` 转义规则相同），其引用记号（reference token）
支持三种形式：

| 记号 | 含义 |
| --- | --- |
| 字面量（如 `subject`、`0`） | 与同位置的记号精确相等（数组下标就是普通字面量记号） |
| `*` | 匹配**恰好一层**：任意对象成员名或数组下标 |
| `**` | 匹配**任意层（含 0 层）**：可出现在任意位置 |

要点：

- 非空对象/数组不是叶子，不会被模式匹配；`**` 不会命中中间的非空容器，只会命中其
  叶后代；
- `/a/**` 既匹配 `/a` 本身（`/a` 是叶时，`**` 消费 0 个记号），也匹配其任意深度的
  叶后代；`/**` 匹配文档内所有叶（含根叶），裸 `**` / `*` 不以 `/` 开头，属于
  `POINTER_SYNTAX`；
- 空指针 `""` 只匹配整文档这一根叶（根标量、根 `{}`、根 `[]`）；
- `*`/`**` 的判定发生在 RFC 6901 反转义之后，因此名字恰好叫 `*` 或 `**` 的对象成员
  无法用通配模式表达（其他接口仍可用精确指针访问）。

`action`：

| action | 对象成员 | 数组元素 |
| --- | --- | --- |
| `mask` | 把叶值替换为规则的常量 `value`（**必填**，任意 JSON 值，每个命中点深拷贝一份） | 同左 |
| `drop` | **移除该成员** | **置为 `null`**（保留下标、不移动后续元素） |

根叶（空指针或 `/**` 命中）被 `drop` 时没有父容器可删除，整个文档变为 JSON `null`。
`drop` 不需要 `value`（多余字段忽略）。

**同一叶被多条规则命中时按如下确定性优先级选一条生效规则：**

1. 字面量记号**数量更多**的规则胜出；
2. 并列时 `*` 记号数量更多的规则胜出（等价于 `**` 更少）；
3. 仍并列时按规则在 `rules` 数组中的**先后顺序**，先者胜。

规则先全部解析并校验（类型、`action`、模式语法、mask 必填 `value`），**再整体生效**：
任一模式非法时整份请求失败，不返回部分脱敏结果。模式语法错误使用统一错误码
`POINTER_SYNTAX`，`loc` 精确定位到 `rules[i].pattern`；结构字段错误为
`INVALID_REQUEST`，定位到 `rules[i]`（或 `.action` / `.value`）。

成功响应：

```json
{
  "document": { …脱敏后的文档… },
  "applied": [
    { "path": "/emails/0", "action": "drop",   "rule": 1 },
    { "path": "/emails/1", "action": "drop",   "rule": 1 },
    { "path": "/subject/age",  "action": "mask", "rule": 2 },
    { "path": "/subject/name", "action": "mask", "rule": 0 }
  ]
}
```

`applied` 每项给出被脱敏叶的 `path`、生效 `action` 与生效规则下标 `rule`
（从 0 开始，便于核对多规则竞争结果），并按 RFC 6901 指针字符串排序。
没有任何模式命中叶时 `document` 原样返回、`applied` 为 `[]`；`rules: []`
是合法的空操作。

注意：被匹配的叶子互为反链（一个叶不可能是另一个叶的祖先），各条脱敏互不影响路径，
因此结果与执行顺序无关；`drop` 掉容器内全部成员后，中间容器以空壳 `{}`/`[]` 保留
（它原本不是叶子，规则并未直接作用于它）。

## API

所有请求/响应均为 JSON。错误响应统一形如：

```json
{ "error": { "code": "ERROR_CODE", "message": "…", "loc": "leaves[0].proof[1].sibling" } }
```

`loc` 指向出错字段（含数组下标），用于定位篡改或非法输入；部分错误还带 `detail`。

### `POST /commit` — 提交并生成证明

请求：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 36, "isAdult": true, "nickname": null },
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {},
    "deep": { "a/b": { "tilde~key": [7] } }
  },
  "reveal": ["/subject/age", "/emails/1", "/empty", "/deep/a~1b/tilde~0key/0"]
}
```

响应（**仅含所选值及证明**，不会回显文档其他部分）：

```json
{
  "root": "9f0c…（64 位十六进制）",
  "leaves": [
    { "path": "/deep/a~1b/tilde~0key/0", "value": 7,
      "proof": [ {"side": "right", "sibling": "…"}, … ] },
    { "path": "/emails/1", "value": "ada@analytical.engine", "proof": [ … ] },
    { "path": "/empty", "value": {}, "proof": [ … ] },
    { "path": "/subject/age", "value": 36, "proof": [ … ] }
  ]
}
```

不带 `proofMode`（或显式 `"standard"`）时响应保持上述形状不变。

#### 紧凑模式（`"proofMode": "compact"`）

请求：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 36, "isAdult": true, "nickname": null },
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {}
  },
  "reveal": ["/subject/age", "/emails/1", "/empty"],
  "proofMode": "compact"
}
```

响应（7 个叶子按路径排序；兄弟节点按 `(level,index)` 去重后只有 2 个，
标准格式需要 3×若干步）：

```json
{
  "root": "f066f677ef752e68f8924da8a10ba44b2beb5a4e3ff9793e20cd38c3a3120607",
  "proofMode": "compact",
  "leafCount": 7,
  "leaves": [
    { "path": "/emails/1", "index": 1, "value": "ada@analytical.engine" },
    { "path": "/empty", "index": 2, "value": {} },
    { "path": "/subject/age", "index": 3, "value": 36 }
  ],
  "proofNodes": [
    { "level": 0, "index": 0,
      "hash": "10854943caa6ff2053c8e3c9c9b183b632d3a8fd04cbe94a1f518bdb5d2ab6b9" },
    { "level": 2, "index": 1,
      "hash": "a3a2a836c671f329a313d7f9493639bc7689d2bc3a2892be6f14c3657a1223e0" }
  ]
}
```

紧凑证明直接原样发给 `/verify`（不需要 `proofMode` 字段，服务自动识别）。

### `POST /diff` — 叶级变更报告

请求（`paths` 可省略；省略时比较两侧全部叶子的并集）：

```json
{
  "before": {
    "name": "Ada",
    "age": 36,
    "role": null,
    "metadata": {},
    "removed": "x"
  },
  "after": {
    "name": "Ada",
    "age": 37,
    "role": "owner",
    "metadata": {},
    "added": []
  },
  "paths": ["/age", "/role", "/removed", "/added"]
}
```

成功（`200`）：

```json
{
  "added": [
    {
      "path": "/added",
      "beforeValue": null,
      "afterValue": [],
      "beforeFingerprint": null,
      "afterFingerprint": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    }
  ],
  "removed": [
    {
      "path": "/removed",
      "beforeValue": "x",
      "afterValue": null,
      "beforeFingerprint": "ba2df4903a2c14e86dc3bcca58911b44ac1d2514b7227bf6eb08cfb978f55a1b",
      "afterFingerprint": null
    }
  ],
  "changed": [
    {
      "path": "/age",
      "beforeValue": 36,
      "afterValue": 37,
      "beforeFingerprint": "76a50887d8f1c2e9301755428990ad81479ee21c25b43215cf524541e0503269",
      "afterFingerprint": "7a61b53701befdae0eeeffaecc73f14e20b537bb0f8b91ad7c2936dc63562b25"
    },
    {
      "path": "/role",
      "beforeValue": null,
      "afterValue": "owner",
      "beforeFingerprint": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
      "afterFingerprint": "622f06cbcd2e7e61637e17212e714e6c721d2dd5a9f7bc1aec911b65b34edcf5"
    }
  ]
}
```

未选择的路径不会出现在结果中。空文档、空容器和根标量按叶规则处理：
`{}` 与 `[]` 的根路径都是 `""`；空文档之间比较得到三个空数组。

过滤器错误沿用统一错误体，例如提交非叶容器路径：

```json
{ "error": {
    "code": "PATH_NOT_LEAF",
    "message": "path '/subject' is a non-empty container in 'before'; paths must be exact leaf paths",
    "loc": "paths[0]",
    "detail": { "path": "/subject", "leafChildren": ["/subject/age", "/subject/name"] }
} }
```

### `POST /patch` — 顺序执行 RFC 6902 补丁

请求（`operations` 按数组顺序执行；数组可用下标插入或用 `-` 追加）：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 36 },
    "emails": ["ada@example.com"],
    "role": null
  },
  "operations": [
    { "op": "replace", "path": "/subject/age", "value": 37 },
    { "op": "add", "path": "/emails/-", "value": "ada@analytical.engine" },
    { "op": "add", "path": "/subject/title", "value": "Countess" },
    { "op": "remove", "path": "/role" }
  ]
}
```

成功（`200`）：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 37, "title": "Countess" },
    "emails": ["ada@example.com", "ada@analytical.engine"]
  },
  "changedPaths": [
    "/emails/1",
    "/role",
    "/subject/age",
    "/subject/title"
  ]
}
```

非法路径/父容器/数组下标时，整份请求失败且不返回任何补丁结果，`loc` 指向出错的
操作（先做结构校验，再按顺序执行，任何一步失败都不会产生部分结果）：

```json
{ "error": {
    "code": "PATH_NOT_FOUND",
    "message": "array index 5 out of range for 'replace' (length 1)",
    "loc": "operations[2]",
    "detail": { "path": "/emails/5" }
} }
```

### `POST /redact` — 确定性叶路径脱敏

请求（`pattern` 支持字面量、`*` 一层、`**` 任意层；`mask` 必须携带 `value`）：

```json
{
  "document": {
    "subject": { "name": "Ada Lovelace", "age": 36, "secret": "x" },
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {}
  },
  "rules": [
    { "pattern": "/subject/name", "action": "mask", "value": "***" },
    { "pattern": "/emails/*", "action": "drop" },
    { "pattern": "/subject/**", "action": "mask", "value": "[redacted]" }
  ]
}
```

成功（`200`）——`/subject/name` 命中规则 0 与 2，字面量更多的规则 0 胜出；
`/subject/age`、`/subject/secret` 由规则 2 掩码；数组元素被置为 `null` 而非移除：

```json
{
  "document": {
    "subject": { "name": "***", "age": "[redacted]", "secret": "[redacted]" },
    "emails": [null, null],
    "empty": {}
  },
  "applied": [
    { "path": "/emails/0", "action": "drop", "rule": 1 },
    { "path": "/emails/1", "action": "drop", "rule": 1 },
    { "path": "/subject/age", "action": "mask", "rule": 2 },
    { "path": "/subject/name", "action": "mask", "rule": 0 },
    { "path": "/subject/secret", "action": "mask", "rule": 2 }
  ]
}
```

非法模式沿用统一错误格式（`loc` 定位到具体规则的模式字段）：

```json
{ "error": {
    "code": "POINTER_SYNTAX",
    "message": "invalid escape sequence in token 'bad~x': '~' must be followed by '0' or '1'",
    "loc": "rules[1].pattern",
    "detail": { "pattern": "/bad~x" }
} }
```

### `POST /verify` — 无原文校验

请求体只需提交方给你的根与所选披露：

```json
{
  "root": "9f0c…",
  "leaves": [
    { "path": "/subject/age", "value": 36,
      "proof": [ {"side": "left", "sibling": "…"} ] }
  ]
}
```

成功（`200`）：

```json
{ "valid": true, "root": "9f0c…",
  "leaves": [ {"path": "/subject/age", "value": 36} ] }
```

紧凑格式的 `/verify` 请求体即 `/commit` 紧凑响应去掉 `proofMode`
（保留 `root`、`leafCount`、`leaves`、`proofNodes`），成功时响应多一个
`"proofMode": "compact"`：

```json
{
  "root": "f066f677ef752e68f8924da8a10ba44b2beb5a4e3ff9793e20cd38c3a3120607",
  "leafCount": 7,
  "leaves": [
    { "path": "/emails/1", "index": 1, "value": "ada@analytical.engine" },
    { "path": "/empty", "index": 2, "value": {} },
    { "path": "/subject/age", "index": 3, "value": 36 }
  ],
  "proofNodes": [
    { "level": 0, "index": 0, "hash": "108549…b9" },
    { "level": 2, "index": 1, "hash": "a3a2a8…e0" }
  ]
}
```

紧凑证明的**结构错误**（缺节点、多节点、下标越界/重复、奇数层末节点等）
是 `400 BAD_PROOF`，例如漏掉一个必需节点：

```json
{ "error": {
    "code": "BAD_PROOF",
    "message": "compact proof is missing the required sibling node at level 0, index 3 …",
    "loc": "proofNodes",
    "detail": { "missing": [ {"level": 0, "index": 3} ] }
} }
```

任一条证明内容失败（`422`，其余结构性输入错误为 `400/413`）：

```json
{ "error": {
    "code": "ROOT_MISMATCH",
    "message": "proof for path '/subject/age' failed: recomputed root does not match …",
    "loc": "leaves[0]",
    "detail": { "path": "/subject/age", "committedRoot": "…",
                "hint": "the path, the value, a sibling hash or the root was tampered with" }
} }
```

### 错误码一览

| HTTP | code | 触发场景（示例） |
| --- | --- | --- |
| 400 | `INVALID_JSON` | 请求体不是合法 JSON、含重复对象键 |
| 400 | `INVALID_REQUEST` | 缺字段、类型错误（含 `/redact` 规则缺 `pattern`/`action`/`value`、非法 `action`） |
| 400 | `POINTER_SYNTAX` | 指针非法（如尾随 `~`、不以 `/` 开头；含 `/redact` 的非法模式，`loc` 为 `rules[i].pattern`） |
| 400 | `PATH_NOT_FOUND` | 文档中不存在该路径（含数组越界、`remove`/`replace` 命中不存在的成员、对 `-` 做非 `add` 操作等） |
| 400 | `PATH_NOT_LEAF` | 路径是非空容器（`/commit` 的 `reveal` 或 `/diff` 的 `paths`）；`detail.leafChildren` 给出可选的叶路径 |
| 400 | `DUPLICATE_PATH` | 同一指针重复提交 |
| 400 | `EMPTY_REVEAL` | 披露列表为空 |
| 400 | `UNSUPPORTED_VALUE` | `NaN`/`Infinity`、孤立代理项等（含 `/patch` 的 `operations[i].value`） |
| 400 | `BAD_HASH_ENCODING` | 根或兄弟哈希不是 64 位十六进制 |
| 400 | `BAD_PROOF` | 证明结构错误：标准格式 side 非法、兄弟长度不对；紧凑格式叶/节点下标越界或重复、节点缺失或多余、携带奇数层末节点等（均有精确 `loc`） |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 `MAX_BODY_BYTES` |
| 422 | `ROOT_MISMATCH` | 证明有效但重算根 ≠ 提交根（路径/值/兄弟/节点哈希/根被篡改） |

### 其他

- `GET /healthz` → `{"status":"ok"}`
- `POST /diff` → 叶级 added/removed/changed 变更报告
- `POST /patch` → 顺序执行 RFC 6902 add/remove/replace 补丁，返回 patched 文档与 changedPaths
- `POST /redact` → 按字面量/`*`/`**` 模式对叶路径执行 mask/drop 确定性脱敏，返回脱敏文档与按路径排序的 applied
- `GET /` → 服务与端点元信息
- `GET /docs` / `GET /openapi.json` → OpenAPI 文档

## 快速试用

```bash
# 1) 提交文档，只披露 /subject/age
curl -s localhost:8000/commit -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["a@x", "b@y"]},
  "reveal": ["/subject/age"]
}' | tee /tmp/commit.json

# 2) 用返回的 root + leaves 验证（不提交 document）
curl -s localhost:8000/verify -H 'content-type: application/json' \
  -d "{\"root\": $(jq .root /tmp/commit.json), \"leaves\": $(jq .leaves /tmp/commit.json)}"

# 3) 把值改成 37 再验证 → 422 ROOT_MISMATCH，loc=leaves[0]

# 4) 报告两份文档的叶级差异；省略 paths 时比较所有叶子
curl -s localhost:8000/diff -H 'content-type: application/json' -d '{
  "before": {"subject": {"name": "Ada", "age": 36}},
  "after": {"subject": {"name": "Ada", "age": 37, "added": null}},
  "paths": ["/subject/age", "/subject/added"]
}'

# 4b) 按顺序执行 RFC 6902 add/remove/replace 补丁；- 表示数组末尾
curl -s localhost:8000/patch -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["ada@x"]},
  "operations": [
    {"op": "replace", "path": "/subject/age", "value": 37},
    {"op": "add", "path": "/emails/-", "value": "ada@engine"},
    {"op": "remove", "path": "/subject/name"}
  ]
}'

# 4c) 确定性脱敏：字面量优先于 *、* 优先于 **，同优先级按规则顺序；
#     mask 替换为常量，drop 删成员 / 把数组元素置 null
curl -s localhost:8000/redact -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["a@x", "b@y"]},
  "rules": [
    {"pattern": "/subject/name", "action": "mask", "value": "***"},
    {"pattern": "/emails/*", "action": "drop"},
    {"pattern": "/subject/**", "action": "mask", "value": "[redacted]"}
  ]
}'
# => {"document": {"subject": {"name": "***", "age": "[redacted]"},
#                  "emails": [null, null]},
#     "applied": ["/emails/0" drop, "/emails/1" drop,
#                 "/subject/age" mask(rule 2), "/subject/name" mask(rule 0)]}

# 5) 紧凑多叶证明：加 "proofMode": "compact"，返回 leafCount、
#    每个叶的 index，以及按 (level,index) 去重的 proofNodes
curl -s localhost:8000/commit -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["a@x", "b@y"]},
  "reveal": ["/subject/name", "/subject/age"],
  "proofMode": "compact"
}'
# /verify 自动识别紧凑格式：把紧凑响应的 root/leafCount/leaves/proofNodes
# 原样回传即可（无需携带 document 或 proofMode）。
```

## 本地开发与测试（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q          # 170+ 个测试
python3 -m uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py        # FastAPI 路由：/commit、/verify、/diff、/patch、/redact、错误处理
  diff.py        # 叶级路径对齐、规范值指纹与 added/removed/changed
  patch.py       # RFC 6902 add/remove/replace 引擎（有序执行、整体校验、changedPaths）
  redact.py      # 叶路径脱敏引擎（字面量/*/** 模式匹配、mask/drop、确定性优先级）
  tree.py        # 叶展开、叶哈希/节点哈希、排序建树、标准与紧凑证明
  pointers.py    # RFC 6901 指针解析与寻址
  canonical.py   # 规范 JSON 序列化
  jsonio.py      # 严格 JSON 解析（拒绝重复键、错误定位）
  validation.py  # 提交值校验（非有限数、非 JSON 类型等）
  openapi.py     # 两种证明格式与 /diff、/patch 的 OpenAPI 模型与文档合并
  errors.py      # 带 code/loc 的 API 错误类型
tests/           # 哈希/树单元测试 + API 端到端（含全部篡改、去重、结构错误、补丁与脱敏场景）
Dockerfile
docker-entrypoint.sh   # 启动时安装依赖并启动 uvicorn
docker-compose.yml
requirements.txt
```
