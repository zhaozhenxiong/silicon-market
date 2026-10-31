# v0.0.1 使用指南

本指南面向首次安装。项目入口和展示图见[README](../README.md)，版本范围见[发布说明](RELEASE_NOTES_v0.0.1.md)。

## 安装与启动

### 环境与平台

| 环境 | 要求与范围 |
| --- | --- |
| Python | 3.11或更新版本 |
| uv | 推荐依赖管理方式；不用uv时可使用下方pip方案 |
| 浏览器 | Playwright Chromium，用于本机登录和网页采集 |
| macOS / Windows / Linux | 网页核心、SQLite、导入导出与看板；Windows / Linux不含原生镜像和Apple Vision组件 |
| Node.js | 22或更新版本，仅本地手机运行器、可选适配器和Node测试需要 |
| 原生手机驱动 | 仅macOS可选；需系统支持iPhone镜像、兼容设备和本人授予权限 |

发行附件为`silicon-market-v0.0.1.zip`。解压后进入包含`pyproject.toml`的目录；也可克隆仓库获得源码。

### 推荐：uv

在项目根目录执行：

```bash
uv sync --locked
uv run playwright install chromium
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
```

打开`http://127.0.0.1:8765`。启动命令仅绑定本机地址，使用单个worker；停止时在终端按`Ctrl+C`。

macOS / Linux可用`./start.sh`，脚本优先执行`uv sync --locked`，否则使用venv与`requirements.txt`。`./start.sh 8877`可更换端口。macOS也可双击[启动硅价.command](../启动硅价.command)。这些入口均不自动启动手机进程。

