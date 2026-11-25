# 手工书装订跨册配页采录台

给小批量盲文和大字版装订工作室使用的离线台账。工作室可记录印张、纸材发料、配帖、书脊抽检以及套册配件和交付。样例为虚构书目与读者版本。

要求 Python 3.11 或更新版本。进入本项目目录即可运行，无需安装依赖。

```text
python -m bindery_gather --help
python -m bindery_gather demo --db workshop.sqlite
python -m bindery_gather audit --db workshop.sqlite
python -m unittest discover -s tests -v
```

分阶段观察交付前后，另用空库运行以下命令。`apply` 自动初始化 SQLite，也可先运行 `init --db staged.sqlite`。

```text
python -m bindery_gather apply --db staged.sqlite fixtures/workshop.json
python -m bindery_gather issue-report --db staged.sqlite issue-11
python -m bindery_gather paper-stock --db staged.sqlite paper-heavy
python -m bindery_gather run-status --db staged.sqlite run-b1
python -m bindery_gather kit-status --db staged.sqlite kit-atlas 60
python -m bindery_gather apply --db staged.sqlite fixtures/late-sample.json
python -m bindery_gather kit-status --db staged.sqlite kit-atlas 60
```

| 工位及事件 | 业务规则 |
| --- | --- |
| 制版 `edition`、`volume`、`signature`、`accessory`、`seal` | 书目、读者规格及修订号绑定版本。封版前逐卷校验所有帖槽和从第 1 页开始的连续页段，封版后冻结配件定额和版样。 |
| 纸材 `paper`、`permit`、`issue` | 领料受批次余量、阅读规格及纹向约束。替代等级须持有这批纸和这款纸帖的审批，还受累计发料上限约束。 |
| 折页 `fold` | 第一张为最外印张。封版规定每张左右页对、翻边和版头方向，扫描输入可乱序。有效形状但错页或错向的整帖隔离计废，仍扣除所用印张。印张序列号与帖号不重复。 |
| 配帖 `gather` | 按卷的槽序扫描实物折帖。废帖、重复帖、错槽和其他版本均被拒绝。每件合格帖仅可占一个书坯。 |
| 装订 `run`、`bind`、`sample`、`certify` | 同版本书坯进入装订批。每本至多一个脊检结果，拉力不低于下限且偏斜不超过上限才记通过。足量且全合格才出证并冻结批次成员，补录失败样本使整批隔离。 |
| 套册 `order`、`kit`、`attach`、`detach` | 按订单额度建套，每卷放一本匹配版本的已装订书。可在交付前撤下预装书并重新装册。 |
| 配件 `accessory-lot`、`bridge`、`allocate`、`return` | 每卷分别补足对应 SKU 与数量。临时许可绑定套册、来源批和目标定额，拨件及交付均须在授权分钟闭区间内。未交付的拨件归还后释放实物余量和许可占用。 |
| 交付 `deliver` | 核对每卷实际书的装订批已放行、配件数量及授权有效性。成功时保留交付分钟和证据快照。迟到失败样本只改变当前复核状态，历史交付仍可查。 |

`fold` 的 `scans` 各包含 `serial`、`position`、`front`、`back`、`turn`、`head`。例如第 1–8 页的最外印张正面从左到右为 `[8,1]`，背面为 `[2,7]`。交付分钟使用同一作业时钟的非负整数。样本晚到以离线事件的入账先后判定。

每条输入有全库唯一 `id` 和 `op`。批次按数组顺序原子提交，完全相同的重试返回 `duplicate`。同事件号冲突或批内错误会回滚整批。纸、帖、书和配件分别有数量账或实物标识。`audit` 从事件重建临时数据库并比较各投影，输出重算的纸材、装订批、套册和历史快照。投影差异退出 1，业务拒绝或读取失败退出 2。

`fixtures/workshop.json` 含两卷盲文版、两张同帖、材料替代、废帖及一次跨册调拨。`late-sample.json` 触发已交付复核。测试亦用 `variants.json` 中的大字版与盲文第二版，以及 `rejections.json` 中的失效批次、误配和越期场景。

## 设备质检采集与补采

