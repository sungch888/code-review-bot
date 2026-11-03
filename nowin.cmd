@echo off
rem 无窗口启动器：用 start /b + CREATE_NO_WINDOW 语义运行命令，不弹黑窗
rem 用法: nowin.cmd <命令...>
python "%~dp0nowin.py" %*