### 备用：pip

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
```

第一次运行不会加载开发者的行情库。系统只建立数据库结构和暂停的示例任务，空曲线、待构建或数据不足是预期状态。选择180天窗口不会自动获得半年历史。

## 完成第一轮网页观测

1. 打开“数据采集”，使用本机登录入口，在可见浏览器中完成闲鱼扫码。系统权限、验证码和平台验证由本人处理。
2. 打开“监控任务”，创建明确关键词及分类。建议一个独立型号一个任务；名称只是备注，搜索及手机目标映射使用关键词或明确型号。
3. 检查内存容量、显存、手机存储等规格。先单次运行并查看日志、商品详情及排除原因，再决定是否启用定时监控。
4. 定时任务间隔至少30分钟，服务需要持续运行。退出应用或关机后不会继续采集；持久队列可在下次服务启动后继续处理。
5. 在“市场总览”按类别和型号查看自己的挂牌样本，在“商品样本”查看观测历史和质量标记。

每轮最多读取一页搜索、40件商品，并复查至多10件历史商品详情。平台限制、登录失效或结构变化会记录失败，不以空列表代替成功。搜索缺失、下架和未知状态均不能自动等同成交。

## 日常工作流

服务已启动时，macOS / Linux可使用[project.sh](../project.sh)：

| 命令 | 含义 |
| --- | --- |
| `./project.sh status` | 查看真实流程、质量状态及队列 |
| `./project.sh collect` | 将当前已启用真实任务持久入队；重复提交复用活动任务 |
| `./project.sh collect --task-id 1 --task-id 2 --wait` | 只等待指定任务；请用本机实际任务ID替换示例 |
| `./project.sh review-export` | 导出最多40条真实难例的材料包；没有待办时返回empty |
| `./project.sh review-import output/review_batches/实际建议.json` | 校验并保存建议，不自动修改正式资格 |
| `./project.sh audit-preview` | 预览当前版本的价格资格规则 |
| `./project.sh audit-apply` | 应用派生审计；保留原始标题、价格和观测时间 |
| `./project.sh audit-state` | 查看活动审计版本及覆盖 |
| `./project.sh audit-rollback` | 撤销活动审计层；保留历史决策供追溯 |
| `./project.sh rebuild-index` | 重建严格真实输入指数，不是图像估算指数 |
| `./project.sh daily --wait` | 等待网页采集成功，再执行规则审计、复核导出和质量允许的严格重算 |

默认`collect`不等待完成；等待超时或失败会明确返回未完成。默认`daily`只提交队列并准备预览及材料，带`--wait`才继续完整主线。它们都不会启动手机、调用模型或自动应用AI建议。

Windows的等价入口是`uv run python -m app.project_cli`，例如：

```powershell
uv run python -m app.project_cli status
uv run python -m app.project_cli collect --wait
```

自定义端口的全局参数必须放在子命令前：

```bash
./project.sh --base-url http://127.0.0.1:8877 status
```

`daily`所有采集与派生操作使用同一个本机服务。独立`audit-*`和`rebuild-index`是本地数据库操作，可显式指定`--db`；它们不会因`--base-url`自动切换数据库。

## 读懂估算与严格数据

### 挂牌与售出

“首次发现”是本系统第一次采到该商品的时间，不是平台发布时间。挂牌价是卖家标价，不是最后议价。售出占比的分母是指定首见窗口内的样本，分子只计有明确售出证据的商品，不代表全市场成交率。

没有过去的实际观测时，不能把当前价格按商品发布时间倒填半年。原始记录始终保留；质量排除项可以在商品页中查看。

### 图像估算行情趋势

“综合指数”页面的图像估算层使用原图的相对曲线：横轴是截图中的观察窗口，纵轴是0–100的图内相对高度。原图月日标签可以展示，年份只作近年估算元数据；没有金额刻度时不能还原人民币或价格涨幅。

趋势分由全段方向40%与近期曲线动量60%组成。“近期”是该图末段的相对窗口，不是固定7日或28日。趋势分50附近表示方向中性或平衡；综合指数还含报价一致度，不能把综合分50直接当作涨跌线。

估算综合名义权重为趋势70%、真实挂牌报价一致度30%，必须有有效趋势。来源不全时公开实际权重及缺失原因；**因子覆盖100%不等于来源覆盖100%**。热度图不算价格曲线，异常日期轴、失效原图和范围不匹配不计入当前分数。

任务、来源或审计变化后，页面会提示待重算。点击“重算估算指数”保存新版本；旧输入哈希、配置、曲线及结果仍保留。该篮子不随挂牌7/30/90/180天筛选裁切。完整公式见[截图趋势指数](IMAGE_TREND_INDEX.md)。

### 严格读数与价格指数

已核验App点保留实际日期、容量/成色范围、图片及两次读屏核对记录；口径为历史各日的近7日成交均价。人工稀疏归档与逐日核验分别计数，不能用少量点声明180天完整。

原有严格价格指数使用真实输入、共同连续基期和固定篮子；严格五因子综合分另有成熟队列及校准历史门槛。条件不足就留空。图像估算和AI建议均不能直接填入这些表或绕过质量门。

## 可选手机支线

网页核心不需要手机。原生镜像驱动仅在支持iPhone镜像的macOS上使用，Windows / Linux不提供此驱动。准备可用镜像设备、Apple Command Line Tools和Node.js 22+后，按需构建：

```bash
bash automation/build_ocr.sh
bash automation/build_native.sh
```

输出在`output/bin/`，发行包不附已编译的机器相关二进制。构建不请求权限、不截图、不点击。本人需在系统设置中授予实际运行程序辅助功能和屏幕录制权限，并自行完成手机解锁、App登录及平台验证。具体命令和限制见[原生手机驱动](NATIVE_MOBILE_DRIVER.md)。

手机任务必须来自**当前已启用且source=live的监控任务**。同目标去重，不从历史目录或全部商品扩成全型号任务；不能确定型号或多个经济范围尚待选择时显示needs_model / needs_scope。单一范围未经核对的截图可待审归档。手机进程默认不启动，须明确启动或恢复，旧all全目录模式已停用。镜像不在前台会等待，不自动抢回焦点。

推荐先截图、后处理图片：

1. 核对本机目标与App实际型号/筛选范围，使用已授权会话的采图执行器保存真实图片。
2. 经截图批次与登记接口保存文件SHA256、拍摄时间和冻结目标。未知日期为null，触点位置不代表日期。
3. 手机暂不可用时明确暂停采集，已有图像仍可继续处理。未核实范围进入queued_scope；图片登记不是价格核验。
4. 模型或人工结果另表保存为建议。只有月日时不能自动确定年份，parsed也不等于正式逐日成交价格。
5. 对合适的价格曲线进行相对形态提取、检查实测列覆盖与原图，再显式重算估算指数。

截图优先的`mobile_capture_worker.mjs`需要已有获准MCP/CUA镜像会话注入；网页不会自动为新安装建立这种会话。原生运行器也不保证从任意App布局无人值守完成所有型号。

开发者当前Codex会话中已使用`gpt-6.1-sol`实际读取保存图片，来源登记为codex_session。**该会话不随代码分发，独立CLI图片请求尚未验证成功**；不能把它描述为新机器已接通的常驻识图API。导航引导也只是绑定截图与当前任务的有限动作建议，需执行器重新核对，不表示模型会常驻监控手机。

### 截图与趋势接口索引

下列截图/趋势接口的写入与本机证据访问受回环地址、同源和JSON约束；接口细节可查看运行后的`http://127.0.0.1:8765/docs`。

