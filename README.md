# DiskViz

**让 AI 告诉你：每个大文件到底是什么，删了会怎样。**

Windows 磁盘清理工具。扫完盘不是甩给你一堆 `C:\Users\...\AppData\...\` 路径，而是逐个文件给一句人话：

> ✅ 可删除 ｜ Chrome GPU缓存，长期未使用 ｜ 程序自动重建
> ⚠️ 需确认 ｜ MineContext 应用核心包，软件依赖 ｜ 删除后软件失效
> ❌ 不要删 ｜ WSL 虚拟磁盘文件 ｜ 删了 Linux 子系统里的数据全没

它**从不自动删除**，所有操作都要你自己点；删掉的东西先进回收站，随时能还原。

---

## 为什么开源

这个项目最初是想做成付费产品：9.9 元买断，用户自带一点算力成本，我赚个辛苦钱。

后来发现三件事，把这条路堵死了：

1. **同类太多了。** 国内有 MangoDisk、柠檬清理、Pinkbin、ai-disk-cleaner…… 全开源、全免费；海外有 Smart Space Saver，功能几乎是同一个东西，还免费。
2. **没有壁垒。** "接个大模型给文件写人话解释"这件事，一个周末就能做出来。真正的门槛在于规则打磨和体验，但那撑不起 9.9 元的护城河。
3. **成本结构是反的。** 一次全盘扫描要送几万 token 给模型，按量付费是持续的，而买断收入是一次性的。用户越活跃，亏得越多。

所以开源是最优解：**代码交出去，让愿意折腾的人自己跑；我不用再替所有人付 token 钱。**

---

## 怎么用

### 方式一：下载打包好的 exe

见 [Releases](../../releases)。解压后双击 `DiskViz.exe`（会请求管理员权限，见下方说明）。

### 方式二：跑源码

```bash
pip install -r requirements.txt
python app.py
```

浏览器会自动打开 `http://127.0.0.1:54329`。**不要直接关网页**——程序跑在系统托盘里，
退出请右键托盘图标选「退出程序」。

以管理员身份运行能读 MFT，整盘扫描快 5~10 倍；普通权限会自动退回目录遍历，
功能不受影响，只是慢一些：

```powershell
Start-Process python -ArgumentList "app.py" -Verb RunAs
```

### 方式三：自己打包

```powershell
.\build.ps1
```

产物在 `dist\DiskViz.zip`。

---

## 第一次运行：填你自己的 API Key

DiskViz **不内置任何 API Key**。程序启动后会弹出设置面板，填三样东西：

| 字段 | 填什么 |
|---|---|
| Base URL | 任何 OpenAI 兼容接口，如 `https://api.deepseek.com/v1` |
| API Key | 你自己的 key |
| 模型名 | 如 `deepseek-chat`、`gpt-4o-mini` |

面板里有一键预设：OpenAI / DeepSeek / Moonshot / 本地 Ollama / 自定义。填完点「测试连接」确认能用。

配置存在 `%APPDATA%\DiskViz\config.json`，**明文**。它不会上传到任何地方，但别把这个文件发给别人。

### 关于本地 Ollama

如果你在意隐私，就装 [Ollama](https://ollama.com)，`ollama pull qwen2.5:14b`，然后在设置里选「本地 Ollama」。这样所有文件路径都在你自己机器上处理，一个字节都不出网。缺点是 14B 模型的判断质量比不上在线大模型。

---

## 它是怎么扫盘的（技术说明）

传统工具用 `os.walk` / `FindFirstFile` 逐目录问系统"这一层有什么"，百万文件要几分钟。

DiskViz 直接读 **NTFS 的 Master File Table**——卷上所有文件的名字、父目录、大小都在这一张表里，位置固定、顺序排列。所以可以一次性整块读进内存，本地解析出完整目录树。

`mft.py` 是纯 `ctypes` 实现，零第三方依赖。实测 C 盘（MFT 3.0 GB / 约 306 万条记录）：

| 步骤 | 耗时 |
|---|---|
| 读 $MFT | ~13 s |
| 解析记录 + 拼路径 | ~7 s |
| **合计** | **约 20 s** |

对照：`os.walk` 扫同样的盘要 3~8 分钟。

**为什么需要管理员权限**：打开 `\\.\C:` 卷设备要 `GENERIC_READ`，没有管理员会被拒绝（`ERROR_ACCESS_DENIED`）。DiskViz 的 exe 声明了 `requireAdministrator`，所以会弹一次 UAC——这跟 WizTree、DiskGenius 是一样的。如果你用普通权限跑源码，程序会自动降级成目录遍历，只是慢一些。

解析 MFT 有个必踩的坑：**Update Sequence Array**。NTFS 会把每条记录每个扇区末尾的 2 字节换成序列号，原值存在记录头部的数组里。不修它，跨扇区边界的属性字段会读到序列号——典型症状是文件大小算出天文数字（我曾解析出 91 TB 的 Edge 安装包）。`mft.py` 的 `_fixup()` 处理了这件事。

---

## 隐私

- **API Key**：只存本地 `%APPDATA%\DiskViz\config.json`。
- **文件路径**：会被发给**你自己配置**的模型接口，用于判断文件用途。发给谁是你在设置里决定的，不是我能决定的。
- **不收集任何统计、不埋点、不联网对时**（早期版本有过，已删）。
- 程序只读取文件元数据（路径、大小、时间），**不读取文件内容**。

---

## 已知限制

- **只支持 Windows**。MFT 读取是 NTFS 专有；FAT32 / exFAT / 网络盘会退回目录遍历。
- **只对 NTFS 有效**。ReFS 卷不适用。
- **$MFT 必须连续**。极少见的碎片化 MFT 会导致结果缺失（不会崩，只是少几项）。
- 判断质量取决于你用的模型。便宜的模型会在"SDK 库文件"这类边界情况上翻车，代码里有硬规则兜底，但没有全覆盖。
- 界面目前只有中文，英文版还没做。

---

## 结构

```
mft.py           读 NTFS MFT 的扫描内核（纯 ctypes）
config.py        BYOK 配置读写
1_Scan.py        扫描：调 mft.py → 增强元数据（siblings/安装状态/是否在用）→ 入库
2_AI.py          AI 分析：分批送模型 → 解析标签 → 写库
app.py           Flask 主程序 + 托盘 + 设置接口
app_routes.py    「问一问」对话、记忆
static/index.html  单文件前端
```

数据流：`1_Scan` 产出 `entries` 表 → `2_AI` 读它、写 `analysis` 表 → 前端读两张表。

---

## 许可

MIT。随便用、随便改、随便商用，出事别找我。

第三方：本项目**不包含、不分发** WizTree 或任何其他第三方扫描器（早期版本曾经依赖 WizTree，因其 EULA 要求商业分发许可，已用自研 MFT 内核完全替换）。

前端 Markdown 渲染用 [marked](https://github.com/markedjs/marked) v9.1.6（MIT），已内置在 `static/marked.min.js`。不用公共 CDN——国内 CDN 常年抽风，而且本地化之后整个前端零外部请求，配合本地 Ollama 时真正做到一个字节不出网。
