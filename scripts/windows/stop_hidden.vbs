' AlphaQuant —— 停止后台服务（完全无窗口）

Option Explicit

Dim fso, shell, root, pyw, mainPy, cmd

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

pyw    = root & "\.venv\Scripts\pythonw.exe"
mainPy = root & "\main.py"

If Not fso.FileExists(pyw) Then
    pyw = root & "\.venv\Scripts\python.exe"
End If
If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"
End If

shell.CurrentDirectory = root
cmd = """" & pyw & """ """ & mainPy & """ stop"
shell.Run cmd, 0, True   ' 等待结束，好让下面的提示准确

WScript.Sleep 1000
shell.Run """" & pyw & """ """ & mainPy & """ status", 0, True
