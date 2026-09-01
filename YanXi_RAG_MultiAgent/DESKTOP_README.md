# 言犀RAG多Agent桌面工作台

本说明保留原桌面入口的语音操作细节。新核心、机主/号码管理、安装与评测对照请优先阅读 [本工程README](README.md)。

## 启动

双击项目根目录的 `启动言犀桌面版.bat`。也可以在 PowerShell 中执行：

```powershell
cd "C:\Users\LZM\Desktop\auto\言犀AI管家_不同版本\YanXi_RAG_MultiAgent"
.\.venv\Scripts\python.exe desktop.py
```

本工程提供 `desktop.py` 与 `evaluation.py` 两个独立GUI入口，不包含旧工程的HTTP或电话接入服务。

## API 设置

- 首次没有密钥时弹出设置窗口；已有 `.env` 的 Qwen 密钥会自动读取。
- 左侧“API 与语音设置”随时修改服务、模型、区域、密钥、识别模型和播报音色。
- 新主界面使用 Qwen，默认为中国内地 `qwen-plus`；相同Key还需具有 `text-embedding-v4` 向量权限。
- 密钥默认保存在项目 `.env`（**本地明文**，不是加密保险库），已被 Git 忽略。
- 取消“保存到本机”会移除所选服务在 `.env` 中的旧密钥，仅本次进程使用新密钥。
- 保存立即应用，不必重启。测试连接会发出一次短请求，可能消耗少量额度。
- 非密钥设置保存在 `data/desktop/settings.json`；不会在 JSON 结果中保存密钥。

## 输入和批量处理

1. **麦克风**：选择系统默认或刷新后指定设备 → 开始录音 → 停止并加入队列。可自动处理；最长 10 分钟。
2. **单个音频**：在“本地音频 / 批量”选择一个文件，再开始处理。
3. **批量音频**：多选文件，或导入一个文件夹的第一层音频。支持 WAV/MP3/M4A/FLAC/OGG/AAC 等；不递归子目录。
4. **文字**：输入或粘贴来电内容，加入队列；不需要加载语音识别模型。

所有音频按顺序处理，每个文件都是独立来电。单条失败会保留错误并继续下一条。
“完成当前后停止”不会中断正在下载的模型、本地推理或已发出的 API 请求；剩余条目保持待处理，可继续。
“重试异常”将失败或模型降级项恢复待处理并启动队列。来电号码可选，批量导入时应用同一个填写的号码；不填则不人为关联真实联系人。
模型网络调用失败而规则仍能给出结果时，单独标记“降级完成”（黄色），不是正常模型成功返回。

## 结果和语音输出

- 点击队列条目，在“处理结果 / 识别文字 / 结构化结果”页签查看。
- 结果包括转写、来电类型、置信度、处理建议、AI 回复、通知摘要和错误信息。
- 可复制当前页，或导出全部任务为 UTF-8 JSON。
- “朗读选中结果”可选 AI 回复或完整摘要；拒接等没有口头回复时，会朗读处理摘要。
- 可勾选整批完成后朗读当前选中结果；不是每条音频都自动播放。
- “停止朗读”可在合成或播放中取消。播报和麦克风录音互斥，避免声音被重新录入。

## 语音实现与依赖

参考同目录 `PythonProject2` 的实现：使用 **sounddevice 采集 + Whisper turbo 中文识别**，保留中文电话场景提示词、`fp16=False`、`temperature=0.0`。
不导入或修改旧工程，也不加载其 RAG/密钥配置。

- 默认使用 `whisper / turbo`，复用当前 Windows 用户 `~/.cache/whisper/large-v3-turbo.pt`。
- 可选择更轻的 `small/base/tiny`，或切换 Faster-Whisper CPU/int8。
- Whisper 需要 FFmpeg。支持系统 PATH，也识别本机已有的 ffmpegio 下载位置。
- Faster-Whisper 模型缓存放在项目 `models/whisper`；未缓存时会询问是否下载，未获同意不开始下载。
- Edge TTS 需要网络，不是纯离线语音合成。
- 重型模块懒加载，下载、识别、分析和播放均在后台；Tk 控件只由主线程更新。

重建环境（Python 3.12 自带 Tkinter）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
```

本机创建的 `.venv` 复用了系统现有 Python 3.12 包，避免重复下载已安装的 Whisper/PyTorch；新机器建议用上述隔离环境安装。

## 隐私、保存位置和边界

- 麦克风音频：`data/desktop/recordings/`
- 每条处理结果：`data/desktop/results/`
- 原工程的画像和通话日志仍在 `data/` 对应目录。
- 音频在本地识别，**转写文字会发送给所选 LLM 服务**；选择朗读时，**朗读文字会发送给 Edge TTS**。
- 本地录音、转写和分析结果可能包含隐私，请按需要管理；`.env` 不要分享、截图或上传。
- 界面是来电内容分析入口；“转接/拒接”等是建议，**不会真实接听、拨打或挂断电话**。
- 本工程已替换为PythonProject2衍生的真实混合RAG与多Agent流程；没有实现真实电话或HTTP服务。

## 验证

不访问麦克风、不联网、不调用真实 API 的测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_desktop -v
```

完整新工程回归命令见README；不将模拟API的测试结果当成真实正确率。

遇到录音设备错误时检查 Windows 麦克风权限、设备占用和输入设备选择。遇到模型下载错误可改用已有缓存模型；不要在程序仍处理当前任务时强行删除缓存。
