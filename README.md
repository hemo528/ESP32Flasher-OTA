# ESP32Flasher-OTA · 喵刷固件商店

> ESP32 固件刷写工具「**喵刷**」的**固件商店 / OTA 更新仓库**：用 git 仓库文件承载 `.fwpack` 固件包，用 `firmware.json` 索引统一分发。

刷机工具启动时拉取本仓库的 `firmware.json`，即可浏览 / 搜索固件 → 下载 → SHA-256 校验 → 入库存放；「刷写」页会对已安装固件打「可更新」标记。全程 HTTPS + SHA-256 校验，匿名可读，无需登录、无需 GitHub API。

工具自身也支持在线更新：新版本 exe 发布到本仓库 	ool/ 目录，客户端启动时检查、一键下载校验并自动替换重启（保留 .exe.old 可回滚）。

---

## 索引地址

| 用途 | 地址 |
| --- | --- |
| **主**（GitHub raw） | `https://raw.githubusercontent.com/hemo528/ESP32Flasher-OTA/main/firmware.json` |
| 备用（jsDelivr CDN，国内访问较快） | `https://cdn.jsdelivr.net/gh/hemo528/ESP32Flasher-OTA@main/firmware.json` |
| 备用（ghproxy 加速） | `https://ghproxy.net/https://raw.githubusercontent.com/hemo528/ESP32Flasher-OTA/main/firmware.json` |
| 备用（gh-proxy 加速） | `https://gh-proxy.com/https://raw.githubusercontent.com/hemo528/ESP32Flasher-OTA/main/firmware.json` |

工具会并行探测以上镜像、先到先用；固件包下载走「上次成功的镜像优先 + 失败自动换下一个」，≥4 MB 的大文件（如工具 exe）优先走较快的代理镜像。镜像前缀 + 索引内 `versions[].url` 拼成完整下载地址。

raw / jsDelivr 缓存约 5 分钟：刚发布新版本时，工具检查更新会带 `?t=<时间戳>` 绕开缓存，避免拿到旧索引。

---

## 当前固件

| 固件 | id | 版本 | 标签 | 包大小 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 4.2 寸墨水屏 · 面板自检 | `eink-42bw` | 0.1.1 | 墨水屏 | 203.4 KB | SSD1619A 4.2 寸黑白墨水屏出厂自检：四色块、棋盘、边界框与全刷计时 |
| 4.2 寸三色墨水屏 · 面板自检 | `eink-42c` | 0.1.0 | 墨水屏 | 210.8 KB | SSD1683 4.2 寸黑/白/红三色墨水屏自检：色块、棋盘格、边框角标与文字信息，串口输出芯片信息与全刷耗时 |
| 4.2 寸三色墨水屏 · 桌面信息屏 | `eink-info-screen` | 1.0.4 | 墨水屏 | 737.7 KB | 时间 / 天气 / 月历 / 用量 / 系统五页面信息屏，400×300 三色；网页后台可改配置、上传自定义图片 |
| RLCD 桌面信息屏 | `rlcd-screen` | 1.2.0 | 桌面屏 | 978.7 KB | 八页面桌面信息屏：时钟 / 温湿度 / 天气 / 额度 / 日历，接入真实接口数据 |
| SSH 终端 | `ssh-terminal` | 1.0.0 | 工具 | 1.24 MB | 全反射屏 SSH 客户端，可在掌机形态下直接连接服务器操作终端 |
| 桌面信息屏（旧版） | `desktop-info` | 1.1.0 | 归档 | 806.3 KB | LVGL 版桌面信息屏（旧版归档），已移除内置凭据 |
| DOOM 全反射屏掌机 | `doom-rlcd` | 1.0.0 | 游戏 | 2.36 MB | 经典 DOOM 移植到全反射屏掌机，网页手柄操控、24fps 运行 |

> 上表与 `firmware.json` 保持一致，最新状态以索引为准（共 7 个固件、7 个版本，合计约 6.47 MB）。
> **所有固件包均不含任何用户凭据**（WiFi 名称/密码、API Key、AK/SK 等）；旧版 `desktop-info` v1.0.0 因内含个人配置已于 2026-11-10 下架删除。
> 墨水屏正式产品固件（ESP-IDF 版）待屏幕显示问题闭环后按同样流程上架，包结构不变。

