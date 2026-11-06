# "Let your AI assistant install it": the copy-paste text for the website

For the website's download section: a box with a Copy button holding one of the two texts below (English, 中文),
for people who would rather let Claude Code or Codex do the install. The website fills in the placeholders on each
release, so the expected hash comes from the website as well as from the release itself:

- `{{VERSION}}`: the release version, e.g. `0.7.0`
- `{{RELEASE_URL}}`: that release's page, `https://github.com/<owner>/<repo>/releases/tag/v{{VERSION}}`
- `{{SETUP_SHA256}}`: the SHA-256 of `PremiereAgentBridge-Setup-{{VERSION}}.exe` as published with the release (the
  signed file's, once releases are signed)

The text only lets the assistant download two files from the release page, check them, and start the installer after
the user agrees. Everything that needs admin rights or Adobe's apps stays with the user.

---

## English

```text
Please install Premiere Agent Bridge {{VERSION}} on this Windows PC for me. Follow these steps exactly and ask me
before each download and before running anything.

1. Open {{RELEASE_URL}} (GitHub). Find the assets PremiereAgentBridge-Setup-{{VERSION}}.exe and
   PremiereAgentBridge-Setup-{{VERSION}}.exe.sha256. Tell me their names and sizes and ask me before downloading.
   Download only these two, into a new empty folder under my Downloads folder. Use no other site or mirror.
2. Check the file before anything runs: in PowerShell,
   (Get-FileHash .\PremiereAgentBridge-Setup-{{VERSION}}.exe -Algorithm SHA256).Hash
   It must equal {{SETUP_SHA256}} (from the project's website) and the hash in the .sha256 file, ignoring case.
   If either differs, stop, delete the download and tell me.
3. Check the signature: (Get-AuthenticodeSignature .\PremiereAgentBridge-Setup-{{VERSION}}.exe).Status
   "Valid" with signer "SignPath Foundation" is a signed release. "NotSigned" is expected until the project's
   signing is approved; tell me which it is. Any other status: stop and tell me.
4. Ask me before starting the installer. Start it normally (no silent switches) so I see the setup wizard. It
   installs for my user only and needs no administrator rights; if Windows SmartScreen says "Windows protected your
   PC" for an unsigned release, tell me it is expected and let me decide whether to click "More info > Run anyway".
5. When the installer finishes, the "Premiere Agent Bridge Setup" window opens and checks this PC. Read it with me.
   Installing UXP Developer Tool (Creative Cloud app) and turning on developer mode need me and administrator
   rights: tell me what to click, do not try to do it yourself.
6. Afterwards, read SKILL.md in the install folder (%LOCALAPPDATA%\Programs\Premiere Agent Bridge). The bridge
   brings its own Python: run its commands as
   & "$env:LOCALAPPDATA\Programs\Premiere Agent Bridge\python\python.exe" -I "$env:LOCALAPPDATA\Programs\Premiere Agent Bridge\cli\ppro.py" ping

Rules: do not disable SmartScreen, Windows Defender or any other security setting; do not use administrator
rights; do not change PowerShell's execution policy; do not run any other file from the download; never open, print
or copy the file %USERPROFILE%\.ppro-agent-bridge\token. If something fails, stop and show me the message.
```

---

## 中文

```text
请帮我在这台 Windows 电脑上安装 Premiere Agent Bridge {{VERSION}}。严格按下面的步骤做；每次下载之前、运行任何东西之前，
都先问我。

1. 打开 {{RELEASE_URL}}（GitHub），找到 PremiereAgentBridge-Setup-{{VERSION}}.exe 和
   PremiereAgentBridge-Setup-{{VERSION}}.exe.sha256 这两个文件。告诉我文件名和大小，问过我再下载。只下载这两个，
   放进“下载”文件夹里新建的一个空文件夹。不要用别的网站或镜像。
2. 运行之前先核对文件：在 PowerShell 里执行
   (Get-FileHash .\PremiereAgentBridge-Setup-{{VERSION}}.exe -Algorithm SHA256).Hash
   结果必须和 {{SETUP_SHA256}}（项目网站上的值）以及 .sha256 文件里的值一致（不分大小写）。有一个对不上就停下，
   删掉下载的文件，告诉我。
3. 核对签名：(Get-AuthenticodeSignature .\PremiereAgentBridge-Setup-{{VERSION}}.exe).Status
   “Valid”且签名者是“SignPath Foundation”，是签过名的正式版；项目的签名申请批下来之前，“NotSigned”是正常的。
   告诉我是哪一种。其他状态就停下告诉我。
4. 启动安装程序之前先问我。正常启动（不加静默参数），让我看到安装向导。它只给我这个用户安装，不需要管理员权限。
   如果是没签名的版本，Windows SmartScreen 会提示“Windows 已保护你的电脑”：告诉我这是预期的，由我决定要不要点
   “更多信息 → 仍要运行”。
5. 安装完成后会打开“Premiere Agent Bridge Setup”检查窗口，和我一起看。安装 UXP Developer Tool（在 Creative Cloud
   里）和打开开发者模式需要我本人和管理员权限：告诉我点哪里，不要自己去做。
6. 装好以后，读安装文件夹（%LOCALAPPDATA%\Programs\Premiere Agent Bridge）里的 SKILL.md。桥自带 Python，命令这样运行：
   & "$env:LOCALAPPDATA\Programs\Premiere Agent Bridge\python\python.exe" -I "$env:LOCALAPPDATA\Programs\Premiere Agent Bridge\cli\ppro.py" ping

规则：不要关闭 SmartScreen、Windows Defender 或任何安全设置；不要用管理员权限；不要改 PowerShell 的执行策略；不要运行
下载里的其他文件；绝不打开、显示或复制 %USERPROFILE%\.ppro-agent-bridge\token 这个文件。出了问题就停下，把提示给我看。
```