`qc-intake` 一次接收一个或多个本地设备导出，并按文件参数及行顺序原子入账。`qc-recover` 扫描目录第一层的 CSV 和 JSON 导出，只收集指定装订批中补采开始时缺少样本的书坯。对这些书坯，优先按采集分钟排序，同分钟按读数事件号排序。支持格式的文件解析失败会拒绝本次采集；目录中的其他文件扩展名会被跳过。

设备号、导出批号和读数号标识一条读数。同一来源的相同字段可重传，修改字段会触发事件号冲突。首条入账读数适用现有版本质检阈值。已有样本的额外读数仍保留原值、采集分钟、夹具及开口，供复核。交付报告跟随其装订批当前结论，交接证据仍按交付时保存。`audit` 会重放原始读数和正式样本。

JSON 导出使用 `spine-readings-v1`，设备号和导出批号位于文件顶部。CSV 每行填写 `device,batch,reading,binding,minute,fixture,jaw_mm,pull,skew`。两种导出都使用作业时钟的分钟、整毫米开口和台账原有的书脊拉力、偏斜整数刻度。`binding` 填绑定事件号。`qc-status` 显示每册的实际纸批、张数和采集记录。传入 `--stations` 时，待采书坯会列出支持其读者规格的工位和各夹具的额定开口。

### 当班可执行采录单

`qc-status` 另接 `--shift` 班次文件（`qc-shift-v1`，必须与 `--stations` 同时给出）。班次输入按纸级独立标定每张厚度，给出每册可进工位的窗口、每台设备每个夹具的离散开口档位与防压余量，以及开班已经校好的设备、夹具和开口。厚度与档位统一用半毫米整数：标准纸标定 `2` 即每张 1 mm。

夹持厚度只按已绑定且尚无正式样本的册计算：沿 `book_pieces` 找实际配入的合格帖，按实物 `sheets.serial` 计数并取发料纸批的等级，废帖和帖槽名义用纸都不参与；任一纸级未标定则该册不计算厚度并不给档位。某档可夹持当且仅当 `册厚 ≤ 档位 ≤ 册厚 + 防压余量` 且不超夹具额定开口、夹具支持该册读者规格。

每窗口只可采一册、执行一次空夹校验或闲置。更换设备、夹具或开口档的任一项都要独占一个窗口做空夹校验，校验窗口不能同时采读数，校好的组合跨窗口携带，直到下一次换装。枚举只读，不写质检样本：返回整班所有不被更大可采集合包含的采录单，每张单给出按窗口可复核的操作、可采册号集合，以及其他在册未覆盖的原因——`uncalibrated-grade`（纸级未标定）、`no-physical-jaw-level`（无物理可用档）或 `cannot-fit-in-windows`（有档但窗口预算不足以完成换装和采录）。空班或整班无可采册时返回全闲置见证和 `no-capturable-binding`。

```text
python -B -m bindery_gather qc-status --db ../qc.sqlite run-b1 \
  --stations fixtures/qc-stations.json --shift fixtures/qc-shift-night.json
```

给一个新数据库依次运行下列命令。`qc-bench.json` 一次导入两册读数。对主批卷二补采时会导入两份离线记录，较早的偏斜和拉力读数触发装订批隔离与已交付套册复核，较晚的复测仍可在采集报告中查看。备用文件中的另一批读数可通过该批单独补采。

```text
python -B -m bindery_gather apply --db ../qc.sqlite fixtures/workshop.json
python -B -m bindery_gather apply --db ../qc.sqlite fixtures/variants.json
python -B -m bindery_gather qc-intake --db ../qc.sqlite fixtures/qc-bench.json
python -B -m bindery_gather qc-status --db ../qc.sqlite run-b1 --stations fixtures/qc-stations.json
python -B -m bindery_gather qc-recover --db ../qc.sqlite run-b1 fixtures/qc-inbox
python -B -m bindery_gather qc-status --db ../qc.sqlite run-b1
python -B -m bindery_gather kit-status --db ../qc.sqlite kit-atlas 60
python -B -m bindery_gather audit --db ../qc.sqlite
```