---

## 刷机工具自身更新（`tool` 块）

索引顶层可选一个 `tool` 块，描述刷机工具 exe 的最新版本；旧版客户端会忽略它，不影响固件下载。

```json
"tool": {
  "latest": "1.3.0",
  "versions": [
    {
      "version": "1.3.0",
      "url": "tool/FirmwareFlasher-1.3.0.exe",
      "size": 21416931,
      "sha256": "0901c2be0fbd6a03…（64 位小写十六进制）",
      "builtAt": "2026-11-13T00:32:41Z",
      "changelog": "刷机工具自身支持在线更新：启动检查、一键下载校验、自动替换重启"
    }
  ]
}
```

- 只保留最近 **2 个版本**（`scripts/publish-tool.py -KeepVersions 2`），多余文件随发布删除
- 工具端流程：启动检查（24 小时最多一次）→ 标题栏「⬆ 新版本」→ 弹窗 → 下载 → size + SHA-256 校验
  → 退出并由 `%LOCALAPPDATA%\ESP32Flasher\store\update\update-<版本>.ps1` 替换 exe → 自动重启；
  旧 exe 保留为 `FirmwareFlasher.exe.old`，回滚时手动改名回来即可
- 校验项：文件存在、`size`、`sha256`、**PE 头 `MZ`**，以及 `tool/` 下无孤儿 exe

---

## 目录结构

```
ESP32Flasher-OTA/
├── README.md                        # 本文件：商店说明、固件表、发布流程
├── firmware.json                    # ★ 索引：刷机工具读取的唯一入口
├── firmware/                        # 固件包（.fwpack，按 id 分目录）
│   ├── <id>/<id>-v<version>.fwpack
│   └── …
├── tool/                            # 刷机工具 exe（工具自身在线更新用）
│   └── FirmwareFlasher-<version>.exe
├── scripts/
│   ├── publish.ps1                  # 发布新固件：校验 → 拷贝 → 改索引 → push
│   ├── validate.py                  # 索引 / 包一致性校验
│   ├── validate-workflow.yml        # GitHub Actions 校验模板（可选启用，见下文）
│   └── fwpack.py                    # 包格式参考实现（从 flasher 工程同步，原样拷贝）
```

---

## fwpack 格式

`.fwpack` 是一个 ZIP 文件，内含 `manifest.json` + 若干 bin（允许清单放在子目录）。manifest 记录包标识、版本、目标芯片、flash 参数（mode/freq/size），以及每个 bin 的刷写偏移（`offset`）与 SHA-256。工具下载后会先校验整包 SHA-256（索引里的 `size`/`sha256`），再用 manifest 校验每个 bin，双重一致才允许刷写。

完整格式规范见刷机工具工程内的 `docs/fwpack-format.md`，参考实现为 `fwpack.py`。要点：

- `format` 固定以 `fwpack/` 开头（当前 `fwpack/1`）；
- `files` 至少一个条目，按 `offset` 升序；`offset` 是十六进制字符串（如 `0x010000`）；
- 每个 bin 的 `sha256` 是整个文件的摘要，刷写前逐个校验；
- `flash.mode` 写进镜像头，**不要随手改成 qio**，填错会导致设备无限重启。

---

## 索引 schema（`fwstore/1`）

