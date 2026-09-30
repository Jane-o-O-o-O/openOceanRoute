Unicode true
Name "OceanRoute"
OutFile "payload-0.12.1/OceanRoute.exe"
RequestExecutionLevel user
SilentInstall silent
AutoCloseWindow true
!include "FileFunc.nsh"
Section
  SetOutPath "$EXEDIR"
  ${GetParameters} $0
  StrCmp $0 "--stop" stop run
  stop:
    ExecWait '"$EXEDIR\runtime\pythonw.exe" -B "$EXEDIR\windows_app_0_12_1.py" --stop' $1
    SetErrorLevel $1
    Goto done
  run:
    Exec '"$EXEDIR\runtime\pythonw.exe" -B "$EXEDIR\windows_app_0_12_1.py"'
    IfErrors failed done
  failed:
    MessageBox MB_ICONSTOP "OceanRoute 启动失败。请重新安装。"
    SetErrorLevel 1
  done:
SectionEnd
