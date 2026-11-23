# 精密计量卷归档台

精密计量资料室用这套工具归还供应商原卷，并在断源后按原号交付获准的字节窗口。需要 Python 3.11，仅使用标准库。命令从项目根目录运行。

```text
python3 -B check_project.py
python3 -B -m unittest discover -v
python3 -B demo.py
python3 -B desk.py --help
```

`check_project.py` 由运行环境提供，直接使用 unittest 也可运行整个 `tests` 包。演示使用临时工作目录，读取 `examples/previous.json` 和 `examples/current.json` 中的固定供应商样例，生成前一班记录并办理本班交接。

## 当班操作

1. 执行 `python3 -B desk.py --root work init`。重复初始化可继续使用原记录。将供应商原文件放入 `work/supplier`。端口按文件及本次装载标识管理读权，装载期间供应商文件应保持原样。
2. 准备清单，再执行 `python3 -B desk.py --root work mount P p.bin manifest.json`。清单为 JSON 对象，包含非负整数 `length`、测次号数组 `runs`，以及 `segments` 数组。每段包含 `id`、`run`、整数 `start` 和 `end`、64 位小写十六进制 `sha256`。段必须在卷内且无重叠，允许卷上有未登记位置。原段摘要针对完整原段计算。
3. 用 `python3 -B desk.py --root work segments 1 --run R` 查询实例 1 的测次 R。每次装卷返回独立的整数 `mount_id`。显示名 P 可以复用。
4. 用 `python3 -B desk.py --root work admit A 1 0 2` 申领实例 1 的 `[0,2)`。坐标单位为字节，终点可以恰好等于卷长。窗口须由已登记原段连续解释。首执表示正式接纳，原号同参重投返回相同首执，修改原参会报冲突。
5. 用 `python3 -B desk.py --root work fulfill A` 领取或复领。输出包括字节整数数组 `body`、逐段 `provenance` 和 `receipt`。同值字节有各自的坐标出处。在线读窗先校验完整原段的长度和 SHA-256，再裁切交付。
6. 用 `python3 -B desk.py --root work retire RETURN-P 1` 归还实例 1。接纳先关闭，已接纳窗口逐件保全，之后独立端口可以拔出原实例。若失败或失答，重开后重投原退卷号和实例。只有查到实际已拔才会返回成功回执。拔出后的原装载标识永久失去端口读权，模拟供应商文件留在端口一侧。等待领取人取件无需纳入退卷操作。
7. 用 `fulfill` 复领断源窗口。用 `python3 -B desk.py --root work report 1` 固定该卷的班报，以 `python3 -B desk.py --root work history 1` 读取班报 1。覆盖按有序半开区间合并并保留空档。逐单档案和首执偏差单列，即使另一个窗口覆盖了相同位置。

## Python 接口及故障处理

```python
from desk import ArchiveDesk
from orders.intake import OrderRequest

with ArchiveDesk('work') as desk:
    request = OrderRequest('A', 1, 0, 2)
    receipt = desk.admit(request)
    window = desk.fulfill(receipt.order_id)
```

`ArchiveDesk` 还提供 `mount(label, source, manifest)`、`manifest(mount_id)`、`segments(mount_id, run_id=None)`、`retire(request_id, mount_id)`、`report(mount_id)` 和 `history(report_id)`。`prepare(request)` 与 `accept(request)` 可分开调用，适用于初验后需要交班的情形。准备记录没有首执。`accept` 重新核对持久准备单及接纳关口。每个线程使用独立的 `ArchiveDesk`。

正常业务拒绝以 `storage.BusinessError` 的子类返回，非法参数可抛出 `ValueError`。失败的清单不发布段表。接纳写入或提交失败不会留下新的正式单或首执。写档失败时归还待办，原实例仍可由端口读取。端口无法报告在位或拔出时归还待确认。无正式单、断源缺档及损坏档案分别有领取错误。已固定的合法首执、退卷回执和旧报告可在重开后读取。

`archive.sqlite3` 保存业务状态、获准字节和班报。`port.sqlite3` 独立持久化模拟原卷读权，供应商全卷仅存于输入目录 `supplier`。端口、业务数据库及供应商文件都位于所选工作目录中。
