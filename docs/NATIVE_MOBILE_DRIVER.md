# 本地 iPhone 镜像控制 helper

`automation/native_mobile.swift` 使用 Apple 的 AppKit、ScreenCaptureKit、ApplicationServices 和 CoreGraphics。最低 macOS 14.2；只需 Command Line Tools，不需要 Xcode。它只允许 bundle `com.apple.ScreenContinuity`，不接受其他应用、PID、显示器或全屏捕获参数。

```bash
bash automation/build_native.sh
output/bin/native-mobile health
```

编译只生成并本地 ad-hoc 签名 `output/bin/native-mobile`，不会请求权限或操作界面。`health` 只检查辅助功能、屏幕录制授权和目标应用的可见窗口元数据，不截图、不弹权限框。`ready` 只表示权限与镜像窗口可用；`deviceUnlocked:null` 明确表示它不能凭窗口存在确认手机已解锁。

## 本人授权

只有用户明确开始授权时才运行：

```bash
output/bin/native-mobile request-permissions
```

该命令是唯一会调用系统授权提示的入口。本人在“系统设置 → 隐私与安全性”中授予实际提示的本机程序辅助功能与屏幕录制权限；必要时重启该程序再执行 `health`。系统权限范围比本 helper 的代码限制更广，应保留并审核源代码。重编译可能改变签名身份，授权是否继续生效以 `health` 实际结果为准。手机 Touch ID、密码、App 登录和平台验证由本人完成。

## CLI

所有命令将一条 JSON 写入 stdout；失败返回 `ok:false`、固定错误码并以状态2退出。框架错误不会原样回显参数，`type` 不输出或保存输入内容。

| 命令 | 行为 |
| --- | --- |
| `health` | 无提示权限预检；返回 `permissions`、`appRunning`、`windowFound`、`ready` |
| `state` | 已有屏幕录制授权后读取目标窗口的几何信息，不保存截图 |
| `screenshot OUT.png` | 仅捕获镜像完整窗口，包含顶栏、不含阴影、鼠标或子窗口；保存真实PNG |
| `click X Y` | 按最近一次本 helper 截图的像素坐标向镜像PID投递按下/抬起事件 |
| `type TEXT` | 用带窗口号的NSEvent向镜像PID输入；ASCII使用对应Mac物理键码，非ASCII保留Unicode路径；不激活其他应用、不使用剪贴板 |
| `focus` | 本人明确请求时，仅尝试让镜像主窗口获得焦点；先用公开AX属性，必要时只激活固定镜像应用 |
| `request-permissions` | 仅在本人准备授权时调用系统权限请求 |

`state/screenshot/click/type/focus` 成功响应包含：

```json
{
  "ok": true,
  "bundleIdentifier": "com.apple.ScreenContinuity",
  "pid": 123,
  "windowId": 456,
  "frame": {"x": 100, "y": 100, "width": 350, "height": 770},
  "imageWidth": 700,
  "imageHeight": 1540,
  "scale": 2,
  "scaleX": 2,
  "scaleY": 2,
  "includesTitlebar": true,
  "coordinateOrigin": "window_top_left_pixels",
  "deviceUnlocked": null
}
```

`frame` 是 Quartz 顶左坐标系中的完整窗口点坐标；PNG 也是完整窗口，点击不会错误减去顶栏。点击将截图像素换算为当前窗口点坐标。窗口移动可以重映射；窗口更换、缩放或距截图超过10分钟时拒绝点击，必须重新截图。几何缓存仅含窗口ID、尺寸、比例和时间，保存在当前用户临时目录，权限0600。

## 边界与验证

截图使用 `SCContentFilter(desktopIndependentWindow:)` 与 `SCScreenshotManager.captureImage`，从目标 bundle/PID 的可见主窗口中选择；找不到窗口或存在相近的歧义窗口时拒绝操作。不会捕获整个显示器、其他应用或最小化窗口。图片仅写入调用者指定的PNG路径，权限0600。

点击通过公开 `NSEvent.mouseEvent` 绑定目标 `windowNumber` 后取得 `cgEvent`；文字通过同样带目标窗口号的 `NSEvent.keyEvent`，ASCII字母、数字、空格与常用标点使用对应的Mac ANSI虚拟键码及Shift修饰，非ASCII使用Unicode事件。点击和文字都只使用 `CGEvent.postToPid`，不向全局事件流投递，也不移动系统鼠标或自动切前台。`health/state` 返回 `targetFrontmost` 和前台应用标识；镜像不是前台时，helper拒绝点击/文字，运行器应暂停并等本人切回，不得自动反复调用focus。只有独立显式 `focus` 命令可以请求镜像焦点，并返回 before/after、AX结果及是否请求了目标激活；它不会激活其他应用。

响应中的 `effectVerified:false` 和 `requiresVisualVerification:true` 明确表示尚未验证界面效果；`unicodeFallbackUsed` 标明是否使用可能被镜像忽略的非ASCII输入路径。事件投递成功不代表镜像/App已响应，必须以随后新截图和本地OCR核实。某些系统版本或镜像状态可能忽略定向事件；不能把这种情况报告为完成采集，也不能声称后台输入已验证成立。

没有 AppleScript、shell UI 脚本、任意程序执行或私有接口。helper 不保存手机凭据；`type` 应用于公开查询词，密码与本人验证不经自动流程。命令行参数在短暂运行期间可能被本机进程检查工具看见，不应将凭据作为 `TEXT` 传入。

当前验证范围：编译通过并仅执行无提示 `health`。权限请求、截图、点击和文字输入须在本人授权后由专属手机操作流程验收；尚未据此声称无人值守手机采集已通过。

API依据：[Apple 单窗口过滤器](https://developer.apple.com/documentation/screencapturekit/sccontentfilter/init(desktopindependentwindow:))、[Apple 截图接口](https://developer.apple.com/documentation/screencapturekit/scscreenshotmanager/captureimage(contentfilter:configuration:completionhandler:))、[Apple 辅助功能授权预检](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions)。