| 接口 | 用途 |
| --- | --- |
| `GET /api/mobile/targets` | 当前启用任务的精确手机目标与跳过原因 |
| `POST /api/mobile/capture-batches` | 冻结截图批次的任务、目录及范围版本 |
| `GET /api/mobile/capture-batches/{id}/control` | 轻量检查授权和暂停状态，不操作设备 |
| `POST /api/mobile/captures` | 登记真实图片及哈希，不接受价格 |
| `GET /api/mobile/captures/frames` | 全部归档；pending接口仅列待识别队列 |
| `POST /api/mobile/captures/recognitions` | 保存带模型、来源adapter和证据的识别建议 |
| `POST /api/image-trends` | 登记已保存图片的相对曲线、轴原文和几何质量 |
| `GET /api/trend-indices` | 读取冻结估算指数；不自动创建版本 |
| `POST /api/trend-indices/rebuild` | 显式保存新的估算构建 |

这些接口不是平台私有行情API，也不自动提供模型调用。更完整的过程见[项目工作流程](PROJECT_WORKFLOW.md)。

## 数据与维护

| 本机位置 | 内容 |
| --- | --- |
| `data/market.sqlite3` | 原始观测、任务、截图队列、建议与指数版本 |
| `data/browser_profile/` | 本机浏览器资料；不要分享或提交 |
| `data/market_evidence/` | 私有原始截图证据，不随发行包提供 |
| `output/` | 本机构建和复核输出，不随发行包提供 |
| `docs/assets/v0.0.1/` | 专门挑选的公开展示图，不能作为安装数据导入 |

`XIANYU_DB_PATH`可指定数据库路径，`XIANYU_DATA_DIR`可指定网页采集资料目录；两者用途不同，配置后仍需确认服务和维护命令指向同一库。服务只用于本机个人工作区，没有多用户权限体系，不应直接暴露公网。

备份前停止服务，再备份自己的`data/`及需要保留的`output/`。新机器重新安装依赖、采集浏览器和可选原生组件，并重新登录；不要复制或共享他人的会话资料。

历史验收文档中的本机路径、行情数值和截图清单只用于记录当时验证，干净发行包不附这些原始数据。查看[公开素材说明](assets/README.md)了解文档截图的范围。

## 常见情况

| 现象 | 检查方向 |
| --- | --- |
| 页面无行情、指数为空 | 首次空库正常；先采集或导入，并查看覆盖与缺失原因 |
| 找不到Chromium | 用同一个Python环境执行`playwright install chromium` |
| 登录成功仍采集失败 | 查看平台验证和任务日志；本地保存会话不保证平台持续认可 |
| collect返回queued | 只代表持久排队；等待模式或页面日志才反映终态 |
| 截图数量多，但严格价格点为0 | 图片、AI建议、范围核验和正式逐日读数是不同阶段 |
| 估算指数待重算 | 来源或任务发生变化，检查后显式生成新版本 |
| 手机无动作 | 检查是否明确启动、是否有启用任务、实际权限/前台/解锁状态；不要通过启用全目录绕过 |

## 开发验证

```bash
uv sync --group dev
uv run pytest -q
node --test tests/*.mjs
```

最后一项使用Node.js 22+，示例为支持shell通配符的终端。Python测试默认库隔离，Node测试使用mock，不操作真实手机。测试通过不等于平台可永久访问或无人值守手机流程已验收。
