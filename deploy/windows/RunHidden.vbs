' Runs one of the two launcher scripts with NO console window (so nothing flashes on screen).
' Used by the desktop shortcuts. Only these two scripts, in this folder, can be started.
Option Explicit
Dim sh, name, folder, command
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count < 1 Then WScript.Quit 2
name = WScript.Arguments(0)
If name <> "Start-CovensLibrary.ps1" And name <> "Stop-CovensLibrary.ps1" Then WScript.Quit 2
folder = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & folder & name & """"
sh.Run command, 0, False
