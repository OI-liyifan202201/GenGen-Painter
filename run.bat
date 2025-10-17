pip install PyQt6 PyQt6-Fluent-Widgets pillow numpy websockets requests
@echo off
if "%1"=="h" goto begin
start mshta vbscript:createobject("wscript.shell").run("""%~nx0"" h",0)(window.close)&&exit
:begin
python Main.pyw
