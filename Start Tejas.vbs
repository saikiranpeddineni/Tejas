' Start Tejas — double-click to launch the dashboard (no console window).
' Starts the local web server hidden; it auto-opens your browser to the UI.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' pythonw runs without a console window. If pythonw is unavailable, falls back to python.
On Error Resume Next
sh.Run "pythonw """ & dir & "\webui.py""", 0, False
If Err.Number <> 0 Then
    sh.Run "python """ & dir & "\webui.py""", 0, False
End If
