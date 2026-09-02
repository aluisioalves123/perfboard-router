@echo off
cd /d "%~dp0"
gcc -O2 -Wall -Wextra -shared -o perfboard.dll perfboard.c place.c
echo compilado: perfboard.dll
