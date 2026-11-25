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

两包同清单纸材的扫码采录可用 `fixtures/workshop.json` 的前 9 条封版事件叠加 `fixtures/packed-paper.json`：

```text
python -m bindery_gather apply --db packed.sqlite fixtures/packed-paper.json
python -m bindery_gather manifest --db packed.sqlite pack-b1
python -m bindery_gather paper-review --db packed.sqlite b1-s11 pack-p pack-q
python -m bindery_gather paper-balance --db packed.sqlite pack-p b1-s11
```

| 工位及事件 | 业务规则 |
| --- | --- |
| 制版 `edition`、`volume`、`signature`、`accessory`、`seal` | 书目、读者规格及修订号绑定版本。封版前逐卷校验所有帖槽和从第 1 页开始的连续页段，封版后冻结配件定额和版样。 |
| 纸材 `paper`、`permit`、`issue` | 领料受批次余量、阅读规格及纹向约束。替代等级须持有这批纸和这款纸帖的审批，还受累计发料上限约束。 |
| 扫码纸包 `paper-scan`、`label`、`confirm`、`issue`（`basis="tentative"`） | 两包纸共享同一装箱清单，清单保证一包标准档、一包偏厚档。扫码仅保留包号、实扫张数、格式、纹向和候选等级；两包的候选等级须共同解释清单，不能各取最有利等级。等级未定前的暂发凭据必须对每种尚相容的归属都合法，并在每种归属下分别占用标准料余量或代料额度；两种余额分开核算，不合并成通用余额。补录可靠标签后只保留相容归属，既有发料与隔离事实不变。`confirm` 在等级唯一后把暂发并入正式账。 |
| 折页 `fold` | 第一张为最外印张。封版规定每张左右页对、翻边和版头方向，扫描输入可乱序。有效形状但错页或错向的整帖隔离计废，仍扣除所用印张。印张序列号与帖号不重复。 |
| 配帖 `gather` | 按卷的槽序扫描实物折帖。废帖、重复帖、错槽和其他版本均被拒绝。每件合格帖仅可占一个书坯。 |
| 装订 `run`、`bind`、`sample`、`certify` | 同版本书坯进入装订批。每本至多一个脊检结果，拉力不低于下限且偏斜不超过上限才记通过。足量且全合格才出证并冻结批次成员，补录失败样本使整批隔离。 |
| 套册 `order`、`kit`、`attach`、`detach` | 按订单额度建套，每卷放一本匹配版本的已装订书。可在交付前撤下预装书并重新装册。 |
| 配件 `accessory-lot`、`bridge`、`allocate`、`return` | 每卷分别补足对应 SKU 与数量。临时许可绑定套册、来源批和目标定额，拨件及交付均须在授权分钟闭区间内。未交付的拨件归还后释放实物余量和许可占用。 |
| 交付 `deliver` | 核对每卷实际书的装订批已放行、配件数量及授权有效性。成功时保留交付分钟和证据快照。迟到失败样本只改变当前复核状态，历史交付仍可查。 |

`fold` 的 `scans` 各包含 `serial`、`position`、`front`、`back`、`turn`、`head`。例如第 1–8 页的最外印张正面从左到右为 `[8,1]`，背面为 `[2,7]`。交付分钟使用同一作业时钟的非负整数。样本晚到以离线事件的入账先后判定。

每条输入有全库唯一 `id` 和 `op`。批次按数组顺序原子提交，完全相同的重试返回 `duplicate`。同事件号冲突或批内错误会回滚整批。纸、帖、书和配件分别有数量账或实物标识。`audit` 从事件重建临时数据库并比较各投影，输出重算的纸材、装订批、套册和历史快照。投影差异退出 1，业务拒绝或读取失败退出 2。

`fixtures/workshop.json` 含两卷盲文版、两张同帖、材料替代、废帖及一次跨册调拨。`late-sample.json` 触发已交付复核。`packed-paper.json` 在前 9 条封版事件后演示两包纸的扫码采录、暂发、补标与隔离。测试亦用 `variants.json` 中的大字版与盲文第二版，以及 `rejections.json` 中的失效批次、误配和越期场景。
