' AlphaQuant —— 后台启动服务（完全无窗口，不弹黑框）
'
' 用法：双击本文件
'
' 为什么需要一个 .vbs：
'   .bat 双击一定会闪一个 cmd 黑框（哪怕里面用了 start /b）。
'   VBS 的 WScript.Shell.Run 第二个参数传 0 就是"隐藏窗口运行"，
'   这才是 Windows 上真正"无感启动"的做法。
'
' 本脚本做两件事：
'   1. 用 pythonw.exe 跑 `main.py start`（隐藏窗口，False=不等待）
'   2. 等几秒后打开监控面板页面

Option Explicit

Dim fso, shell, root, pyw, mainPy, cmd

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' 本文件在 <root>\scripts\windows\ 下，往上两级就是工程根
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

pyw    = root & "\.venv\Scripts\pythonw.exe"
mainPy = root & "\main.py"

' 没有 pythonw 就退回 python.exe（此时可能仍有黑框，提示一下）
If Not fso.FileExists(pyw) Then
    pyw = root & "\.venv\Scripts\python.exe"
End If
If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"
End If

shell.CurrentDirectory = root
cmd = """" & pyw & """ """ & mainPy & """ start"
shell.Run cmd, 0, False

' 给服务一点时间起来，再打开面板
WScript.Sleep 4000
shell.Run "http://127.0.0.1:8787/", 1, False