```json
{
  "schema": "fwstore/1",
  "generatedAt": "2026-11-10T06:00:00Z",
  "homepage": "https://github.com/hemo528/ESP32Flasher-OTA",
  "mirrors": ["https://raw.githubusercontent.com/hemo528/ESP32Flasher-OTA/main/"],
  "firmwares": [
    {
      "id": "rlcd-screen",
      "name": "RLCD 桌面信息屏",
      "tag": "桌面屏",
      "chip": "esp32s3",
      "desc": "八页面 · 真实接口数据",
      "latest": "1.2.0",
      "versions": [
        {
          "version": "1.2.0",
          "url": "firmware/rlcd-screen/rlcd-screen-v1.2.0.fwpack",
          "size": 1002160,
          "sha256": "ef1cc0480ffe7d16…（64 位小写十六进制）",
          "builtAt": "2026-11-01T06:24:13Z",
          "changelog": "八页面桌面信息屏：时钟 / 温湿度 / 天气 / 额度 / 日历，接入真实接口数据",
          "minFlasher": "1.2.0",
          "flash": { "mode": "dio", "freq": "80m", "size": "16MB" },
          "files": [
            { "offset": "0x000000", "path": "bootloader.bin", "size": 22272, "role": "bootloader" },
            { "offset": "0x008000", "path": "partition-table.bin", "size": 3072, "role": "partition" },
            { "offset": "0x010000", "path": "rlcd_frame_test.bin", "size": 1682736, "role": "app" }
          ]
        }
      ]
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | ✓ | 固定 `fwstore/1`；不匹配时工具提示「商店索引版本不支持」 |
| `generatedAt` | ✓ | 索引生成时间（ISO8601 UTC） |
| `homepage` | ✓ | 仓库主页 |
| `mirrors` | ✓ | 镜像前缀列表，均以 `/` 结尾；工具按顺序探测 |
| `firmwares[].id/name/tag/chip/desc` | ✓ | 展示用；`chip` 用于刷写前提示（不匹配只警告不阻拦） |
| `firmwares[].latest` | ✓ | 冗余字段，必须等于 `versions[0].version`（validate.py 强制） |
| `firmwares[].versions[]` | ✓ | 按版本号**从新到旧**排列，工具取 `versions[0]` 为最新 |
| `versions[].version` | ✓ | 形如 `x.y.z`，同一 id 下唯一且单调递增（CI 强制） |
| `versions[].url` | ✓ | **仓库内相对路径**（不是绝对 URL），镜像切换时无需改索引 |
| `versions[].size` | ✓ | 整包 `.fwpack` 文件字节数（不是包内某个 bin） |
| `versions[].sha256` | ✓ | 整包 `.fwpack` 文件的 SHA-256（64 位小写十六进制） |
| `versions[].builtAt` | ✓ | 固件构建时间（ISO8601 UTC），取自包内 manifest |
| `versions[].changelog` | ✓ | 更新说明，支持 `\n` 换行 |
| `versions[].minFlasher` | | 需要的最低刷机工具版本，低于时禁用安装并提示升级 |
| `versions[].flash` | ✓ | 刷写参数，**直接从包内 manifest 的 `flash` 拷贝**：`mode` / `freq` / `size` |
| `versions[].files` | ✓ | 包内文件清单，**直接从包内 manifest 的 `files` 拷贝**：`offset`（hex 字符串原样）/ `path` / `size`（整数）/ `role`，按 `offset` 升序 |

> `flash` / `files` 与包内 manifest 由 `scripts/validate.py` 交叉校验，必须完全一致——工具详情面板直接展示这两项，不需要先下载包。
> 索引里**不重复**维护版本之外的内容；包内清单是唯一事实来源，索引只是它的镜像。

---

## 发布新版本

发布脚本在仓库根目录执行（任意工作目录均可，脚本按自身位置定位仓库）：

```powershell
# 1. 用刷机工具的 make_pack.py 打出 .fwpack（或在构建目录直接产出）
# 2. 预演：只打印将要执行的操作，不复制 / 不改索引 / 不提交
.\scripts\publish.ps1 -Pack 'C:\build\rlcd-screen-v1.3.0.fwpack' -Changelog "1. 新增大模型额度页" -DryRun

# 3. 正式发布（校验 → 拷贝 → 改索引 → 裁剪旧版本 → git commit & push）
.\scripts\publish.ps1 -Pack 'C:\build\rlcd-screen-v1.3.0.fwpack' -Changelog "1. 新增大模型额度页"
```

发布刷机工具新版本（先构建 exe，再推 tool 块）：

```powershell
# 预演
python .\scripts\publish-tool.py -Exe C:\path\FirmwareFlasher.exe -Version 1.3.1 -Changelog "更新说明" -DryRun
# 正式发布（校验 → 拷贝 → 改 tool 块 → 裁剪 → git commit & push）
python .\scripts\publish-tool.py -Exe C:\path\FirmwareFlasher.exe -Version 1.3.1 -Changelog "更新说明"
```

`publish-tool.py` 参数：`-Exe` 必填、`-Version` 必填（必须大于当前 tool.latest）、`-Changelog`、
`-KeepVersions` 默认 2、`-DryRun`、`-Force`（允许同版本覆盖）；同版本同 SHA-256 幂等跳过。

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-Pack` | 必填 | 一个或多个 `.fwpack` 文件路径 |
| `-Changelog` | 包内 `desc` | 本次发布的更新说明（中文） |
| `-KeepVersions` | `3` | 每个固件保留的历史版本数，超出部分删旧文件 + 删索引条目 |
| `-DryRun` | — | 预演模式：只打印操作，不落盘、不提交、不推送 |
| `-Force` | — | 允许覆盖同版本（文件内容不同时覆盖；索引同版本条目被替换） |

脚本行为：

1. 解析包内 `manifest.json`，并调用 `scripts/fwpack.py` 做一次完整包校验（format / 文件 / 内部 SHA-256）；
2. 拷贝到 `firmware/<id>/<id>-v<version>.fwpack`；目标已存在且 SHA-256 相同 → 幂等跳过；
3. 用实际文件计算 `size` / `sha256`，写入 `firmware.json`；**版本必须大于当前最新**，回退直接拒绝（`-Force` 只允许同版本覆盖）；
4. 版本从新到旧排序，裁剪超过 `-KeepVersions` 的旧条目并删除旧文件；
5. `git add -A` → `git commit -m "firmware(<id>): v<version>"` → `git push`；
6. 打印 raw 下载地址，并提示 CDN 缓存约 5 分钟。

发布后 5 分钟内不要重复发布同一版本（raw CDN 未刷新，工具可能仍拿到旧索引）。

---

## 校验与 CI

本地校验（纯 Python 标准库，无需装依赖）：

```powershell
python scripts/validate.py
```

校验内容：`schema` 前缀、`mirrors` 格式、固件必填字段、`latest == versions[0].version`、版本号格式与唯一性、`url` 相对路径与文件存在、`size`/`sha256` 与整包一致、包内 manifest 可解析（format / files / offset 合法且不重复 / 内部文件存在且 SHA-256 一致）、索引 `flash`/`files` 与包内 manifest 完全一致、`firmware/` 下无孤儿包；以及 `tool` 块（`latest` 与排序、文件存在、size/sha256、PE 头 `MZ`、孤儿 exe）。输出中文 ✓/✗ 列表，退出码 0 = 通过、1 = 失败。

**可选 CI**：`scripts/validate-workflow.yml` 是现成的 GitHub Actions 工作流模板。由于推送 workflow 文件需要 token 带 `workflow` 权限，本仓库默认不启用；需要时执行：

```powershell
gh auth refresh -h github.com -s workflow   # 给 gh token 加 workflow 权限（浏览器确认一次）
mkdir .github\workflows
move scripts\validate-workflow.yml .github\workflows\validate.yml
git add -A; git commit -m "ci: 启用 validate 工作流"; git push
```

---

## 免责声明

- 本仓库固件为**个人开发**，仅供**个人学习**与**自有设备刷写**使用；商用或二次分发请联系作者获得许可。
- 刷机有风险：操作不当可能导致设备无法启动。请确认固件 `chip` 与目标设备一致（本仓库当前均为 `esp32s3`），并自行承担刷写风险；作者不承担设备损坏责任。
- 固件包由本仓库索引与 SHA-256 保证完整性，请只从上方几个镜像地址下载。

## License

- 索引与脚本（`firmware.json`、`scripts/`）：[MIT](LICENSE)。
- `firmware/` 下的固件包及其内容：版权归作者所有，授权范围见 [LICENSE](LICENSE) 中的「固件版权说明」。
